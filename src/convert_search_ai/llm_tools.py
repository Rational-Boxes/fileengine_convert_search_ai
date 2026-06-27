"""LLM tool layer for chat-with-documents (WEB_SEARCH_TOOL_PLAN §6).

A ``Tool`` is a name + JSON-schema + ``run()``. P1 ships the ``web_search`` tool and
its pluggable backend; the provider tool-calling loop that actually *invokes* tools
lands in P2. ``build_tools(config)`` returns the enabled tools — empty unless
``CSAI_WEB_SEARCH_ENABLED`` is set (web search is OFF by default)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List
from urllib.parse import urlparse

from . import audit, guards
from .config import Config


@dataclass
class ToolContext:
    """Per-answer context handed to a tool: who is asking, plus a ``sources``
    accumulator the chat loop reads back to build citations."""
    identity: object
    config: Config
    sources: List[dict] = field(default_factory=list)  # {kind:"web", url, title}


@dataclass
class ToolOutput:
    """A tool's result: ``text`` is fed back to the model as the tool result;
    ``sources`` are the structured citations this call contributed."""
    text: str
    sources: List[dict] = field(default_factory=list)


class Tool(ABC):
    name: str = "tool"
    description: str = ""
    # JSON schema for the arguments object (provider-agnostic; the P2 loop wraps it
    # into each provider's tool envelope).
    schema: dict = {"type": "object", "properties": {}}

    @abstractmethod
    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        ...


class WebSearchTool(Tool):
    """Search the public internet via the configured ``WebSearchProvider``."""

    name = "web_search"
    description = (
        "Search the public internet for current, external, or general-knowledge "
        "information that is NOT in the user's documents. Returns titled result "
        "snippets with their source URLs. Use only when the provided document "
        "context is insufficient to answer.")
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The web search query."},
        },
        "required": ["query"],
    }

    def __init__(self, provider, *, max_results: int = 5, max_chars: int = 4000,
                 max_query_chars: int = 1000):
        self.provider = provider
        self.max_results = max_results
        self.max_chars = max_chars
        self.max_query_chars = max_query_chars

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        args = args or {}
        try:
            query = guards.check_query(str(args.get("query", "")), self.max_query_chars)
        except guards.GuardError as e:
            return ToolOutput(text=f"(web_search error: {e})")
        k = guards.cap_limit(int(args.get("max_results", self.max_results)), self.max_results)
        results = self.provider.search(query, k=k) or []

        # Audit the SHAPE only — never the query text (audit.py contract). The
        # `web` flag records that a query was sent to a third-party engine.
        audit.record(action="web_search", user=getattr(ctx.identity, "user", ""),
                     tenant=getattr(ctx.identity, "tenant", ""), result="ok",
                     provider=getattr(self.provider, "provider_id", "?"),
                     results=len(results), web=True)

        if not results:
            return ToolOutput(text="No web results found.")

        blocks, added, total = [], [], 0
        for r in results:
            block = self._format(r)
            if blocks and total + len(block) > self.max_chars:
                break
            blocks.append(block)
            total += len(block)
            src = {"kind": "web", "url": r.url, "title": r.title, "snippet": r.snippet}
            added.append(src)
            ctx.sources.append(src)
        return ToolOutput(text="\n\n".join(blocks), sources=added)

    @staticmethod
    def _format(r) -> str:
        domain = urlparse(r.url).netloc or r.url
        head = r.title or domain
        return f"{head} ({domain})\n{r.snippet}\nSource: {r.url}"


def _default_page_fetch(url: str, *, max_bytes: int, timeout: float):
    from .webfetch import fetch_text
    return fetch_text(url, max_bytes=max_bytes, timeout=timeout)


class FetchPageTool(Tool):
    """Fetch and read the full text of a single public web page (SSRF-guarded)."""

    name = "fetch_page"
    description = (
        "Fetch and read the full text of a single public web page by its https URL "
        "(e.g. a URL returned by web_search) when the search snippet is not enough. "
        "Only public https pages can be read.")
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The https URL to fetch."},
        },
        "required": ["url"],
    }

    def __init__(self, *, fetcher=None, max_bytes: int = 2_000_000,
                 timeout_ms: int = 5000, max_chars: int = 4000):
        self._fetch = fetcher or _default_page_fetch
        self.max_bytes = max_bytes
        self.timeout = max(1.0, timeout_ms / 1000.0)
        self.max_chars = max_chars

    def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        url = str((args or {}).get("url", "")).strip()
        if not url:
            return ToolOutput(text="(fetch_page error: url is required)")
        result = self._fetch(url, max_bytes=self.max_bytes, timeout=self.timeout)
        audit.record(action="fetch_page", user=getattr(ctx.identity, "user", ""),
                     tenant=getattr(ctx.identity, "tenant", ""),
                     result="ok" if result else "error", web=True)
        if not result:
            return ToolOutput(
                text=f"Could not read {url} (blocked, non-text, or unavailable).")
        title, text = result
        text = text[:self.max_chars]
        src = {"kind": "web", "url": url, "title": title, "snippet": text}
        ctx.sources.append(src)
        return ToolOutput(text=text, sources=[src])


def build_tools(config: Config) -> List[Tool]:
    """The tools to expose to the model for this deployment. Empty unless web
    search is enabled (OFF by default). fetch_page is added only when page fetch
    is additionally enabled."""
    tools: List[Tool] = []
    if getattr(config, "web_search_enabled", False):
        from .providers import make_web_search_provider
        tools.append(WebSearchTool(
            make_web_search_provider(config),
            max_results=getattr(config, "web_search_results", 5),
            max_chars=getattr(config, "web_max_chars", 4000),
            max_query_chars=getattr(config, "max_query_chars", 1000)))
        if getattr(config, "web_fetch_pages", False):
            tools.append(FetchPageTool(
                max_bytes=getattr(config, "web_fetch_max_bytes", 2_000_000),
                timeout_ms=getattr(config, "web_timeout_ms", 5000),
                max_chars=getattr(config, "web_max_chars", 4000)))
    return tools
