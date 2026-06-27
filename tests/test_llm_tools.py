"""Unit tests for the LLM tool layer — the web_search tool + registry (offline)."""
from types import SimpleNamespace

from convert_search_ai.config import Config
from convert_search_ai.llm_tools import ToolContext, WebSearchTool, build_tools
from convert_search_ai.providers.websearch import (
    FakeWebSearchProvider, NullWebSearchProvider)


def _ctx():
    return ToolContext(identity=SimpleNamespace(user="u", tenant="t"), config=Config())


def test_web_search_tool_returns_text_and_web_sources():
    out = WebSearchTool(FakeWebSearchProvider(), max_results=3).run({"query": "mars"}, _ctx())
    assert "example.com" in out.text
    assert len(out.sources) == 3
    assert all(s["kind"] == "web" and s["url"] and "title" in s for s in out.sources)


def test_sources_accumulate_into_context():
    ctx = _ctx()
    out = WebSearchTool(FakeWebSearchProvider(), max_results=2).run({"query": "x"}, ctx)
    assert ctx.sources == out.sources and len(ctx.sources) == 2


def test_empty_query_is_a_graceful_error_not_a_raise():
    out = WebSearchTool(FakeWebSearchProvider()).run({"query": "   "}, _ctx())
    assert "error" in out.text.lower() and out.sources == []


def test_results_count_is_capped():
    out = WebSearchTool(FakeWebSearchProvider(), max_results=2).run(
        {"query": "x", "max_results": 50}, _ctx())
    assert len(out.sources) == 2


def test_char_budget_keeps_first_then_stops():
    out = WebSearchTool(FakeWebSearchProvider(), max_results=5, max_chars=1).run(
        {"query": "x"}, _ctx())
    assert len(out.sources) == 1  # first result is always kept


def test_no_results_message():
    out = WebSearchTool(NullWebSearchProvider()).run({"query": "x"}, _ctx())
    assert out.sources == [] and "no web results" in out.text.lower()


def test_build_tools_off_by_default():
    c = Config()
    assert c.web_search_enabled is False
    assert build_tools(c) == []


def test_build_tools_includes_web_search_when_enabled():
    c = Config()
    c.web_search_enabled = True
    c.web_search_provider = "fake"
    tools = build_tools(c)
    assert len(tools) == 1 and tools[0].name == "web_search"
    assert tools[0].schema["required"] == ["query"]
