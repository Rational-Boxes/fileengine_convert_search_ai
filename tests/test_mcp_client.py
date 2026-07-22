"""MCP tool wrapping, consent gating, discovery caching + fail-open (no network)."""
from types import SimpleNamespace

import convert_search_ai.mcp_client as mc
from convert_search_ai.config import Config
from convert_search_ai.llm_tools import ToolContext
from convert_search_ai.mcp_client import McpTool, McpToolProvider, ToolSpec
from convert_search_ai.mcp_store import McpIntegration


def _cfg(**over):
    c = Config()
    c.mcp_enabled = True
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _integ(**over):
    base = dict(id="i1", name="CRM", slug="crm", description="", transport="streamable-http",
                endpoint_url="https://mcp.example.com/mcp", auth_type="none", auth_header="",
                has_secret=False, headers={}, enabled=True, allowed_tools=None,
                forward_identity=False)
    base.update(over)
    return McpIntegration(**base)


class FakeStore:
    def __init__(self, integrations=None, secret=None):
        self._integrations = integrations or []
        self._secret = secret

    def list(self, tenant, *, enabled_only=False):
        return [i for i in self._integrations if (i.enabled or not enabled_only)]

    def decrypted_secret(self, tenant, id_):
        return self._secret


def _ctx(consent=None):
    return ToolContext(identity=SimpleNamespace(user="u", tenant="t"), config=_cfg(),
                       consent=consent)


def _tool(monkeypatch, *, returns="the result", integ=None):
    integ = integ or _integ()
    spec = ToolSpec(name="create_ticket", description="Create a ticket",
                    input_schema={"type": "object", "properties": {"subject": {"type": "string"}}})
    monkeypatch.setattr(mc, "call_tool", lambda **kw: returns)
    return McpTool(_cfg(), FakeStore(), integ, spec), spec


def test_namespacing_and_schema_copy(monkeypatch):
    tool, spec = _tool(monkeypatch)
    assert tool.name == "mcp__crm__create_ticket"
    assert tool.schema == spec.input_schema


def test_denied_consent_does_not_call(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(mc, "call_tool", lambda **kw: called.__setitem__("n", called["n"] + 1) or "x")
    integ = _integ()
    spec = ToolSpec(name="create_ticket", description="", input_schema={})
    tool = McpTool(_cfg(), FakeStore(), integ, spec)
    out = tool.run({"subject": "Hi"}, _ctx(consent=lambda req: False))
    assert "declined" in out.text and called["n"] == 0


def test_missing_consent_channel_denies(monkeypatch):
    tool, _ = _tool(monkeypatch)
    out = tool.run({"subject": "Hi"}, _ctx(consent=None))
    assert "approval" in out.text.lower()


def test_approved_consent_calls_and_passes_request(monkeypatch):
    seen = {}
    tool, _ = _tool(monkeypatch, returns="TICKET-42 created")

    def consent(req):
        seen["req"] = req
        return True

    out = tool.run({"subject": "Hi"}, _ctx(consent=consent))
    assert out.text == "TICKET-42 created"
    assert seen["req"].tool_full == "mcp__crm__create_ticket"
    assert "subject=Hi" in seen["req"].args_summary


def test_output_truncation(monkeypatch):
    tool, _ = _tool(monkeypatch, returns="x" * 5000)
    tool._config.mcp_max_tool_output_chars = 100
    out = tool.run({}, _ctx(consent=lambda r: True))
    assert out.text.endswith("…(truncated)") and len(out.text) < 200


def test_connection_error_is_swallowed(monkeypatch):
    integ = _integ()
    spec = ToolSpec(name="t", description="", input_schema={})
    tool = McpTool(_cfg(), FakeStore(), integ, spec)

    def boom(**kw):
        raise mc.McpConnectionError("down")
    monkeypatch.setattr(mc, "call_tool", boom)
    out = tool.run({}, _ctx(consent=lambda r: True))
    assert "could not be reached" in out.text  # fail-safe, no raise


def test_bearer_auth_header_built(monkeypatch):
    integ = _integ(auth_type="bearer", has_secret=True)
    headers = mc._build_headers(_cfg(), FakeStore(secret="tok123"), integ,
                                SimpleNamespace(user="u", tenant="t"))
    assert headers["Authorization"] == "Bearer tok123"


def test_oauth_client_credentials_header(monkeypatch):
    # auth_type=oauth → CSAI exchanges the client secret for a bearer token (cached)
    # and presents it to the MCP server.
    from convert_search_ai import mcp_oauth
    monkeypatch.setattr(mcp_oauth, "get_access_token", lambda integ, secret, **kw: "access-XYZ")
    integ = _integ(auth_type="oauth", has_secret=True,
                   token_url="https://auth.example.com/token", oauth_client_id="c1",
                   oauth_scope="mcp.read")
    headers = mc._build_headers(_cfg(), FakeStore(secret="client-secret"), integ,
                                SimpleNamespace(user="u", tenant="t"))
    assert headers["Authorization"] == "Bearer access-XYZ"


def test_oauth_token_failure_omits_header(monkeypatch):
    from convert_search_ai import mcp_oauth

    def boom(integ, secret, **kw):
        raise mcp_oauth.McpOAuthError("token endpoint down")
    monkeypatch.setattr(mcp_oauth, "get_access_token", boom)
    integ = _integ(auth_type="oauth", has_secret=True,
                   token_url="https://auth.example.com/token", oauth_client_id="c1")
    headers = mc._build_headers(_cfg(), FakeStore(secret="s"), integ,
                                SimpleNamespace(user="u", tenant="t"))
    assert "Authorization" not in headers  # fail-open: no token → no header


def test_forward_identity_opt_in(monkeypatch):
    off = mc._build_headers(_cfg(mcp_identity_secret="k"), FakeStore(), _integ(),
                            SimpleNamespace(user="alice", tenant="acme"))
    assert "X-Fileengine-User" not in off  # default: no identity forwarded
    on = mc._build_headers(_cfg(mcp_identity_secret="k"), FakeStore(),
                           _integ(forward_identity=True),
                           SimpleNamespace(user="alice", tenant="acme"))
    assert on["X-Fileengine-User"] == "alice" and "X-Fileengine-User-Assertion" in on


# ------------------------------ provider -----------------------------------
def test_provider_discovers_wraps_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_discover(**kw):
        calls["n"] += 1
        return [ToolSpec("a", "", {}), ToolSpec("b", "", {})]
    monkeypatch.setattr(mc, "discover_tools", fake_discover)

    prov = McpToolProvider(_cfg(), FakeStore([_integ()]))
    ident = SimpleNamespace(user="u", tenant="t")
    tools = prov.tools_for(ident)
    assert {t.name for t in tools} == {"mcp__crm__a", "mcp__crm__b"}
    prov.tools_for(ident)  # cached (same config-version) -> no second discovery
    assert calls["n"] == 1
    prov.invalidate("t")
    prov.tools_for(ident)
    assert calls["n"] == 2


def test_provider_allowed_tools_filter(monkeypatch):
    monkeypatch.setattr(mc, "discover_tools",
                        lambda **kw: [ToolSpec("a", "", {}), ToolSpec("b", "", {})])
    prov = McpToolProvider(_cfg(), FakeStore([_integ(allowed_tools=["a"])]))
    tools = prov.tools_for(SimpleNamespace(user="u", tenant="t"))
    assert {t.name for t in tools} == {"mcp__crm__a"}


def test_provider_fails_open_on_bad_server(monkeypatch):
    def boom(**kw):
        raise mc.McpConnectionError("unreachable")
    monkeypatch.setattr(mc, "discover_tools", boom)
    prov = McpToolProvider(_cfg(), FakeStore([_integ()]))
    assert prov.tools_for(SimpleNamespace(user="u", tenant="t")) == []  # no raise


def test_provider_off_when_disabled(monkeypatch):
    prov = McpToolProvider(_cfg(mcp_enabled=False), FakeStore([_integ()]))
    assert prov.tools_for(SimpleNamespace(user="u", tenant="t")) == []


# ------------------------------ role gating --------------------------------
def test_role_permitted_helper():
    ident = SimpleNamespace(user="u", tenant="t", roles=["users", "engineering"])
    assert mc.role_permitted(_integ(allowed_roles=None), ident) is True      # unset = all
    assert mc.role_permitted(_integ(allowed_roles=[]), ident) is True        # empty = all
    assert mc.role_permitted(_integ(allowed_roles=["engineering"]), ident) is True
    assert mc.role_permitted(_integ(allowed_roles=["finance"]), ident) is False
    # a user with no roles is barred from a restricted integration
    assert mc.role_permitted(_integ(allowed_roles=["x"]), SimpleNamespace(roles=[])) is False


def test_provider_filters_by_role(monkeypatch):
    monkeypatch.setattr(mc, "discover_tools", lambda **kw: [ToolSpec("t", "", {})])
    prov = McpToolProvider(_cfg(), FakeStore([_integ(allowed_roles=["finance"])]))
    eng = SimpleNamespace(user="e", tenant="t", roles=["engineering"])
    fin = SimpleNamespace(user="f", tenant="t", roles=["finance"])
    assert prov.tools_for(eng) == []                              # no matching role → hidden
    assert {t.name for t in prov.tools_for(fin)} == {"mcp__crm__t"}  # matching role → visible


def test_tool_run_denies_barred_role(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(mc, "call_tool", lambda **kw: called.__setitem__("n", called["n"] + 1) or "x")
    integ = _integ(allowed_roles=["finance"])
    tool = McpTool(_cfg(), FakeStore(), integ, ToolSpec("t", "", {}))
    ctx = ToolContext(identity=SimpleNamespace(user="u", tenant="t", roles=["engineering"]),
                      config=_cfg(), consent=lambda r: True)
    out = tool.run({}, ctx)
    assert "not permitted" in out.text and called["n"] == 0      # never reaches the network
