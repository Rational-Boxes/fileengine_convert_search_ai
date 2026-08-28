# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Ingest worker — turn file-activity events into conversions.

Maps the core publisher's events (EVENT_CONTRACT.md) onto pipeline actions:
- file.created / file.updated / file.restored → (re)convert the file
- file.deleted                                → drop our document row (core cascades renditions)
- is_rendition events                         → ignored (our own output; avoids a feedback loop)
- dir.* / acl.* / role.*                      → ignored here (governance feeds M2's permission cache)

Conversion is idempotent, so at-least-once redelivery is safe; the reconcile
sweep is the backstop for anything missed during an outage.

Disconnected (read-only) mode: when the core fails over to a read-only replica
its writes are rejected with a ``WriteUnavailableError`` (a transient error).
Rather than ack-and-drop those events, the worker leaves them un-acked and
enters a **sleep/poll** loop — pausing ``failover_poll_interval_s`` between
retries of its pending events — until the primary recovers and writes succeed."""
from __future__ import annotations

import logging
import time

from .config import Config
from ._client import WriteUnavailableError
from .emit import EventEmitter

log = logging.getLogger("convert_search_ai.ingest")

_CONVERT_TYPES = {"file.created", "file.updated", "file.restored"}


class Ingestor:
    def __init__(self, config: Config, pipeline, store, source, emitter=None):
        self.config = config
        self.pipeline = pipeline
        self.store = store
        self.source = source
        # Publishes the terminal conversion.complete / conversion.failed event once
        # a conversion resolves (best-effort; never fails ingestion). Lazily built
        # from config when not injected — it opens no Redis connection until it
        # actually publishes, so constructing it here is free.
        self.emitter = emitter or EventEmitter(config)
        self._provisioned: set[str] = set()
        # Read-only (failover) sleep/poll state: while the core rejects writes we
        # retry un-acked events from the pending list after a back-off instead of
        # dropping them. Cleared once a normal (new-message) read succeeds.
        self._degraded = False

    def _sleep(self, seconds: float) -> None:
        """Back-off hook — overridable in tests so they need not really sleep."""
        time.sleep(seconds)

    @property
    def degraded(self) -> bool:
        """True while paused in read-only sleep/poll mode (core is read-only)."""
        return self._degraded

    def _ensure_tenant(self, tenant: str) -> None:
        """Provision the tenant's CSAI schema + tables on its first event
        (idempotent; cached). The core/CSAI create per-tenant storage on demand —
        this is CSAI's equivalent for its own pgvector/FTS tables."""
        if tenant in self._provisioned:
            return
        from .db import provision_tenant
        provision_tenant(self.config, tenant)
        self._provisioned.add(tenant)

    def handle(self, event: dict) -> None:
        if event.get("is_rendition"):
            return  # our own rendition write — never recurse on it
        etype = event.get("type", "")
        uid = event.get("file_uid", "")
        tenant = event.get("tenant") or "default"
        if not uid:
            return
        if etype in _CONVERT_TYPES:
            self._ensure_tenant(tenant)
            outcome = self.pipeline.convert(uid, tenant)
            log.info("convert %s -> %s (%s)", uid, outcome.status, outcome.detail)
            # Once the conversion has resolved (renditions/text durably written, or
            # it cannot be converted), publish exactly one terminal event to the
            # shared core stream. tenant/actor ride the triggering event's envelope.
            # Guarded/best-effort inside emit_conversion — never fails ingestion.
            self.emitter.emit_conversion(event, outcome)
        elif etype == "file.deleted":
            self._ensure_tenant(tenant)
            self.store.delete(tenant, uid)
            log.info("deleted document rows for %s", uid)
        # other types are intentionally not conversion concerns

    def run_once(self, count: int = 32, block_ms: int = 5000) -> int:
        """Read one batch, handle each, ack. Returns the number processed.

        If the core is read-only (``WriteUnavailableError``) the batch is left
        un-acked and the worker enters sleep/poll mode: subsequent calls re-read
        the pending events and retry them after ``failover_poll_interval_s`` until
        the primary recovers."""
        # While degraded, drain our own un-acked (pending) events first so the
        # ones we paused on are retried before consuming anything new.
        if self._degraded:
            batch = self.source.read_pending(count=count)
            if not batch:                       # pending cleared -> back to normal
                self._degraded = False
                batch = self.source.read(count=count, block_ms=block_ms)
        else:
            batch = self.source.read(count=count, block_ms=block_ms)

        processed = 0
        for msg_id, event in batch:
            try:
                self.handle(event)
            except WriteUnavailableError as e:
                # Core is read-only (primary-DB failover). Leave this event (and
                # the rest of the batch) un-acked so they redeliver from the PEL,
                # then sleep/poll until writes succeed again — never dropped.
                self._degraded = True
                log.warning("core is read-only (%s); pausing ingest %.1fs then retrying pending",
                            e, self.config.failover_poll_interval_s)
                self._sleep(self.config.failover_poll_interval_s)
                return processed
            except Exception:  # never let one bad event stall the loop
                log.exception("failed handling event %s", msg_id)
            # Ack only on success/non-transient failure: conversion is idempotent
            # and reconcile backstops genuine misses.
            self.source.ack([msg_id])
            processed += 1
        return processed

    def drain_pending(self, count: int = 32) -> int:
        """Re-handle this consumer's delivered-but-un-acked entries. Returns how
        many were recovered.

        XREADGROUP with ">" returns only entries NEVER delivered to the group, so
        anything in flight when the worker last stopped is invisible to run_once
        and stays pending forever. read_pending existed but only the read-only
        sleep/poll mode used it, so an ordinary restart mid-conversion stranded
        those events permanently: 21 sat in the PEL on the production deployment,
        untouched for five hours, because nothing re-reads a consumer's own PEL
        and the reconcile sweep skips anything already marked converted.

        The consumer name is stable, so reading id "0" hands them back to us."""
        recovered = 0
        while True:
            batch = self.source.read_pending(count=count)
            if not batch:
                return recovered
            for msg_id, event in batch:
                try:
                    self.handle(event)
                except Exception:
                    log.exception("unhandled error replaying pending entry %s", msg_id)
                self.source.ack([msg_id])
                recovered += 1

    def run_forever(self) -> None:
        self.source.ensure_group()
        log.info("ingest worker started; stream=%s group=%s",
                 self.config.events_stream, self.config.events_group)
        # Drain our own PEL FIRST, wrapped so a recovery failure can never stop
        # the worker starting: new events matter more than old ones.
        try:
            recovered = self.drain_pending()
            if recovered:
                log.info("recovered %d event(s) stranded by a previous stop", recovered)
        except Exception:
            log.exception("failed to drain pending entries; continuing")
        while True:
            self.run_once()


def build_ingestor(config: Config) -> Ingestor:
    """Assemble the worker from config: agent gRPC client, store, pipeline, source."""
    from .core_client import agent_client
    from .events import RedisEventSource
    from .indexing import Indexer
    from .pipeline import ConversionPipeline
    from .store import DocumentStore

    mf = agent_client(config)
    store = DocumentStore(config)
    pipeline = ConversionPipeline(mf=mf, store=store, config=config, indexer=Indexer(config))
    source = RedisEventSource(config)
    return Ingestor(config, pipeline, store, source)


def startup_sweep(config: Config) -> None:
    """Run the reconcile sweep once, in the background, at worker startup.

    In a thread, and never fatally: the sweep is recovery, so it must not delay
    the worker's real job or take it down. An exception here would otherwise stop
    a service that was working fine before the sweep was added — trading a
    backlog of unindexed documents for no ingestion at all, which is worse than
    the fault it fixes.

    Deliberately NOT a periodic loop. A reconcile that exits after one pass under
    a restart-always policy reads as a healthy service while it restarts forever,
    and the logs look identical either way; the only tell is the restart count.
    This runs inside the long-lived worker instead, so there is no process whose
    whole life is one sweep."""
    import threading

    def _run() -> None:
        from .reconcile import reconcile, sweep

        cap = getattr(config, "reconcile_max_files", 0) or None
        try:
            log.info("startup sweep: retrying documents that still need conversion")
            log.info("startup sweep: %s", sweep(config, max_files=cap))
        except Exception:
            log.exception("startup sweep failed; the worker continues")
        if getattr(config, "reconcile_full_on_startup", False):
            try:
                log.info("startup sweep: full tree walk")
                log.info("startup walk: %s", reconcile(config, max_files=cap))
            except Exception:
                log.exception("startup tree walk failed; the worker continues")

    threading.Thread(target=_run, name="csai-startup-sweep", daemon=True).start()


def main() -> None:
    from .config import load_dotenv

    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    config = Config()
    ingestor = build_ingestor(config)
    if getattr(config, "reconcile_on_startup", True):
        startup_sweep(config)
    ingestor.run_forever()


if __name__ == "__main__":
    main()
