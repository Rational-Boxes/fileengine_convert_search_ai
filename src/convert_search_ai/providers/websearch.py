"""Web-search provider implementations (lazy external imports).

The chat ``web_search`` tool (WEB_SEARCH_TOOL_PLAN) calls one of these to fetch
public internet results. DuckDuckGo is the default (no API key); an offline
``fake`` keeps unit tests dependency-free, and ``null`` disables search."""
from __future__ import annotations

import logging
from typing import List

from .base import WebSearchProvider, WebSearchResult

_log = logging.getLogger("convert_search_ai.websearch")


class DuckDuckGoSearchProvider(WebSearchProvider):
    """DuckDuckGo via the ``ddgs`` library (the maintained DuckDuckGo client).

    No API key required. ``ddgs`` is an optional dependency, imported lazily so the
    package installs and tests run without it (mirrors the anthropic/openai
    providers). Backend errors degrade to ``[]`` rather than failing the answer."""

    provider_id = "duckduckgo"

    def __init__(self, *, region: str = "wt-wt", safesearch: str = "moderate",
                 timelimit: str = "", timeout_ms: int = 4000):
        self.region = region or "wt-wt"
        self.safesearch = safesearch or "moderate"
        # ddgs expects None (not "") for "no time limit".
        self.timelimit = timelimit or None
        self.timeout = max(1.0, timeout_ms / 1000.0)

    def search(self, query: str, *, k: int) -> List[WebSearchResult]:
        try:
            from ddgs import DDGS
        except ImportError as e:  # pragma: no cover - exercised via the disabled path
            _log.warning("ddgs not installed; web_search returns no results (%s)", e)
            return []
        try:
            with DDGS(timeout=self.timeout) as ddgs:
                rows = ddgs.text(
                    query, region=self.region, safesearch=self.safesearch,
                    timelimit=self.timelimit, max_results=max(1, int(k)),
                ) or []
        except Exception as e:  # network/parse/rate-limit — never crash the answer
            _log.warning("web search failed: %s", e)
            return []
        out: List[WebSearchResult] = []
        for r in rows:
            url = r.get("href") or r.get("url") or ""
            if not url:
                continue
            out.append(WebSearchResult(
                title=(r.get("title") or "").strip(),
                url=url.strip(),
                snippet=(r.get("body") or r.get("snippet") or "").strip(),
            ))
            if len(out) >= k:
                break
        return out


class FakeWebSearchProvider(WebSearchProvider):
    """Deterministic offline results derived from the query — for dev/tests, no
    network. Returns exactly ``k`` synthetic results."""

    provider_id = "fake"

    def search(self, query: str, *, k: int) -> List[WebSearchResult]:
        q = (query or "").strip()
        return [
            WebSearchResult(
                title=f"Result {i + 1} for {q}",
                url=f"https://example.com/{i + 1}?q={q.replace(' ', '+')}",
                snippet=f"Synthetic snippet {i + 1} about {q}.",
            )
            for i in range(max(0, int(k)))
        ]


class NullWebSearchProvider(WebSearchProvider):
    """Disabled backend — always returns no results."""

    provider_id = "null"

    def search(self, query: str, *, k: int) -> List[WebSearchResult]:
        return []
