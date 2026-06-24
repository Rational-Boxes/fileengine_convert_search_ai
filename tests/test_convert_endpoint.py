"""POST /documents/{uid}/convert — on-demand rendition (preview) generation."""
from fastapi.testclient import TestClient

import convert_search_ai.api as api
import convert_search_ai.core_client as core_client
from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.pipeline import ConvertOutcome


class FakePipeline:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def convert(self, uid, tenant):
        self.calls.append((uid, tenant))
        return self.outcome


class FakeIngestor:
    def __init__(self, pipeline):
        self.pipeline = pipeline


class FakeGate:
    def __init__(self, allow):
        self.allow = allow

    def can_read(self, mf, identity, uid):
        return self.allow


def _setup(monkeypatch, *, allow=True, outcome=None):
    app = build_app(Config())
    pipe = FakePipeline(
        outcome
        or ConvertOutcome("f1", "converted", ["v-thumbnail.png", "v-preview.png"], has_markdown=True)
    )
    app.state.ingestor = FakeIngestor(pipe)
    app.state.permission_gate = FakeGate(allow)
    monkeypatch.setattr(api, "_check_core", lambda config: True)
    monkeypatch.setattr(core_client, "client_for", lambda identity, config: object())
    tok = app.state.token_store.issue(Identity(user="u", tenant="default", authenticated=True))
    return TestClient(app), tok, pipe


def test_convert_triggers_pipeline_for_a_readable_file(monkeypatch):
    client, tok, pipe = _setup(monkeypatch, allow=True)
    r = client.post("/documents/f1/convert", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "converted"
    assert body["renditions"] == ["v-thumbnail.png", "v-preview.png"]
    assert body["has_markdown"] is True
    assert pipe.calls == [("f1", "default")]


def test_convert_forbidden_when_not_readable(monkeypatch):
    client, tok, pipe = _setup(monkeypatch, allow=False)
    r = client.post("/documents/f1/convert", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    assert pipe.calls == []  # never converted


def test_convert_requires_auth(monkeypatch):
    client, _tok, _pipe = _setup(monkeypatch, allow=True)
    r = client.post("/documents/f1/convert")
    assert r.status_code == 401
