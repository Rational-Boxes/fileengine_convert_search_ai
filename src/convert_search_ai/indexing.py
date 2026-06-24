"""Indexing: chunk extracted Markdown, embed the chunks, store them in pgvector.

Runs after conversion (the pipeline calls it when an Indexer is wired). Idempotent
— re-indexing a file replaces its chunks. Embeddings come from the configured
``EmbeddingProvider`` (default: the offline ``hash`` provider)."""
from __future__ import annotations

from typing import Optional

from .chunking import chunk_markdown
from .config import Config


class Indexer:
    def __init__(self, config: Config, *, embedder=None, chunk_store=None):
        self.config = config
        self._embedder = embedder
        self._chunks = chunk_store

    @property
    def embedder(self):
        if self._embedder is None:
            from .providers import make_embedding_provider
            self._embedder = make_embedding_provider(self.config)
        return self._embedder

    @property
    def chunks(self):
        if self._chunks is None:
            from .vectorstore import ChunkStore
            self._chunks = ChunkStore(self.config)
        return self._chunks

    def index(self, tenant: str, file_uid: str, content_md: Optional[str],
              version: Optional[str] = None) -> int:
        """Chunk + embed + store. Returns the number of chunks indexed."""
        chunks = chunk_markdown(content_md or "")
        if not chunks:
            self.chunks.delete(tenant, file_uid)
            return 0
        vectors = self.embedder.embed([c.text for c in chunks])
        items = [(c.ordinal, c.text, v) for c, v in zip(chunks, vectors)]
        self.chunks.replace(tenant, file_uid, items)
        return len(items)

    def remove(self, tenant: str, file_uid: str) -> None:
        self.chunks.delete(tenant, file_uid)
