"""Reconcile sweep — walk FileEngine and convert anything not up to date.

Events are go-forward only, so this covers the initial corpus, retention gaps,
and anything missed during an outage. Conversion is idempotent (keyed on
``(file_uid, source_version)``), so a sweep re-visiting converted files is cheap.
Runs as the agent identity."""
from __future__ import annotations

import logging
from typing import Dict, Optional

log = logging.getLogger("convert_search_ai.reconcile")


def reconcile_tenant(mf, pipeline, tenant: str, *, max_files: Optional[int] = None) -> Dict[str, int]:
    """Depth-first walk of the tenant's tree, converting each file. Returns counts."""
    from fileengine import ROOT_UID

    from ._client import FileEngineError

    counts = {"files": 0, "converted": 0, "skipped": 0, "unsupported": 0,
              "missing": 0, "error": 0}
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
            try:
                outcome = pipeline.convert(e.uid, tenant)
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
            except Exception:
                counts["error"] += 1
                log.exception("reconcile: convert failed for %s", e.uid)
            if max_files and counts["files"] >= max_files:
                log.info("reconcile: hit max_files=%s", max_files)
                return counts
    return counts


def reconcile(config, tenant: Optional[str] = None, *, max_files: Optional[int] = None) -> Dict[str, int]:
    """Build the agent client + pipeline and reconcile one tenant (default: config's)."""
    from .core_client import agent_client
    from .indexing import Indexer
    from .pipeline import ConversionPipeline
    from .store import DocumentStore

    tenant = tenant or config.tenant
    mf = agent_client(config)
    pipeline = ConversionPipeline(mf=mf, store=DocumentStore(config), config=config,
                                  indexer=Indexer(config))
    result = reconcile_tenant(mf, pipeline, tenant, max_files=max_files)
    log.info("reconcile(%s): %s", tenant, result)
    return result
