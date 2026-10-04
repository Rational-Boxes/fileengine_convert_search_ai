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

"""Conversion pipeline — the heart of M1.

For a source file: fetch its content (as the agent), detect MIME, run the matching
plugin to produce renditions + extracted Markdown, write the renditions back as
hidden children, and record the document's state. Idempotent on
``(file_uid, source_version)`` so re-processing the same version is a no-op."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from . import mime as mimelib
from . import tools
from ._client import NotFoundError
from .plugins.registry import PluginRegistry, default_registry
from .renditions import RenditionWriter

#: How much of a streamed source is sniffed to decide whether a path plugin
#: claims it. Only that decision: a source no path plugin claims is read whole
#: and sniffed in full, exactly as before (MS0 — no new behaviour).
SNIFF_BYTES = 1024 * 1024

log = logging.getLogger("convert_search_ai.pipeline")


@dataclass
class ConvertOutcome:
    file_uid: str
    status: str                       # converted | unsupported | skipped | missing | error
    renditions_written: List[str]
    has_markdown: bool = False
    detail: str = ""
    version: str = ""                 # the source version that resolved (for the terminal event)


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

    def _fetch_to(self, file_uid: str, tenant: str, path: str) -> None:
        """Stream the current version's content to ``path`` in chunks.

        Raises NotFoundError (from the client) if the content is gone. Falls back
        to the whole-blob read only for a client with no streaming read — a test
        double, never the real ManagedFiles."""
        get_stream = getattr(self.mf, "get_stream", None)
        with open(path, "wb") as out:
            if get_stream is None:
                out.write(self.mf.get(file_uid, tenant=tenant).read())
                return
            for chunk in get_stream(file_uid, tenant=tenant):
                out.write(chunk)

    def convert(self, file_uid: str, tenant: str, force: bool = False,
                max_bytes: Optional[int] = None) -> ConvertOutcome:
        """Convert + index a file. ``force`` (the on-demand path) re-runs even when
        the version was already processed — needed for files indexed before a new
        rendition-producing plugin existed (e.g. text → preview), which the
        event-driven worker would otherwise skip as up-to-date.

        ``max_bytes`` refuses a file larger than that, before a byte of it is
        read. It exists because the failure mode of a very large document is not
        a failed conversion but a dead worker: the content is read whole into
        memory and the converter may hold several derived copies of it, so the
        process is killed outright and no ``except`` here ever runs. Passed by
        the unattended sweeps; left unset on the on-demand path, where somebody
        is waiting on one specific file."""
        # Erased uids are refused before anything is read, let alone written
        # (PROPOSAL_accountability_record.md §5.4.5). An erasure can land while a
        # conversion is already in flight for the same uid; if that job then
        # completes, the extracted text and embeddings go straight back — AFTER
        # the platform recorded the erasure complete. Cancelling in-flight work
        # is best-effort, so this refusal is what actually closes the race, and
        # it is checked here rather than at the write so nothing is fetched or
        # extracted for a file that must not exist.
        if self.store.is_erased(tenant, file_uid):
            return ConvertOutcome(file_uid, "skipped", [], detail="erased")

        try:
            info = self.mf.stat(file_uid, tenant=tenant)
        except NotFoundError:
            return ConvertOutcome(file_uid, "missing", [], detail="stat failed / not found")
        if info.is_dir:                          # FileInfo.is_dir is a property
            return ConvertOutcome(file_uid, "skipped", [], detail="directory")

        # Before the content is fetched: the read is what kills the process, so a
        # check after it would not be a check at all.
        #
        # Which means the type has to be judged from the NAME. Content sniffing
        # needs the bytes, and needing the bytes is the whole problem — so an
        # extension it is, and a file with no usable extension is treated as
        # unclaimed and therefore limited. That is the safe direction: an unknown
        # type is exactly the case where reading it whole is the risk.
        #
        # Only converters that parse in this process are limited. A video or a
        # BIM model is handled by a child process, or under the converter's own
        # ceiling, and refusing those would strip previews from the files that
        # most need one while buying nothing.
        if max_bytes and info.size > max_bytes:
            by_name = mimelib.detect(b"", info.name)
            if not self.registry.bounds_own_memory(by_name, info.name):
                # The row is deliberately left as it stands — no 'unsupported',
                # no 'error'. Neither is true, and both would be a claim about
                # the FILE when this is a statement about the limit in force
                # today. Left alone, it is picked up by the first sweep that runs
                # with a bigger limit, which is what an operator raising it would
                # expect. The cost is one stat per sweep.
                log.warning("skipping %s (%s, %s): %d bytes exceeds the sweep "
                            "limit of %d and this type is converted in-process",
                            file_uid, info.name, by_name, info.size, max_bytes)
                return ConvertOutcome(file_uid, "skipped", [],
                                      detail=f"too-large: {info.size} > {max_bytes}")

        version = info.version or ""

        # Idempotency: same version already converted/indexed -> nothing to do
        # (unless forced — an explicit user (re)generate must run the plugins).
        prior = self.store.get_status(tenant, file_uid)
        # "index_failed" is deliberately absent: it is the one status that MUST be
        # retried. "converted" stays here because it is the correct terminal state
        # for a file with no text to embed (an image, say) — re-converting those
        # on every sweep would be waste, not recovery.
        already_done = bool(prior and prior.source_version == version
                            and prior.status in ("converted", "indexed"))
        if already_done and not force:
            return ConvertOutcome(file_uid, "skipped", [], detail="up-to-date")

        # STREAMED to a temp file in bounded chunks (MEDIA_SHARE.md §4.2a, MS0),
        # never read whole into this process first. A plugin that works from a
        # path (video) gets the path; anything else is read back from the temp
        # file and converted exactly as before. The temp file is gone on every
        # exit — `workdir` removes it however this block is left.
        with tools.workdir() as wd:
            src = os.path.join(wd, "source")
            try:
                self._fetch_to(file_uid, tenant, src)
            except NotFoundError:
                return ConvertOutcome(file_uid, "missing", [], detail="content not found")

            with open(src, "rb") as f:
                head = f.read(SNIFF_BYTES)
            by_head = mimelib.detect(head, info.name)
            if self.registry.consumes_path(by_head, info.name):
                data, mime = None, by_head
            else:
                with open(src, "rb") as f:
                    data = f.read()
                mime = mimelib.detect(data, info.name)

            self.store.upsert(tenant, file_uid, source_version=version, mime=mime,
                              name=info.name, status="converting")

            converted = (self.registry.convert_path(src, mime, info.name) if data is None
                         else self.registry.convert(data, mime, info.name))

            # `with`: a file-backed rendition owns a temp file that outlives the
            # converter's workdir on purpose (see plugins.base.Rendition). Nothing
            # else deletes it, so every exit from here -- including "unsupported"
            # and any exception below -- has to release them or the disk fills up
            # one conversion at a time.
            with converted as result:
                if not result.supported:
                    self.store.upsert(tenant, file_uid, source_version=version, mime=mime,
                                      name=info.name, status="unsupported")
                    return ConvertOutcome(file_uid, "unsupported", [], detail=mime, version=version)

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
                # "index_failed", not "converted". A document that HAS text but
                # could not be embedded is not in the same state as one that
                # simply has no text to embed, and recording both as "converted"
                # made the failure invisible and permanent: the idempotency guard
                # counts "converted" as already-done, so neither redelivery nor a
                # reconcile sweep ever tried again. Twenty-two documents sat that
                # way, unsearchable, with nothing to show for it but a log line
                # in a container that had since been replaced.
                status = "index_failed"
                log.exception("indexing failed for %s (left '%s'; will be retried)",
                              file_uid, status)

        self.store.upsert(tenant, file_uid, source_version=version, mime=mime,
                          name=info.name, content_md=result.markdown, status=status)
        # On-demand callers want the full current set (so a repeat click still
        # reports the existing renditions); the worker only needs what's new.
        reported = self.writer.names_for_version(file_uid, version, tenant) if force else written
        return ConvertOutcome(file_uid, status, reported,
                              has_markdown=bool(result.markdown), detail=mime,
                              version=version)
