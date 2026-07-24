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

"""Embedding provider implementations (lazy external imports)."""
from __future__ import annotations

import hashlib
import math
import os
from typing import List

from .base import EmbeddingProvider


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic, offline feature-hashing embeddings — no external calls.

    Not semantically strong, but stable (same text → same vector) and dependency-
    free, so the service and its tests run without an embedding API. Good enough
    for dev and for exercising the pgvector path end to end."""

    def __init__(self, dimension: int = 1024, model_id: str = "hash-local"):
        self.dimension = dimension
        self.model_id = model_id

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> List[float]:
        v = [0.0] * self.dimension
        for tok in (text or "").lower().split():
            d = hashlib.md5(tok.encode("utf-8")).digest()
            idx = int.from_bytes(d[:4], "big") % self.dimension
            v[idx] += 1.0 if (d[4] & 1) else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Voyage AI embeddings (Anthropic's recommended partner). Requires ``voyageai``."""

    def __init__(self, model: str = "voyage-3", dimension: int = 1024, api_key: str | None = None):
        self.model_id = model
        self.dimension = dimension
        self._key = api_key or os.environ.get("VOYAGE_API_KEY")

    def embed(self, texts: List[str]) -> List[List[float]]:
        import voyageai
        client = voyageai.Client(api_key=self._key)
        return client.embed(texts, model=self.model_id, input_type="document").embeddings


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embeddings via any OpenAI-compatible endpoint — OpenAI, **Ollama**, etc. —
    selected by ``base_url``. Requires the ``openai`` SDK.

    ``send_dimensions`` controls whether the OpenAI-only ``dimensions`` param is
    sent (off by default for compatibility; Ollama/local models produce their own
    native dimension, which must match the pgvector column / CSAI_EMBEDDING_DIMENSION)."""

    def __init__(self, model: str = "text-embedding-3-small", dimension: int = 1536,
                 api_key: str | None = None, base_url: str | None = None,
                 send_dimensions: bool = False):
        self.model_id = model
        self.dimension = dimension
        self.base_url = base_url or None
        self.send_dimensions = send_dimensions
        self._key = api_key or os.environ.get("OPENAI_API_KEY") or "not-needed"

    def embed(self, texts: List[str]) -> List[List[float]]:
        from openai import OpenAI
        client = OpenAI(api_key=self._key, base_url=self.base_url)
        kwargs = {"model": self.model_id, "input": texts}
        if self.send_dimensions and self.dimension:
            kwargs["dimensions"] = self.dimension
        resp = client.embeddings.create(**kwargs)
        return [d.embedding for d in resp.data]
