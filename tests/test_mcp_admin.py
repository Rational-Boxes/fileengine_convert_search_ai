"""MCP admin API: CRUD, tenant-admin gating, SSRF/stdio rejection, secret write-only.

Uses an injected in-memory store (no Postgres) + a monkeypatched endpoint validator
on the happy paths; one test exercises the real SSRF guard with a literal IP."""
import uuid

import convert_search_ai.mcp_client as mc
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.mcp_store import McpIntegration, slugify


class InMemoryStore:
    def __init__(self):
        self.rows = {}          # id -> McpIntegration
        self.secrets = {}       # id -> plaintext (test view)

    def list(self, tenant, *, enabled_only=False):
        return [i for i in self.rows.values() if (i.enabled or not enabled_only)]

    def get(self, tenant, id_):
        return self.rows.get(id_)

    def count(self, tenant):
        return len(self.rows)

    def create(self, tenant, *, name, transport, endpoint_url, auth_type, auth_header="",
               secret_enc=None, headers=None, allowed_tools=None, enabled=False,
               forward_identity=False, allowed_roles=None, token_url="", oauth_client_id="",
               oauth_scope="", description="", created_by=""):
        id_ = uuid.uuid4().hex
        integ = McpIntegration(id=id_, name=name, slug=slugify(name), description=description,
                               transport=transport, endpoint_url=endpoint_url, auth_type=auth_type,
                               auth_header=auth_header, has_secret=secret_enc is not None,
                               headers=headers or {}, enabled=enabled, allowed_tools=allowed_tools,
                               forward_identity=forward_identity, allowed_roles=allowed_roles,
                               token_url=token_url, oauth_client_id=oauth_client_id,
                               oauth_scope=oauth_scope, created_by=created_by)
        self.rows[id_] = integ
        if secret_enc is not None:
            self.secrets[id_] = secret_enc
        return integ

    def update(self, tenant, id_, *, secret_enc=..., allowed_tools=..., allowed_roles=..., **fields):
        integ = self.rows[id_]
        for k, v in fields.items():
            if v is not None:
                setattr(integ, k, v)
        if secret_enc is not ...:
            integ.has_secret = secret_enc is not None
        if allowed_tools is not ...:
            integ.allowed_tools = allowed_tools
        if allowed_roles is not ...:
            integ.allowed_roles = allowed_roles
        return integ

    def delete(self, tenant, id_):
        return self.rows.pop(id_, None) is not None


def _client(monkeypatch, *, accept_urls=True):
    app = build_app(Config())
    app.state.config.mcp_secret_key = _fernet_key()
    app.state.mcp_store = InMemoryStore()
    if accept_urls:
        monkeypatch.setattr(mc, "validate_endpoint", lambda u: u)
    return app, TestClient(app)


def _fernet_key():
    from convert_search_ai.crypto import generate_key
    return generate_key()


def _tok(app, *, admin=True):
    roles = ["administrators"] if admin else ["users"]
    return app.state.token_store.issue(
        Identity(user="alice", roles=roles, tenant="default", authenticated=True))


def _auth(app, **kw):
    return {"Authorization": f"Bearer {_tok(app, **kw)}"}


_BASE = "/v1/admin/mcp-integrations"


def test_non_admin_gets_403(monkeypatch):
    app, c = _client(monkeypatch)
    r = c.get(_BASE, headers=_auth(app, admin=False))
    assert r.status_code == 403


def test_unauthenticated_gets_401(monkeypatch):
    app, c = _client(monkeypatch)
    assert c.get(_BASE).status_code == 401


def test_create_list_get_delete(monkeypatch):
    app, c = _client(monkeypatch)
    body = {"name": "CRM", "endpoint_url": "https://mcp.example.com/mcp",
            "transport": "streamable-http", "auth_type": "bearer", "secret": "tok",
            "enabled": True}
    r = c.post(_BASE, json=body, headers=_auth(app))
    assert r.status_code == 200, r.text
    integ = r.json()
    assert integ["slug"] == "crm" and integ["has_secret"] is True
    assert "secret" not in integ and "secret_enc" not in integ  # never returned

    lst = c.get(_BASE, headers=_auth(app)).json()["integrations"]
    assert len(lst) == 1 and lst[0]["id"] == integ["id"]

    got = c.get(f"{_BASE}/{integ['id']}", headers=_auth(app)).json()
    assert got["name"] == "CRM"

    assert c.delete(f"{_BASE}/{integ['id']}", headers=_auth(app)).status_code == 200
    assert c.get(f"{_BASE}/{integ['id']}", headers=_auth(app)).status_code == 404


def test_stdio_transport_rejected(monkeypatch):
    app, c = _client(monkeypatch)
    r = c.post(_BASE, json={"name": "x", "endpoint_url": "https://e.example/mcp",
                            "transport": "stdio", "auth_type": "none"}, headers=_auth(app))
    assert r.status_code == 400 and "stdio" in r.json()["detail"]


def test_bearer_requires_secret(monkeypatch):
    app, c = _client(monkeypatch)
    r = c.post(_BASE, json={"name": "x", "endpoint_url": "https://e.example/mcp",
                            "auth_type": "bearer"}, headers=_auth(app))
    assert r.status_code == 400 and "secret" in r.json()["detail"]


def test_allowed_roles_create_and_normalization(monkeypatch):
    app, c = _client(monkeypatch)
    r = c.post(_BASE, json={"name": "Restricted", "endpoint_url": "https://mcp.example.com/mcp",
                            "auth_type": "none", "allowed_roles": ["engineering", " ", "admins"],
                            "enabled": True}, headers=_auth(app))
    assert r.status_code == 200, r.text
    # blanks dropped
    assert r.json()["allowed_roles"] == ["engineering", "admins"]
    cid = r.json()["id"]

    # empty list normalizes to null (= all users)
    up = c.put(f"{_BASE}/{cid}", json={"allowed_roles": []}, headers=_auth(app))
    assert up.status_code == 200 and up.json()["allowed_roles"] is None

    # bad type rejected
    bad = c.post(_BASE, json={"name": "x", "endpoint_url": "https://e/mcp", "auth_type": "none",
                              "allowed_roles": "engineering"}, headers=_auth(app))
    assert bad.status_code == 400 and "allowed_roles" in bad.json()["detail"]


def test_oauth_create_and_requirements(monkeypatch):
    app, c = _client(monkeypatch)
    # happy path: oauth with token_url + client_id + secret
    ok = c.post(_BASE, json={
        "name": "OAuth MCP", "endpoint_url": "https://mcp.example.com/mcp",
        "auth_type": "oauth", "token_url": "https://auth.example.com/token",
        "oauth_client_id": "client-1", "oauth_scope": "mcp.read", "secret": "client-secret",
        "enabled": True}, headers=_auth(app))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["auth_type"] == "oauth" and body["token_url"] == "https://auth.example.com/token"
    assert body["oauth_client_id"] == "client-1" and body["has_secret"] is True
    assert "secret" not in body  # client secret never returned

    # missing token_url → 400
    r = c.post(_BASE, json={"name": "a", "endpoint_url": "https://e/mcp", "auth_type": "oauth",
                            "oauth_client_id": "c", "secret": "s"}, headers=_auth(app))
    assert r.status_code == 400 and "token_url" in r.json()["detail"]
    # missing client_id → 400
    r = c.post(_BASE, json={"name": "b", "endpoint_url": "https://e/mcp", "auth_type": "oauth",
                            "token_url": "https://auth/token", "secret": "s"}, headers=_auth(app))
    assert r.status_code == 400 and "oauth_client_id" in r.json()["detail"]
    # missing secret → 400
    r = c.post(_BASE, json={"name": "d", "endpoint_url": "https://e/mcp", "auth_type": "oauth",
                            "token_url": "https://auth/token", "oauth_client_id": "c"},
               headers=_auth(app))
    assert r.status_code == 400 and "secret" in r.json()["detail"]


def test_count_cap(monkeypatch):
    app, c = _client(monkeypatch)
    app.state.config.mcp_max_integrations = 1
    ok = c.post(_BASE, json={"name": "one", "endpoint_url": "https://e.example/mcp",
                             "auth_type": "none"}, headers=_auth(app))
    assert ok.status_code == 200
    over = c.post(_BASE, json={"name": "two", "endpoint_url": "https://e.example/mcp",
                               "auth_type": "none"}, headers=_auth(app))
    assert over.status_code == 409


def test_real_ssrf_guard_rejects_private_ip(monkeypatch):
    # accept_urls=False -> the REAL validator runs; a literal private IP needs no DNS.
    app, c = _client(monkeypatch, accept_urls=False)
    r = c.post(_BASE, json={"name": "x", "endpoint_url": "https://127.0.0.1/mcp",
                            "auth_type": "none"}, headers=_auth(app))
    assert r.status_code == 400 and "endpoint_url" in r.json()["detail"]


def test_update_toggle_enabled_and_rotate_secret(monkeypatch):
    app, c = _client(monkeypatch)
    integ = c.post(_BASE, json={"name": "CRM", "endpoint_url": "https://e.example/mcp",
                                "auth_type": "bearer", "secret": "old"},
                   headers=_auth(app)).json()
    r = c.put(f"{_BASE}/{integ['id']}", json={"enabled": True, "secret": "new"},
              headers=_auth(app))
    assert r.status_code == 200 and r.json()["enabled"] is True
    # store received a new encrypted secret (write-only; not echoed back)
    assert app.state.mcp_store.rows[integ["id"]].has_secret is True


def test_test_config_dry_run(monkeypatch):
    app, c = _client(monkeypatch)
    monkeypatch.setattr(mc, "discover_tools",
                        lambda **kw: [mc.ToolSpec("ping", "Ping", {})])
    r = c.post(f"{_BASE}/test", json={"name": "x", "endpoint_url": "https://e.example/mcp",
                                      "transport": "streamable-http", "auth_type": "none"},
               headers=_auth(app))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["tools"][0]["name"] == "ping"
