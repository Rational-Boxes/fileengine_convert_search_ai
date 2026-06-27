"""Embedding + chat + web-search provider interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


@dataclass
class WebSearchResult:
    """One public web result. ``snippet`` is the search engine's excerpt; no page
    content is fetched in P1 (snippets only)."""
    title: str
    url: str
    snippet: str
    published: str = ""


class WebSearchProvider(ABC):
    """Pluggable internet search backend for the chat ``web_search`` tool
    (WEB_SEARCH_TOOL_PLAN §5). Implementations are selected by config; the default
    is DuckDuckGo. Results are public, so unlike document retrieval they need no
    permission gating — but the query is sent to a third party (see §9)."""

    provider_id: str = "websearch"

    @abstractmethod
    def search(self, query: str, *, k: int) -> List[WebSearchResult]:
        """Return up to ``k`` results for ``query`` (fewer if the engine returns
        fewer). Implementations must degrade gracefully (return ``[]``) rather than
        raise on a transient backend error."""
