"""Conversion pipeline — the heart of M1.

For a source file: fetch its content (as the agent), detect MIME, run the matching
plugin to produce renditions + extracted Markdown, write the renditions back as
hidden children, and record the document's state. Idempotent on
``(file_uid, source_version)`` so re-processing the same version is a no-op."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from . import mime as mimelib
from ._client import NotFoundError
from .plugins.registry import PluginRegistry, default_registry
from .renditions import RenditionWriter

log = logging.getLogger("convert_search_ai.pipeline")


@dataclass
class ConvertOutcome:
    file_uid: str
    status: str                       # converted | unsupported | skipped | missing | error
    renditions_written: List[str]
    has_markdown: bool = False
    detail: str = ""


class ConversionPipeline:
    """Wires the agent gRPC client, plugin registry, rendition writer, and store.

    ``store`` is any object with ``get_status``/``upsert`` (the real one is
    ``store.DocumentStore``; tests inject a fake)."""

    def __init__(self, *, mf, store, registry: Optional[PluginRegistry] = None,
                 writer: Optional[RenditionWriter] = None, config=None, indexer=None):
        self.mf = mf
        self.store = store
        self.registry = registry or default_registry(config)
        self.writer = writer or RenditionWriter(mf)
        self.indexer = indexer  # optional: chunk+embed+store into pgvector (M3)

    def convert(self, file_uid: str, tenant: str, force: bool = False) -> ConvertOutcome:
        """Convert + index a file. ``force`` (the on-demand path) re-runs even when
        the version was already processed — needed for files indexed before a new
        rendition-producing plugin existed (e.g. text → preview), which the
        event-driven worker would otherwise skip as up-to-date."""
        try:
            info = self.mf.stat(file_uid, tenant=tenant)
        except NotFoundError:
            return ConvertOutcome(file_uid, "missing", [], detail="stat failed / not found")
        if info.is_dir:                          # FileInfo.is_dir is a property
            return ConvertOutcome(file_uid, "skipped", [], detail="directory")

        version = info.version or ""

        # Idempotency: same version already converted/indexed -> nothing to do
        # (unless forced — an explicit user (re)generate must run the plugins).
        prior = self.store.get_status(tenant, file_uid)
        already_done = bool(prior and prior.source_version == version
                            and prior.status in ("converted", "indexed"))
        if already_done and not force:
            return ConvertOutcome(file_uid, "skipped", [], detail="up-to-date")

        try:
            blob = self.mf.get(file_uid, tenant=tenant)
        except NotFoundError:
            return ConvertOutcome(file_uid, "missing", [], detail="content not found")
        data = blob.read()
        mime = mimelib.detect(data, info.name)

        self.store.upsert(tenant, file_uid, source_version=version, mime=mime,
                          name=info.name, status="converting")

        result = self.registry.convert(data, mime, info.name)
        if not result.supported:
            self.store.upsert(tenant, file_uid, source_version=version, mime=mime,
                              name=info.name, status="unsupported")
            return ConvertOutcome(file_uid, "unsupported", [], detail=mime)

        written = self.writer.write(file_uid, version, result.renditions, tenant)

        # Now that the current version's renditions exist, drop any left over from
        # superseded versions (all formats) so stale previews don't accumulate or
        # get served for the wrong content.
        pruned = self.writer.prune_old_versions(file_uid, version, tenant)
        if pruned:
            log.info("pruned %d stale rendition(s) from old versions of %s: %s",
                     len(pruned), file_uid, ", ".join(sorted(pruned)))

        # Index for vector retrieval (M3) when wired and there is text to chunk.
        # A force re-render of an already-indexed version writes any missing
        # renditions but does not re-embed unchanged content.
        already_indexed = bool(prior and prior.source_version == version
                               and prior.status == "indexed")
        status = "indexed" if already_indexed else "converted"
        if self.indexer is not None and result.markdown and not already_indexed:
            try:
                self.indexer.index(tenant, file_uid, result.markdown, version)
                status = "indexed"
            except Exception:
                log.exception("indexing failed for %s (left 'converted')", file_uid)

        self.store.upsert(tenant, file_uid, source_version=version, mime=mime,
                          name=info.name, content_md=result.markdown, status=status)
        # On-demand callers want the full current set (so a repeat click still
        # reports the existing renditions); the worker only needs what's new.
        reported = self.writer.names_for_version(file_uid, version, tenant) if force else written
        return ConvertOutcome(file_uid, status, reported,
                              has_markdown=bool(result.markdown), detail=mime)
