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
