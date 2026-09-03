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

#: A request rejected because the input ARRAY held too many chunks, rather than
#: because one chunk was too long. A different limit with the same remedy — send
#: fewer at a time — so it feeds the same halving. DeepInfra phrases it "Value
#: should have at most 1024 items after validation, not 1445"; OpenAI's is
#: "array_above_max_length".
_TOO_MANY = ("at most", "items after validation", "array_above_max_length",
             "too many inputs")


def _is_length_error(exc: Exception) -> bool:
    m = str(exc).lower()
    return any(p in m for p in _TOO_LONG) or _is_batch_error(exc)


def _is_batch_error(exc: Exception) -> bool:
    """Too many items in one request, as opposed to one item being too long.

    Kept separate from ``_TOO_LONG`` because the two are only remedied the same
    way by coincidence: halving a batch fixes a count, and halving a single
    remaining item fixes a length. Splitting a batch that was refused for COUNT
    down to one item and then truncating that item would be silent data loss for
    a request that was never about the item at all — so ``_fit`` stops splitting
    when a batch of one is still refused for count.
    """
    m = str(exc).lower()
    return any(p in m for p in _TOO_MANY)


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

    @property
    def batch_size(self) -> int:
        """Chunks per embeddings request. Read through a property so an Indexer
        built without a Config (the tests do this) still has a usable default."""
        return getattr(getattr(self, "config", None), "embedding_batch_size", 512) or 512

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
        #
        # In BATCHES, because providers cap the number of items in one request as
        # well as the size of each: a document chunked past that cap was refused
        # in its entirety, and since the refusal is not a per-chunk length error
        # the splitting below never engaged. It failed, was left `index_failed`,
        # and every retry reproduced it exactly.
        texts: list[str] = []
        vectors: list = []
        for start in range(0, len(chunks), self.batch_size):
            batch = [c.text for c in chunks[start:start + self.batch_size]]
            fitted, vecs = self._fit(batch, 0)
            texts.extend(fitted)
            vectors.extend(vecs)
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
        return self._fit(list(texts), 0)[0]

    def _fit(self, texts, depth):
        """``(accepted_texts, their_vectors)`` for one batch.

        Returns the vectors from the call that succeeded rather than throwing
        them away and re-embedding: this ran the whole document through the
        provider twice, at twice the cost, latency and rate-limit pressure, for
        an answer it had already been given.
        """
        if not texts:
            return [], []
        try:
            return texts, list(self.embedder.embed(texts))
        except Exception as e:
            if not _is_length_error(e):
                raise
            batch_refusal = _is_batch_error(e)
        if len(texts) > 1:                      # narrow to the offending chunk
            mid = len(texts) // 2
            left_t, left_v = self._fit(texts[:mid], depth)
            right_t, right_v = self._fit(texts[mid:], depth)
            return left_t + right_t, left_v + right_v
        text = texts[0]
        # A single chunk still refused for COUNT is not a chunk that is too long,
        # and halving its text would not help. Splitting on would corrupt it and
        # truncating would discard content over a limit it never breached, so let
        # the caller see the real error instead.
        if batch_refusal:
            raise RuntimeError(
                "embedder refused a single-item request as too many items; "
                "CSAI_EMBEDDING_BATCH_SIZE cannot fix this")
        if depth >= _MAX_SPLIT_DEPTH or len(text) < 2:
            log.warning("chunk still rejected after %d split(s); truncating to %d chars",
                        depth, _TRUNCATE_CHARS)
            truncated = text[:_TRUNCATE_CHARS]
            try:
                return [truncated], list(self.embedder.embed([truncated]))
            except Exception as e:
                if not _is_length_error(e):
                    raise
                # Nothing left to try. Drop this one fragment and index the rest:
                # previously the truncated text was returned unembedded and the
                # caller re-embedded the whole document, so the same rejection
                # came back and took every other chunk down with it. One lost
                # fragment beats an unsearchable document.
                log.warning("dropping a chunk the embedder rejects even at %d chars",
                            _TRUNCATE_CHARS)
                return [], []
        mid = len(text) // 2
        log.info("embedder rejected a %d-char chunk; halving and retrying", len(text))
        left_t, left_v = self._fit([text[:mid]], depth + 1)
        right_t, right_v = self._fit([text[mid:]], depth + 1)
        return left_t + right_t, left_v + right_v

    def remove(self, tenant: str, file_uid: str) -> None:
        self.chunks.delete(tenant, file_uid)
