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

import logging
from typing import Optional

from .chunking import chunk_markdown
from .config import Config



#: A chunk the embedding model rejects for length. Matched on the message rather
#: than a status code because the providers differ; all of them say this.
log = logging.getLogger("convert_search_ai.indexing")

_MAX_SPLIT_DEPTH = 6
#: Last resort if a chunk is still rejected after repeated halving.
_TRUNCATE_CHARS = 400

_TOO_LONG = ("context length", "input tokens", "maximum context", "too long",
             "max_tokens", "reduce the length")


def _is_length_error(exc: Exception) -> bool:
    m = str(exc).lower()
    return any(p in m for p in _TOO_LONG)


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
        # Ordinals are assigned AFTER fitting: a chunk the model rejects gets
        # split, so the final count can exceed len(chunks) and the original
        # ordinals would collide.
        texts = self._fit_to_model([c.text for c in chunks])
        vectors = self.embedder.embed(texts)
        items = [(i, text, v) for i, (text, v) in enumerate(zip(texts, vectors))]
        self.chunks.replace(tenant, file_uid, items)
        return len(items)


    def _fit_to_model(self, texts):
        """Return chunk texts the embedder will actually accept.

        The chunker bounds chunks by CHARACTERS, which is a proxy for tokens and
        not a reliable one: prose runs ~4 chars/token, but an IFC model's GUIDs
        and long identifiers run closer to 2.3, so a 1200-char chunk arrived as
        513 tokens against a 512-token model — and one rejected chunk fails the
        request for the whole document.

        Rather than guess a smaller character budget (the same mistake with a
        different constant, and it would over-split ordinary prose), a batch the
        model rejects for length is halved until it fits. Self-correcting and
        model-agnostic: no tokenizer, no per-model table, and it keeps working if
        the embedding model is swapped for one with a different limit."""
        return self._fit(list(texts), 0)

    def _fit(self, texts, depth):
        if not texts:
            return []
        try:
            self.embedder.embed(texts)
            return texts
        except Exception as e:
            if not _is_length_error(e):
                raise
        if len(texts) > 1:                      # narrow to the offending chunk
            mid = len(texts) // 2
            return self._fit(texts[:mid], depth) + self._fit(texts[mid:], depth)
        text = texts[0]
        if depth >= _MAX_SPLIT_DEPTH or len(text) < 2:
            log.warning("chunk still rejected after %d split(s); truncating to %d chars",
                        depth, _TRUNCATE_CHARS)
            return [text[:_TRUNCATE_CHARS]]
        mid = len(text) // 2
        log.info("embedder rejected a %d-char chunk; halving and retrying", len(text))
        return self._fit([text[:mid]], depth + 1) + self._fit([text[mid:]], depth + 1)

    def remove(self, tenant: str, file_uid: str) -> None:
        self.chunks.delete(tenant, file_uid)
