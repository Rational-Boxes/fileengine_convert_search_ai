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

"""Reconcile sweep — walk FileEngine and convert anything not up to date.

Events are go-forward only, so this covers the initial corpus, retention gaps,
and anything missed during an outage. Conversion is idempotent (keyed on
``(file_uid, source_version)``), so a sweep re-visiting converted files is cheap.
Runs as the agent identity."""
from __future__ import annotations

import logging
from typing import Dict, Optional

log = logging.getLogger("convert_search_ai.reconcile")


#: Statuses that mean "this run did not finish", so the document must be retried.
#:
#: 'converting' is here because it is what a crashed or errored run leaves behind:
#: the pipeline writes it before doing the work and overwrites it after, so a row
#: still holding it is a run that never came back. 'index_failed' is the explicit
#: version of the same thing. 'pending' is a row created but never processed, and
#: 'error' is a recorded failure.
#:
#: 'converted' and 'indexed' are NOT here — they are terminal, and are re-judged
#: on plugin coverage instead (see :func:`needs_conversion`).
RETRY_STATUSES = ("pending", "converting", "index_failed", "error")


def needs_conversion(row, registry) -> Optional[str]:
    """Why ``row`` should be re-converted, or ``None`` to leave it alone.

    Three independent reasons; the last two are what a status-only sweep misses:

    1. **The last run did not finish** — see :data:`RETRY_STATUSES`.

    2. **It was recorded 'unsupported' and the registry now has a converter.**
       Plugin coverage GROWS. 'unsupported' is not a property of the file, it is a
       verdict the registry returned on the day it was asked — so a file recorded
       before its converter shipped stays invisible forever unless something
       re-asks. Install a plugin, restart the service, and this backfills every
       file that plugin now understands.

       Tested with ``supports`` and NOT ``extracts_text``, deliberately. A new
       image or video format is new support that yields thumbnails and previews
       and no text whatsoever; asking "does this extract text" would skip exactly
       the files a new image plugin was installed to handle.

    3. **A text extractor claims this type, but the document has no chunks.**
       Extraction was supposed to produce something and did not.

    The chunk count is what keeps reason 3 honest. A JPEG and a failed PDF both
    sit at 'converted' with nothing in the index; ``extracts_text`` says only the
    PDF was ever supposed to produce any, so images and video are not dragged
    through a pointless re-convert on every sweep — they are caught by reason 2,
    once, when their converter actually arrives."""
    if row.status in RETRY_STATUSES:
        return row.status
    if row.status == "unsupported" and registry.supports(row.mime):
        return "unsupported/now-supported"
    if row.chunks == 0 and registry.extracts_text(row.mime):
        # 'indexed' with zero chunks is a real case too: a document whose text
        # extracted to an empty string. Cheap to retry, and the alternative is
        # trusting a status that the chunk count contradicts.
        return f"{row.status}/no-chunks"
    return None


def sweep_tenant(store, registry, pipeline, tenant: str, *,
                 max_files: Optional[int] = None,
                 max_bytes: Optional[int] = None) -> Dict[str, int]:
    """Retry every document this tenant has recorded that still needs conversion.

    Driven from the documents table, not from a tree walk: the rows already name
    every file the service has ever seen, so finding the broken ones is one query
    instead of a full recursive listing of the corpus. That makes it cheap enough
    to run unattended at startup, which a tree walk is not.

    It is therefore NOT a replacement for :func:`reconcile_tenant` — it can only
    see files that reached the table at least once. A file whose creation event
    was missed entirely has no row and needs the walk to find it.

    ``max_bytes`` bounds what this unattended pass will attempt. Without it one
    document too large to convert stops the sweep permanently rather than
    failing: the process dies mid-conversion, the row is left at 'converting',
    and 'converting' is the first thing this sweep retries on the next start —
    so it dies in the same place every time and nothing behind it is ever
    reached."""
    counts: Dict[str, int] = {"examined": 0, "retried": 0, "skipped": 0}
    reasons: Dict[str, int] = {}
    for row in store.list_documents(tenant):
        counts["examined"] += 1
        reason = needs_conversion(row, registry)
        if reason is None:
            counts["skipped"] += 1
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        try:
            # force=True: a row sitting at 'converted' is inside the pipeline's
            # idempotency guard, and that guard is exactly what has been hiding
            # these. Reason 2 only ever selects rows the guard would skip.
            outcome = pipeline.convert(row.file_uid, tenant, force=True,
                                       max_bytes=max_bytes)
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            # Counted apart from the ordinary 'skipped' (up-to-date, erased, a
            # directory). Those mean "nothing to do"; this one means "there is
            # work here and this pass refused it", which is the number an
            # operator needs to decide whether the limit is set right.
            if outcome.detail.startswith("too-large"):
                counts["too_large"] = counts.get("too_large", 0) + 1
            counts["retried"] += 1
            log.info("sweep: %s (%s, %s) -> %s", row.file_uid, row.name, reason, outcome.status)
        except Exception:
            counts["error"] = counts.get("error", 0) + 1
            log.exception("sweep: convert failed for %s (%s)", row.file_uid, row.name)
        if max_files and counts["retried"] >= max_files:
            log.info("sweep: hit max_files=%s", max_files)
            break
    if reasons:
        log.info("sweep(%s) reasons: %s", tenant, reasons)
    return counts


def sweep(config, tenant: Optional[str] = None, *,
          max_files: Optional[int] = None,
          max_bytes: Optional[int] = None) -> Dict[str, int]:
    """Build the agent client + pipeline and sweep one tenant (default: config's).

    ``max_bytes`` defaults to the configured ``reconcile_max_bytes`` — every
    caller of this function is an unattended pass, so the limit has to be the
    default rather than something each one remembers to ask for."""
    from .core_client import agent_client
    from .indexing import Indexer
    from .pipeline import ConversionPipeline
    from .store import DocumentStore

    tenant = tenant or config.tenant
    if max_bytes is None:
        max_bytes = getattr(config, "reconcile_max_bytes", 0) or None
    store = DocumentStore(config)
    pipeline = ConversionPipeline(mf=agent_client(config), store=store, config=config,
                                  indexer=Indexer(config))
    result = sweep_tenant(store, pipeline.registry, pipeline, tenant,
                          max_files=max_files, max_bytes=max_bytes)
    log.info("sweep(%s, max_bytes=%s): %s", tenant, max_bytes, result)
    return result


def reconcile_tenant(mf, pipeline, tenant: str, *, max_files: Optional[int] = None,
                     max_bytes: Optional[int] = None) -> Dict[str, int]:
    """Depth-first walk of the tenant's tree, converting each file. Returns counts.

    ``max_bytes`` is the same guard as in :func:`sweep_tenant`, and matters more
    here: the walk visits the whole corpus, so it meets every oversized file
    there is rather than only the ones already recorded."""
    from fileengine import ROOT_UID

    from . import mime as mimelib
    from ._client import FileEngineError

    counts = {"files": 0, "converted": 0, "skipped": 0, "unsupported": 0,
              "missing": 0, "error": 0, "too_large": 0}
    stack = [ROOT_UID]
    seen = set()

    while stack:
        uid = stack.pop()
        if uid in seen:
            continue
        seen.add(uid)

        # A directory may vanish or be inaccessible between listing and visiting
        # during a live walk — skip it rather than abort the whole sweep.
        try:
            entries = mf.dir(uid, tenant=tenant)
        except FileEngineError:
            log.debug("reconcile: could not list %s; skipping", uid)
            continue
        if not entries:  # empty directory
            continue
        for e in entries:
            if e.is_container:                   # DirectoryEntry.is_container is a property
                stack.append(e.uid)
                continue
            counts["files"] += 1
            # The listing already carries the size, so an oversized file costs
            # nothing here — not even the stat the pipeline would do to find out.
            # Same rule as the pipeline's: only types parsed in-process are
            # limited, judged from the name because the bytes are the risk.
            if max_bytes and getattr(e, "size", 0) > max_bytes \
                    and not pipeline.registry.bounds_own_memory(mimelib.detect(b"", e.name)):
                counts["too_large"] += 1
                log.warning("reconcile: skipping %s (%s): %d bytes exceeds the limit of %d",
                            e.uid, e.name, e.size, max_bytes)
                continue
            try:
                outcome = pipeline.convert(e.uid, tenant, max_bytes=max_bytes)
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
            except Exception:
                counts["error"] += 1
                log.exception("reconcile: convert failed for %s", e.uid)
            if max_files and counts["files"] >= max_files:
                log.info("reconcile: hit max_files=%s", max_files)
                return counts
    return counts


def reconcile(config, tenant: Optional[str] = None, *, max_files: Optional[int] = None,
              max_bytes: Optional[int] = None) -> Dict[str, int]:
    """Build the agent client + pipeline and reconcile one tenant (default: config's).

    As with :func:`sweep`, ``max_bytes`` defaults to the configured
    ``reconcile_max_bytes``."""
    from .core_client import agent_client
    from .indexing import Indexer
    from .pipeline import ConversionPipeline
    from .store import DocumentStore

    tenant = tenant or config.tenant
    if max_bytes is None:
        max_bytes = getattr(config, "reconcile_max_bytes", 0) or None
    mf = agent_client(config)
    pipeline = ConversionPipeline(mf=mf, store=DocumentStore(config), config=config,
                                  indexer=Indexer(config))
    result = reconcile_tenant(mf, pipeline, tenant, max_files=max_files,
                              max_bytes=max_bytes)
    log.info("reconcile(%s, max_bytes=%s): %s", tenant, max_bytes, result)
    return result
