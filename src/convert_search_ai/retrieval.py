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

"""Permission-scoped vector retrieval for RAG.

Embed the query, ANN-search the tenant's chunks, then keep only chunks whose
source file the **requesting user** may read (the same PermissionGate as search).
Over-fetch then filter so the permission step can't starve the context."""
from __future__ import annotations

import logging
from typing import List, Optional

from .config import Config
from .permissions import PermissionGate
from .vectorstore import ChunkStore, RetrievedChunk

_log = logging.getLogger("convert_search_ai.retrieval")


class Retriever:
    def __init__(self, config: Config, *, embedder=None, chunk_store=None,
                 gate: Optional[PermissionGate] = None, client_factory=None):
        self.config = config
        self._embedder = embedder
        self.chunks = chunk_store or ChunkStore(config)
        self.gate = gate or PermissionGate(config.permission_cache_ttl)
        self._client_factory = client_factory

    @property
    def embedder(self):
        if self._embedder is None:
            from .providers import make_embedding_provider
            self._embedder = make_embedding_provider(self.config)
        return self._embedder

    def _client(self, identity):
        if self._client_factory:
            return self._client_factory(identity)
        from .core_client import client_for
        return client_for(identity, self.config)

    def retrieve(self, identity, query: str, *, k: int = 8, fetch: Optional[int] = None,
                 scope_folder_uids: Optional[List[str]] = None) -> List[RetrievedChunk]:
        if not query or not query.strip():
            return []
        scoped = bool(scope_folder_uids)
        qv = self.embedder.embed_query(query)
        mf = self._client(identity)
        try:
            file_uids: Optional[set] = None
            if scoped:
                # The conversation is scoped to chosen folders: expand them (and their
                # subfolders) to a document set, walked as the caller so it's ACL-safe.
                file_uids = self._resolve_scope_file_uids(mf, identity.tenant, scope_folder_uids)
                if not file_uids:
                    # A scope was chosen but nothing readable is in it → no doc context.
                    return []
            # Over-fetch more when scoped so the post-ANN permission filter still fills k.
            want = fetch or max(k * (8 if scoped else 4), k)
            try:
                rows = self.chunks.ann_search(identity.tenant, qv, want, file_uids=file_uids)
            except Exception as e:
                # A vector-store outage shouldn't 500 the chat — degrade to no document
                # context (the answer falls back to general knowledge / web search).
                _log.warning("vector retrieval unavailable; answering without document "
                             "context: %s", e)
                return []
            out: List[RetrievedChunk] = []
            for r in rows:
                if self.gate.can_read(mf, identity, r.file_uid):
                    out.append(r)
                    if len(out) >= k:
                        break
            return out
        finally:
            try:
                mf.close()
            except Exception:
                pass

    def _resolve_scope_file_uids(self, mf, tenant: str, folder_uids: List[str],
                                 *, max_folders: int = 2000) -> set:
        """Expand the selected folder UIDs to the set of document (file) UIDs they
        contain, recursively including subfolders. Walked one level at a time via
        ``mf.dir()`` as the caller — so folders/files the user can't read are simply
        absent. Bounded by ``max_folders`` to cap the traversal for very large trees."""
        files: set = set()
        seen: set = set()
        stack = [u for u in (folder_uids or []) if u]
        while stack and len(seen) < max_folders:
            fuid = stack.pop()
            if fuid in seen:
                continue
            seen.add(fuid)
            try:
                entries = mf.dir(fuid, tenant=tenant) or []
            except Exception:
                continue  # unreadable/missing folder → skip (fail-safe)
            for e in entries:
                if getattr(e, "is_container", False):
                    if e.uid not in seen:
                        stack.append(e.uid)
                elif getattr(e, "uid", None):
                    files.add(e.uid)
        return files
