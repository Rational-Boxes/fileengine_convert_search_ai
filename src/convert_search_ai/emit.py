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

"""Publish terminal conversion-outcome events to the shared core stream.

Once a file's conversion **resolves**, the ingest worker emits exactly one
terminal event onto ``fileengine:events`` (the same stream CSAI *consumes*, see
``events.py`` / ``EVENT_CONTRACT.md``) so downstream consumers that must wait for
content resolution (e.g. ``folder_actions``' automatic sorter and webhooks) can
key off it and never hang on a file that simply cannot be converted:

- ``conversion.complete`` — the extracted text / renditions were durably written;
  ``renditions`` lists the formats actually produced (e.g. ``["text", "pdf"]``).
- ``conversion.failed`` — the conversion produced no renditions, carrying a
  ``reason``: ``unsupported`` (no converter exists for the MIME type — a rendition
  will *never* arrive) or ``error`` (a converter was attempted but produced
  nothing). ``renditions`` is ``[]``.

Publishing is **best-effort / fail-open**: a Redis outage must never fail (or
even perturb) ingestion, so ``publish`` swallows and logs errors and the emit
path is fully guarded — it can never raise into the ingest worker. The envelope
mirrors the core publisher's schema (event_id / type / tenant / file_uid /
version / actor / ts / schema) exactly as ``discussion.events`` does, plus the
``renditions`` list and (for failures) ``reason``. ``redis`` is imported lazily."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import uuid
from typing import List, Optional

from .renditions import parse_rendition_name

log = logging.getLogger("convert_search_ai.emit")

_SCHEMA = 1
_MAXLEN = 100_000

COMPLETE = "conversion.complete"
FAILED = "conversion.failed"

# ConvertOutcome.status values that represent a *fresh* terminal resolution of a
# (file_uid, version). "skipped" (directory / already up-to-date) and "missing"
# (stat/content not found) are NOT fresh resolutions — the former was already
# emitted when the version first resolved, the latter is a transient/not-found
# the reconcile sweep backstops — so neither emits.
_TERMINAL_STATUSES = frozenset({"converted", "indexed", "unsupported"})


def _now_ts() -> str:
    # YYYYMMDD_HHMMSS.mmm — same shape as the core's event timestamps.
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S.%f")[:-3]


def make_conversion_event(etype: str, *, tenant: str, file_uid: str, version: str,
                          actor: str, renditions: List[str],
                          reason: Optional[str] = None) -> dict:
    """Build a terminal conversion event envelope (§4 of the folder_actions spec).

    ``reason`` is included only for ``conversion.failed`` (``unsupported`` |
    ``error``); ``conversion.complete`` omits it."""
    evt = {
        "event_id": uuid.uuid4().hex,
        "type": etype,
        "tenant": tenant or "default",
        "file_uid": file_uid or "",
        "version": version or "",
        "actor": actor or "",
        "ts": _now_ts(),
        "schema": _SCHEMA,
        "renditions": list(renditions or []),
    }
    if reason is not None:
        evt["reason"] = reason
    return evt


def _rendition_fmts(outcome) -> List[str]:
    """The rendition *format* vocabulary actually produced for this outcome —
    ``"text"`` when extracted Markdown exists, plus each recognized rendition-child
    fmt (``pdf`` / ``preview`` / ``thumbnail`` / ``poster`` / ``model`` / …) parsed
    from the written child names. Deduped, order-stable, ``text`` first."""
    fmts: List[str] = []
    seen = set()
    if getattr(outcome, "has_markdown", False):
        fmts.append("text")
        seen.add("text")
    for name in getattr(outcome, "renditions_written", None) or []:
        parsed = parse_rendition_name(name)
        if not parsed:
            continue                       # not one of our recognized renditions
        fmt = parsed[1]
        if fmt not in seen:
            seen.add(fmt)
            fmts.append(fmt)
    return fmts


class EventEmitter:
    """Publishes terminal conversion events to the core ``events_stream``.

    Modelled on ``discussion.events.EventPublisher`` but XADDs to the shared core
    stream (``config.events_stream`` = ``fileengine:events``), NOT a private one —
    these events join the recognized file-activity stream every consumer reads."""

    def __init__(self, config):
        self.config = config
        self.stream = config.events_stream
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis  # lazy
            self._redis = redis.Redis(
                host=self.config.redis_host, port=self.config.redis_port,
                password=self.config.redis_password or None, db=self.config.redis_db,
            )
        return self._redis

    def publish(self, etype: str, *, tenant: str, file_uid: str, version: str,
                actor: str, renditions: List[str],
                reason: Optional[str] = None) -> dict:
        """Build + XADD one terminal event. Best-effort — never raises."""
        evt = make_conversion_event(etype, tenant=tenant, file_uid=file_uid,
                                    version=version, actor=actor,
                                    renditions=renditions, reason=reason)
        try:
            self._client().xadd(self.stream, {"payload": json.dumps(evt)},
                                maxlen=_MAXLEN, approximate=True)
        except Exception:
            log.warning("conversion event publish failed (%s for %s) — continuing",
                        etype, file_uid, exc_info=True)
        return evt

    def emit_conversion(self, event: dict, outcome) -> None:
        """Map a resolved ``ConvertOutcome`` to exactly one terminal event (or
        none). ``tenant`` / ``actor`` come from the triggering event's envelope.
        Fully guarded: emission must never fail ingestion."""
        try:
            self._emit_conversion(event, outcome)
        except Exception:
            log.warning("failed to emit conversion outcome for %s — continuing",
                        getattr(outcome, "file_uid", "?"), exc_info=True)

    def _emit_conversion(self, event: dict, outcome) -> None:
        status = getattr(outcome, "status", "") or ""
        if status not in _TERMINAL_STATUSES:
            return                              # not a fresh terminal resolution
        tenant = event.get("tenant") or "default"
        actor = event.get("actor") or ""
        file_uid = getattr(outcome, "file_uid", "") or event.get("file_uid", "")
        version = getattr(outcome, "version", "") or event.get("version", "")

        if status == "unsupported":
            # No plugin claimed the MIME type — a rendition will never arrive.
            self.publish(FAILED, tenant=tenant, file_uid=file_uid, version=version,
                         actor=actor, renditions=[], reason="unsupported")
            return

        fmts = _rendition_fmts(outcome)
        if fmts:
            self.publish(COMPLETE, tenant=tenant, file_uid=file_uid, version=version,
                         actor=actor, renditions=fmts)
        else:
            # A converter matched the type but produced neither a rendition nor
            # extracted text (it degraded or raised inside the fail-soft registry):
            # the conversion resolved with nothing to show -> terminal failure.
            self.publish(FAILED, tenant=tenant, file_uid=file_uid, version=version,
                         actor=actor, renditions=[], reason="error")
