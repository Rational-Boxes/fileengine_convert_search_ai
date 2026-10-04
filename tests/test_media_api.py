"""MS2 (MEDIA_SHARE.md §4.6): POST / GET / DELETE /documents/{uid}/media."""
from __future__ import annotations

import types

from fastapi.testclient import TestClient

import convert_search_ai.core_client as core_client
import convert_search_ai.db as db
from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.media_jobs import MemoryMediaJobStore


class CallerMF:
    """The core as the CALLER sees it — permission is per user."""

    def __init__(self, *, write=True, exists=True, name="intro.mp4", version="v7", is_dir=False):
        self.write, self.exists = write, exists
        self.info = types.SimpleNamespace(name=name, version=version, is_dir=is_dir, size=10)
        self.asked = []

    def check_permission(self, uid, perm, tenant=None):
        self.asked.append(perm)
        return self.write if perm == "w" else True

    def entity_exists(self, uid):
        return self.exists

    def stat(self, uid, tenant=None):
        if not self.exists:
            raise LookupError(uid)
        return self.info


class Gate:
    def __init__(self, allow):
        self.allow = allow

    def can_read(self, mf, identity, uid):
        return self.allow


def _setup(monkeypatch, *, mf=None, readable=True, published=(), sniffed_mime=None):
    app = build_app(Config())
    mf = mf or CallerMF()
    monkeypatch.setattr(core_client, "client_for", lambda identity, config: mf)
    monkeypatch.setattr(db, "provision_tenant", lambda config, tenant: f"tenant_{tenant}")
    app.state.permission_gate = Gate(readable)
    app.state.media_jobs = MemoryMediaJobStore()
    doc = types.SimpleNamespace(mime=sniffed_mime) if sniffed_mime else None
    app.state.ingestor = types.SimpleNamespace(
        store=types.SimpleNamespace(get_status=lambda t, u: doc),
        pipeline=types.SimpleNamespace(writer=types.SimpleNamespace(
            names_for_version=lambda u, v, t: [f"{v}-preview.webm", *published])))
    tok = app.state.token_store.issue(Identity(user="ann", tenant="acme", authenticated=True))
    return TestClient(app), {"Authorization": f"Bearer {tok}", "X-Tenant": "acme"}, app, mf


def test_publishing_queues_the_video_profiles_for_the_current_version(monkeypatch):
    c, h, app, mf = _setup(monkeypatch)
    r = c.post("/documents/f1/media", headers=h, json={})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["source_version"] == "v7" and body["mime"] == "video/mp4"
    assert [j["profile"] for j in body["jobs"]] == [
        "video-720p-vp9", "video-480p-vp9", "video-emailposter"]
    assert all(j["status"] == "queued" and j["created"] for j in body["jobs"])
    assert all(j["requested_by"] == "ann" for j in body["jobs"])
    assert "w" in mf.asked                     # WRITE was asked of the core, as the caller


def test_asking_twice_queues_nothing_new(monkeypatch):
    c, h, app, _ = _setup(monkeypatch)
    first = c.post("/documents/f1/media", headers=h, json={}).json()["jobs"]
    second = c.post("/documents/f1/media", headers=h, json={}).json()["jobs"]
    assert [j["job_uid"] for j in first] == [j["job_uid"] for j in second]
    assert not any(j["created"] for j in second)


def test_publishing_requires_write(monkeypatch):
    c, h, app, _ = _setup(monkeypatch, mf=CallerMF(write=False))
    r = c.post("/documents/f1/media", headers=h, json={})
    assert r.status_code == 403
    assert app.state.media_jobs.for_file("acme", "f1") == []


def test_a_file_that_does_not_exist_is_refused_even_though_the_core_defaults_to_allow(monkeypatch):
    c, h, app, _ = _setup(monkeypatch, mf=CallerMF(exists=False))
    assert c.post("/documents/f1/media", headers=h, json={}).status_code == 403


def test_audio_queues_the_audio_profiles(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch, mf=CallerMF(name="talk.mp3"))
    r = c.post("/documents/f1/media", headers=h, json={})
    assert [j["profile"] for j in r.json()["jobs"]] == ["audio-mp3", "audio-opus"]


def test_the_sniffed_mime_wins_over_the_name(monkeypatch):
    # A video with a misleading extension: ingest sniffed the content.
    c, h, _a, _m = _setup(monkeypatch, mf=CallerMF(name="clip.bin"), sniffed_mime="video/webm")
    assert c.post("/documents/f1/media", headers=h, json={}).status_code == 202


def test_a_document_is_not_publishable(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch, mf=CallerMF(name="report.pdf"))
    assert c.post("/documents/f1/media", headers=h, json={}).status_code == 415


def test_one_profile_may_be_requested_but_only_one_that_applies(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch)
    r = c.post("/documents/f1/media", headers=h, json={"profile": "video-480p-vp9"})
    assert [j["profile"] for j in r.json()["jobs"]] == ["video-480p-vp9"]
    assert c.post("/documents/f1/media", headers=h,
                  json={"profile": "audio-mp3"}).status_code == 400


def test_state_lists_jobs_and_published_renditions_only(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch, published=("v7-media.webm", "v7-media_sd.webm"))
    c.post("/documents/f1/media", headers=h, json={})
    r = c.get("/documents/f1/media", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 3
    assert body["renditions"] == ["v7-media.webm", "v7-media_sd.webm"]   # not the preview


def test_state_is_read_gated(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch, readable=False)
    assert c.get("/documents/f1/media", headers=h).status_code == 403


def test_unpublish_cancels_running_work(monkeypatch):
    c, h, app, _ = _setup(monkeypatch)
    c.post("/documents/f1/media", headers=h, json={})
    r = c.delete("/documents/f1/media", headers=h)
    assert r.status_code == 200
    assert len(r.json()["cancelled"]) == 3
    assert {j.status for j in app.state.media_jobs.for_file("acme", "f1")} == {"cancelled"}


def test_unpublish_refuses_to_remove_a_published_copy_it_cannot_prove_unused(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch)
    assert c.delete("/documents/f1/media", headers=h).status_code == 409


def test_unpublish_requires_write(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch, mf=CallerMF(write=False))
    assert c.delete("/documents/f1/media", headers=h).status_code == 403


def test_the_routes_require_authentication(monkeypatch):
    c, _h, _a, _m = _setup(monkeypatch)
    assert c.post("/documents/f1/media", json={}).status_code == 401
    assert c.get("/documents/f1/media").status_code == 401
