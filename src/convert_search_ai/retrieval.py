"""Permission-scoped vector retrieval for RAG.

Embed the query, ANN-search the tenant's chunks, then keep only chunks whose
source file the **requesting user** may read (the same PermissionGate as search).
Over-fetch then filter so the permission step can't starve the context."""
from __future__ import annotations

from typing import List, Optional

from .config import Config
from .permissions import PermissionGate
from .vectorstore import ChunkStore, RetrievedChunk


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
        rows = self.chunks.ann_search(identity.tenant, qv, fetch or max(k * 4, k))
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
