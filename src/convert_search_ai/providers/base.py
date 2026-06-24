"""Embedding + chat provider interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, List, Optional


class EmbeddingProvider(ABC):
    model_id: str = "embedding"
    dimension: int = 1024

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts into vectors of length ``dimension``."""

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


class ChatProvider(ABC):
    model_id: str = "chat"

    @abstractmethod
    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        """Stream a completion as text deltas. ``messages`` are ``{role, content}``
        (user/assistant); ``system`` is the system prompt."""
