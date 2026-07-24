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
    # Whether this provider can drive the tool-calling loop (``run_tools``). When
    # False, the chat service uses ``stream`` and never offers tools.
    supports_tools: bool = False

    @abstractmethod
    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        """Stream a completion as text deltas. ``messages`` are ``{role, content}``
        (user/assistant); ``system`` is the system prompt."""

    def run_tools(self, messages: List[dict], *, system: Optional[str] = None,
                  tools: Optional[List[dict]] = None, execute=None,
                  max_iterations: int = 4) -> Iterator[dict]:
        """Drive one agentic answer, optionally calling tools (WEB_SEARCH_TOOL_PLAN
        §4). Yields event dicts: ``{"type":"text","text":…}`` deltas,
        ``{"type":"tool_call","name","args"}``, and ``{"type":"tool_result","name"}``.

        ``tools`` are provider-agnostic specs ``{name, description, schema}``;
        ``execute(name, args) -> str`` runs a tool and returns the result text the
        model sees. The default (for providers without native tool support) ignores
        tools and just streams text."""
        for delta in self.stream(messages, system=system):
            yield {"type": "text", "text": delta}


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
