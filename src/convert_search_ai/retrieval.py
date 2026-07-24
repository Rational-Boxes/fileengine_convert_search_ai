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

    def retrieve(self, identity, query: str, *, k: int = 8, fetch: Optional[int] = None) -> List[RetrievedChunk]:
        if not query or not query.strip():
            return []
        qv = self.embedder.embed_query(query)
        try:
            rows = self.chunks.ann_search(identity.tenant, qv, fetch or max(k * 4, k))
        except Exception as e:
            # A vector-store outage shouldn't 500 the chat — degrade to no document
            # context (the answer falls back to general knowledge / web search).
            _log.warning("vector retrieval unavailable; answering without document "
                         "context: %s", e)
            return []
        mf = self._client(identity)
        try:
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
