"""Endpoint tests for the M2 surface (TestClient + injected fake search + tokens)."""
from fastapi.testclient import TestClient

import convert_search_ai.api as apimod
from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.search import Hit


class FakeSearch:
    def search(self, identity, query, *, limit=20, fuzzy=True):
        return [Hit("f1", "Doc", "a **hit** here", 1.0)]

    def get_text(self, identity, file_uid):
        if file_uid == "denied":
            raise PermissionError()
        if file_uid == "missing":
            raise FileNotFoundError()
        return "# Markdown"


def _client():
    app = build_app(Config(), search=FakeSearch())
    return app, TestClient(app)


def _bearer(app, user="alice", roles=("administrators",), tenant="default"):
    ident = Identity(user=user, roles=list(roles), tenant=tenant, authenticated=True)
    return {"Authorization": f"Bearer {app.state.token_store.issue(ident)}"}


def test_search_requires_auth():
    _, c = _client()
    assert c.post("/search", json={"query": "x"}).status_code == 401


def test_search_with_token_returns_hits():
    app, c = _client()
    r = c.post("/search", json={"query": "hello"}, headers=_bearer(app))
    assert r.status_code == 200
    assert r.json()["hits"][0]["file_uid"] == "f1"


def test_search_empty_query_400():
    app, c = _client()
    assert c.post("/search", json={"query": "   "}, headers=_bearer(app)).status_code == 400


def test_whoami():
    app, c = _client()
    r = c.get("/whoami", headers=_bearer(app, user="bob"))
    assert r.status_code == 200 and r.json()["user"] == "bob"


def test_text_endpoint_states():
    app, c = _client()
    h = _bearer(app)
    assert c.get("/documents/f1/text", headers=h).json()["text"] == "# Markdown"
    assert c.get("/documents/denied/text", headers=h).status_code == 403
    assert c.get("/documents/missing/text", headers=h).status_code == 404
    assert c.get("/documents/f1/text").status_code == 401  # no auth


def test_auth_token_success_and_failure(monkeypatch):
    app, c = _client()

    def fake_auth(config, user, password):
        return Identity(user=user, roles=["users"], tenant="default",
                        authenticated=(password == "right"))

    monkeypatch.setattr(apimod, "authenticate", fake_auth)
    ok = c.post("/auth/token", json={"username": "alice", "password": "right"})
    assert ok.status_code == 200 and ok.json()["token_type"] == "bearer"
    bad = c.post("/auth/token", json={"username": "alice", "password": "wrong"})
    assert bad.status_code == 401
