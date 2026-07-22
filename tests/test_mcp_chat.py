"""ChatService <-> MCP: an MCP tool is offered, consent-gated, and its result flows
back through the agentic loop (offline; provider + MCP client faked)."""
from types import SimpleNamespace

import convert_search_ai.mcp_client as mc
from convert_search_ai.chat import ChatService
from convert_search_ai.config import Config
from convert_search_ai.mcp_client import McpTool, ToolSpec
from convert_search_ai.mcp_store import McpIntegration
from convert_search_ai.providers.base import ChatProvider
from convert_search_ai.vectorstore import RetrievedChunk


class FakeRetriever:
    def retrieve(self, identity, message, k=8):
        return [RetrievedChunk("f0", 0, "chunk", 0.1)]


class McpCallingProvider(ChatProvider):
    """Calls the first offered mcp__ tool once, then answers — exercising run_tools."""
    model_id = "mcp-echo"
    supports_tools = True

    def stream(self, messages, *, system=None):
        yield "noop"

    def run_tools(self, messages, *, system=None, tools=None, execute=None, max_iterations=4):
        self.last_system = system
        mcp = next((t for t in (tools or []) if t["name"].startswith("mcp__")), None)
        if mcp and execute:
            args = {"subject": "Hello"}
            yield {"type": "tool_call", "name": mcp["name"], "args": args}
            result = execute(mcp["name"], args)
            yield {"type": "tool_result", "name": mcp["name"]}
            yield {"type": "text", "text": f"done: {result}"}
        else:
            yield {"type": "text", "text": "no mcp tool"}


class FakeProvider:
    """Resolves one MCP tool for the tenant (a wrapped, consent-gated McpTool)."""
    def __init__(self, config):
        self.config = config

    def tools_for(self, identity):
        integ = McpIntegration(id="i1", name="CRM", slug="crm", description="",
                               transport="streamable-http", endpoint_url="https://e.example/mcp",
                               auth_type="none", auth_header="", has_secret=False, headers={},
                               enabled=True, allowed_tools=None, forward_identity=False)
        spec = ToolSpec("create_ticket", "Create a ticket", {"type": "object"})

        class _Store:
            def decrypted_secret(self, t, i):
                return None
        return [McpTool(self.config, _Store(), integ, spec)]


def _cfg():
    c = Config()
    c.mcp_enabled = True
    return c


def _svc(chat):
    cfg = _cfg()
    return ChatService(cfg, retriever=FakeRetriever(), chat=chat, mcp=FakeProvider(cfg)), cfg


def _id():
    return SimpleNamespace(user="u", tenant="t")


def test_mcp_tool_runs_when_consent_approves(monkeypatch):
    monkeypatch.setattr(mc, "call_tool", lambda **kw: "TICKET-7 created")
    chat = McpCallingProvider()
    svc, _ = _svc(chat)
    events = list(svc.answer(_id(), message="make a ticket", consent=lambda req: True))
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls and calls[0]["name"] == "mcp__crm__create_ticket"
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "TICKET-7 created" in text
    # The MCP usage instruction was injected into the system prompt.
    assert "external systems" in (chat.last_system or "")
    # A successful MCP call leaves a bibliographic note in the turn's citations,
    # alongside document/web sources and with a shared [n] marker.
    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    mcp_cites = [c for c in cites if c.get("kind") == "mcp"]
    assert len(mcp_cites) == 1
    assert mcp_cites[0]["integration"] == "CRM" and mcp_cites[0]["tool"] == "create_ticket"
    assert isinstance(mcp_cites[0]["marker"], int) and mcp_cites[0]["marker"] >= 1


def test_mcp_tool_denied_when_no_consent(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(mc, "call_tool",
                        lambda **kw: called.__setitem__("n", called["n"] + 1) or "x")
    svc, _ = _svc(McpCallingProvider())
    # consent defaults to None (no channel) -> the tool must NOT execute
    events = list(svc.answer(_id(), message="make a ticket"))
    assert called["n"] == 0
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "approval" in text.lower() or "declined" in text.lower()
    # A declined call is not a source — no MCP citation is recorded.
    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    assert not [c for c in cites if c.get("kind") == "mcp"]


def test_no_mcp_tools_when_provider_absent():
    svc = ChatService(_cfg(), retriever=FakeRetriever(), chat=McpCallingProvider(), mcp=None)
    events = list(svc.answer(_id(), message="hi", consent=lambda r: True))
    assert not any(e["type"] == "tool_call" for e in events)
