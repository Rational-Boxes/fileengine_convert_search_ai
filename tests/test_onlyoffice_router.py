"""Router tests for ONLYOFFICE editing (TestClient + injected fake core client).

No Document Server / gRPC core needed: ``client_for`` and the edited-doc fetch are
monkeypatched. The full loop against a real Document Server is in test_onlyoffice_e2e."""
import io

import convert_search_ai.core_client as core_client
import convert_search_ai.routers.onlyoffice as oor
from fastapi.testclient import TestClient

from convert_search_ai import onlyoffice as oo
from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.crypto import sign_scoped_token, verify_scoped_token
from convert_search_ai.ldap_auth import Identity


class FakeInfo:
    def __init__(self, name, version="v1"):
        self.name, self.version = name, version


class FakeMF:
    def __init__(self, *, name="report.docx", writable=True, content=b"DOCX-BYTES"):
        self._name, self._writable, self._content = name, writable, content
        self.puts = []

    def stat(self, uid, **kw):
        return FakeInfo(self._name, "20260101_000000.000")

    def check_permission(self, uid, perm, **kw):
        return perm == "WRITE" and self._writable

    def get(self, uid, **kw):
        return io.BytesIO(self._content)

    def put(self, uid, payload, **kw):
        self.puts.append((uid, payload))
        return 1.0

    def close(self):
        pass


def _app(monkeypatch, *, mf=None, enabled=True, jwt_secret="", **cfgover):
    cfg = Config()
    cfg.onlyoffice_enabled = enabled
    cfg.onlyoffice_docserver_url = "http://localhost:8080"
    cfg.onlyoffice_signing_secret = "sign-secret"
    cfg.onlyoffice_jwt_secret = jwt_secret
    cfg.onlyoffice_callback_base = "http://host.docker.internal:8092"
    for k, v in cfgover.items():
        setattr(cfg, k, v)
    the_mf = mf or FakeMF()
    monkeypatch.setattr(core_client, "client_for", lambda identity, config: the_mf)
    app = build_app(cfg)
    return app, TestClient(app), the_mf


def _auth(app, user="alice", roles=("users",)):
    tok = app.state.token_store.issue(
        Identity(user=user, roles=list(roles), tenant="default", authenticated=True))
    return {"Authorization": f"Bearer {tok}"}


def test_disabled_returns_404(monkeypatch):
    app, c, _ = _app(monkeypatch, enabled=False)
    assert c.get("/v1/onlyoffice/config/f1", headers=_auth(app)).status_code == 404


def test_config_requires_auth(monkeypatch):
    app, c, _ = _app(monkeypatch)
    assert c.get("/v1/onlyoffice/config/f1").status_code == 401


def test_config_returns_signed_editor_config(monkeypatch):
    app, c, _ = _app(monkeypatch, jwt_secret="docserver-secret")
    r = c.get("/v1/onlyoffice/config/f1", headers=_auth(app))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["docserver_url"] == "http://localhost:8080"
    cfg = body["config"]
    assert cfg["documentType"] == "word" and cfg["document"]["fileType"] == "docx"
    assert cfg["editorConfig"]["user"]["id"] == "alice"
    # URLs point at the callback base (reachable from the Doc Server), with tokens
    assert cfg["document"]["url"].startswith("http://host.docker.internal:8092/v1/onlyoffice/download?token=")
    assert cfg["editorConfig"]["callbackUrl"].startswith(
        "http://host.docker.internal:8092/v1/onlyoffice/callback?token=")
    # config is JWT-signed (Doc Server requires it when JWT is enabled)
    assert oo.verify_onlyoffice_jwt("docserver-secret", cfg["token"])["document"]["key"]
    # the download token binds the requesting user + file
    dl = cfg["document"]["url"].split("token=", 1)[1]
    claims = verify_scoped_token("sign-secret", dl, purpose="oo-download")
    assert claims["user"] == "alice" and claims["file_uid"] == "f1"


def test_config_415_for_non_editable(monkeypatch):
    app, c, _ = _app(monkeypatch, mf=FakeMF(name="photo.png"))
    assert c.get("/v1/onlyoffice/config/f1", headers=_auth(app)).status_code == 415


def test_config_403_without_write(monkeypatch):
    app, c, _ = _app(monkeypatch, mf=FakeMF(writable=False))
    assert c.get("/v1/onlyoffice/config/f1", headers=_auth(app)).status_code == 403


def test_download_serves_bytes_for_valid_token(monkeypatch):
    app, c, mf = _app(monkeypatch)
    tok = sign_scoped_token("sign-secret", purpose="oo-download", ttl=600,
                            file_uid="f1", user="alice", tenant="default", roles=["users"])
    r = c.get(f"/v1/onlyoffice/download?token={tok}")
    assert r.status_code == 200 and r.content == b"DOCX-BYTES"


def test_download_rejects_bad_and_wrong_purpose_token(monkeypatch):
    app, c, _ = _app(monkeypatch)
    assert c.get("/v1/onlyoffice/download?token=garbage").status_code == 401
    # a callback-purpose token cannot be replayed on the download endpoint
    cb = sign_scoped_token("sign-secret", purpose="oo-callback", ttl=600,
                           file_uid="f1", user="alice", tenant="default", roles=[])
    assert c.get(f"/v1/onlyoffice/download?token={cb}").status_code == 401


def test_callback_editing_status_does_not_write(monkeypatch):
    app, c, mf = _app(monkeypatch)
    tok = sign_scoped_token("sign-secret", purpose="oo-callback", ttl=600,
                            file_uid="f1", user="alice", tenant="default", roles=["users"])
    r = c.post(f"/v1/onlyoffice/callback?token={tok}", json={"status": 1})
    assert r.status_code == 200 and r.json() == {"error": 0}
    assert mf.puts == []  # status 1 = still editing → no version written


def test_callback_save_writes_new_version_as_user(monkeypatch):
    app, c, mf = _app(monkeypatch)
    monkeypatch.setattr(oor, "_fetch", lambda url, **kw: b"EDITED-DOCX")
    tok = sign_scoped_token("sign-secret", purpose="oo-callback", ttl=600,
                            file_uid="f1", user="alice", tenant="default", roles=["users"])
    r = c.post(f"/v1/onlyoffice/callback?token={tok}",
               json={"status": 2, "url": "http://ds/edited.docx", "key": "k"})
    assert r.status_code == 200 and r.json() == {"error": 0}
    assert mf.puts == [("f1", b"EDITED-DOCX")]  # new version written back


def test_callback_verifies_onlyoffice_jwt_when_enabled(monkeypatch):
    app, c, mf = _app(monkeypatch, jwt_secret="docserver-secret")
    monkeypatch.setattr(oor, "_fetch", lambda url, **kw: b"EDITED")
    tok = sign_scoped_token("sign-secret", purpose="oo-callback", ttl=600,
                            file_uid="f1", user="alice", tenant="default", roles=["users"])
    # unsigned body → rejected
    assert c.post(f"/v1/onlyoffice/callback?token={tok}", json={"status": 2, "url": "x"}).status_code == 401
    assert mf.puts == []
    # correctly-signed envelope → saved
    signed = oo.sign_onlyoffice_jwt("docserver-secret",
                                    {"status": 2, "url": "http://ds/e.docx", "key": "k"})
    r = c.post(f"/v1/onlyoffice/callback?token={tok}", json={"token": signed})
    assert r.status_code == 200 and mf.puts == [("f1", b"EDITED")]


def test_callback_rejects_bad_scoped_token(monkeypatch):
    app, c, _ = _app(monkeypatch)
    assert c.post("/v1/onlyoffice/callback?token=bad", json={"status": 2}).status_code == 401
