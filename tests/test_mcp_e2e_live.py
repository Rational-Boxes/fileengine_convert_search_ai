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

"""Opt-in end-to-end MCP integration test against a REAL remote MCP server.

Drives the whole tenant-managed MCP feature (MCP_INTEGRATIONS.md) in-process via
``build_app`` + FastAPI ``TestClient`` — the same style as ``test_e2e_live`` — but
reaching a **real external MCP server** for discovery and tool calls:

  admin API: dry-run test → create (enabled) → list        (real network discovery)
  chat WS  : model calls the tool → consent prompt → approve → tool runs, result
             flows into the answer; and the DENY path (tool is NOT executed).

The chat LLM is replaced by a deterministic tool-calling provider so the test
asserts OUR code paths (discovery, wrapping, consent broker, real tool call, audit)
rather than a model's non-deterministic choice to call a tool. Everything else is
real: the per-tenant Postgres store, the MCP client/transport, the network call to
the reference server, the consent WebSocket flow, and the audit log.

Opt-in (never runs in the default suite — it reaches a third-party server):
  CSAI_MCP_E2E=1                 enable this module
  CSAI_MCP_E2E_URL=<https url>   reference MCP server (default: Hugging Face's public
                                 MCP endpoint — no auth, exposes a no-arg `hf_whoami`)
Also requires Postgres (CSAI_PG_*) with pgvector + pg_trgm. Run:
  CSAI_MCP_E2E=1 pytest tests/test_mcp_e2e_live.py -q
"""
import json
import os
import tempfile
import uuid

import pytest

from convert_search_ai.providers.base import ChatProvider

_REF_URL = os.environ.get("CSAI_MCP_E2E_URL", "https://huggingface.co/mcp")
_REF_TRANSPORT = os.environ.get("CSAI_MCP_E2E_TRANSPORT", "streamable-http")


def _opt_in() -> bool:
    return os.environ.get("CSAI_MCP_E2E", "").strip().lower() in ("1", "true", "yes", "on")


def _skip_reason() -> str:
    if not _opt_in():
        return ("opt-in only — set CSAI_MCP_E2E=1 to run (reaches a real external MCP "
                "server; needs Postgres with pgvector/pg_trgm)")
    from convert_search_ai.config import Config
    try:
        from convert_search_ai import db
        db.connect(Config()).close()
    except Exception as e:  # local, cheap — skip fast if no DB
        return f"Postgres unavailable: {e.__class__.__name__}"
    return ""


_SKIP = _skip_reason()
pytestmark = pytest.mark.skipif(bool(_SKIP), reason=_SKIP)


# --------------------------------------------------------------------------- #
# Deterministic tool-calling chat provider — calls the first no-arg MCP tool.
# --------------------------------------------------------------------------- #
class _McpToolCallingProvider(ChatProvider):
    """Offline stand-in for the LLM: on each turn it calls the first offered
    ``mcp__*`` tool that needs no required arguments, then wraps the tool result in
    the answer between sentinels so the test can assert it round-tripped."""

    model_id = "mcp-e2e"
    supports_tools = True
    last_system = ""

    def stream(self, messages, *, system=None):
        yield "ok"

    def run_tools(self, messages, *, system=None, tools=None, execute=None, max_iterations=4):
        self.last_system = system or ""
        tool = self._pick(tools)
        if not (tool and execute):
            yield {"type": "text", "text": "NO_MCP_TOOL"}
            return
        yield {"type": "tool_call", "name": tool["name"], "args": {}}
        result = execute(tool["name"], {})
        yield {"type": "tool_result", "name": tool["name"]}
        yield {"type": "text", "text": f"RESULT_BEGIN {result} RESULT_END"}

    @staticmethod
    def _pick(tools):
        mcp = [t for t in (tools or []) if t["name"].startswith("mcp__")]
        for t in mcp:
            if not (t.get("schema") or {}).get("required"):
                return t
        return mcp[0] if mcp else None


class _EmptyRetriever:
    """No RAG context (keeps the test off the embeddings/pgvector path)."""
    def retrieve(self, identity, message, k=8):
        return []


# --------------------------------------------------------------------------- #
# Fixture: a real app wired to the real store + real MCP provider, fake LLM.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def ctx():
    from fastapi.testclient import TestClient
    from convert_search_ai import db
    from convert_search_ai.app import build_app
    from convert_search_ai.config import Config
    from convert_search_ai.crypto import generate_key
    from convert_search_ai.ldap_auth import Identity
    from convert_search_ai.schema import schema_name

    cfg = Config()
    cfg.mcp_enabled = True
    cfg.mcp_secret_key = generate_key()
    cfg.audit_log_file = tempfile.mktemp(prefix="csai_mcp_e2e_audit_", suffix=".log")
    tenant = "mcpe2e_" + uuid.uuid4().hex[:8]

    # Reference server reachable? Skip cleanly (not error) on a network blip.
    from convert_search_ai import mcp_client as mc
    try:
        specs = mc.discover_tools(endpoint_url=_REF_URL, transport=_REF_TRANSPORT,
                                  headers={}, timeout_s=20)
    except Exception as e:
        pytest.skip(f"reference MCP server {_REF_URL} unreachable: {e.__class__.__name__}: {e}")
    if not specs:
        pytest.skip(f"reference MCP server {_REF_URL} returned no tools")
    if all((s.input_schema or {}).get("required") for s in specs):
        pytest.skip(f"reference MCP server {_REF_URL} has no no-argument tool to call")

    provider = _McpToolCallingProvider()
    app = build_app(cfg)                       # wires app.state.mcp_store + app.state.mcp
    app.state.chat._chat = provider            # replace the LLM with the deterministic stand-in
    app.state.chat.retriever = _EmptyRetriever()
    client = TestClient(app)

    token = app.state.token_store.issue(
        Identity(user="mcp-admin", roles=["administrators", "users"], tenant=tenant,
                 authenticated=True))
    yield {"cfg": cfg, "app": app, "client": client, "tenant": tenant, "token": token,
           "auth": {"Authorization": f"Bearer {token}", "X-Tenant": tenant}}

    # Teardown: drop the throwaway tenant schema so we leave a clean slate.
    try:
        conn = db.connect(cfg)
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name(tenant)}" CASCADE')
        conn.commit()
        conn.close()
    except Exception:
        pass
    try:
        os.remove(cfg.audit_log_file)
    except OSError:
        pass


_BASE = "/v1/admin/mcp-integrations"


def _audit(ctx):
    try:
        return [json.loads(ln[len("audit "):]) for ln in open(ctx["cfg"].audit_log_file)
                if ln.startswith("audit ")]
    except OSError:
        return []


def _create_integration(ctx, *, enabled=True):
    body = {"name": "Reference MCP", "endpoint_url": _REF_URL,
            "transport": _REF_TRANSPORT, "auth_type": "none", "enabled": enabled}
    r = ctx["client"].post(_BASE, json=body, headers=ctx["auth"])
    assert r.status_code == 200, r.text
    return r.json()


def _drive_turn(ctx, *, approve: bool):
    """One chat turn over the WS: approve/deny every consent prompt. Returns
    ``(events, answer_text, tool_full)``."""
    token, tenant = ctx["token"], ctx["tenant"]
    events, parts, tool_full = [], [], None
    with ctx["client"].websocket_connect(f"/chat?token={token}&tenant={tenant}") as ws:
        ws.send_json({"message": "Use the available external tool to check status.",
                      "web_search": False})
        while True:
            e = ws.receive_json()
            events.append(e)
            t = e.get("type")
            if t == "tool_call":
                tool_full = e.get("name")
            elif t == "tool_consent_request":
                ws.send_json({"type": "tool_consent", "id": e["id"],
                              "decision": approve, "remember": False})
            elif t == "token":
                parts.append(e.get("text", ""))
            elif t == "done":
                break
    return events, "".join(parts), tool_full


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_admin_dry_run_discovers_real_tools(ctx):
    r = ctx["client"].post(f"{_BASE}/test", json={
        "name": "probe", "endpoint_url": _REF_URL, "transport": _REF_TRANSPORT,
        "auth_type": "none"}, headers=ctx["auth"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["tools"], body
    # Every discovered tool carries a name + schema we can wrap.
    assert all(t.get("name") and "input_schema" in t for t in body["tools"])


def test_admin_create_and_list_secret_free(ctx):
    integ = _create_integration(ctx, enabled=True)
    assert integ["enabled"] is True and integ["slug"] and integ["has_secret"] is False
    assert "secret" not in integ and "secret_enc" not in integ  # never returned
    listing = ctx["client"].get(_BASE, headers=ctx["auth"]).json()["integrations"]
    assert integ["id"] in [i["id"] for i in listing]
    # Clean up so the chat tests start from a single, known integration.
    ctx["client"].delete(f"{_BASE}/{integ['id']}", headers=ctx["auth"])


def test_chat_consent_approve_runs_real_tool(ctx):
    integ = _create_integration(ctx, enabled=True)
    try:
        events, answer, tool_full = _drive_turn(ctx, approve=True)
        types = [e["type"] for e in events]
        assert "tool_consent_request" in types, types
        assert "tool_result" in types, types
        assert tool_full and tool_full.startswith(f"mcp__{integ['slug']}__"), tool_full
        # The real tool's output round-tripped into the answer.
        assert "RESULT_BEGIN" in answer and "RESULT_END" in answer
        result = answer.split("RESULT_BEGIN", 1)[1].split("RESULT_END", 1)[0]
        assert result.strip() and "declined" not in result.lower()
        # The invocation left a bibliographic note (an MCP citation) in the turn.
        cites = [e for e in events if e["type"] == "citations"][-1]["citations"]
        mcp_cites = [c for c in cites if c.get("kind") == "mcp"]
        assert mcp_cites and mcp_cites[0]["integration"] == integ["name"]
        assert isinstance(mcp_cites[0].get("marker"), int)
        # Audited: consent granted + the external tool call recorded ok.
        actions = _audit(ctx)
        assert any(a["action"] == "mcp_consent" and a["result"] == "ok" for a in actions)
        assert any(a["action"] == "mcp_tool" and a["result"] == "ok" for a in actions)
        # The model was told about the external tools.
        assert "external" in ctx["app"].state.chat._chat.last_system.lower()
    finally:
        ctx["client"].delete(f"{_BASE}/{integ['id']}", headers=ctx["auth"])


def test_chat_consent_deny_does_not_run_tool(ctx):
    integ = _create_integration(ctx, enabled=True)
    try:
        before = sum(1 for a in _audit(ctx) if a["action"] == "mcp_tool" and a["result"] == "ok")
        events, answer, _ = _drive_turn(ctx, approve=False)
        assert "tool_consent_request" in [e["type"] for e in events]
        # The tool did NOT run: the model sees a declined message, and no new
        # successful mcp_tool call was audited for this turn.
        assert "declined" in answer.lower()
        after = sum(1 for a in _audit(ctx) if a["action"] == "mcp_tool" and a["result"] == "ok")
        assert after == before
        assert any(a["action"] == "mcp_consent" and a["result"] == "denied" for a in _audit(ctx))
    finally:
        ctx["client"].delete(f"{_BASE}/{integ['id']}", headers=ctx["auth"])
