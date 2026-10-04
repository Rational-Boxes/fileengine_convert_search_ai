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


class AgentMF:
    """The service's own client: it wrote the renditions, so it removes them."""

    def __init__(self, names=()):
        self.children = {n: f"uid-{n}" for n in names}
        self.removed = []

    def dir(self, uid, tenant=None):
        return [types.SimpleNamespace(name=n, uid=u) for n, u in self.children.items()]

    def remove(self, uid, tenant=None):
        self.removed.append(uid)
        self.children = {n: u for n, u in self.children.items() if u != uid}


def _setup(monkeypatch, *, mf=None, readable=True, published=(), sniffed_mime=None,
           live_links=None, children=(), internal_secret=""):
    from convert_search_ai.media_worker import ShareRefs
    monkeypatch.setattr(ShareRefs, "live_links", lambda self, t, f: live_links)
    cfg = Config()
    cfg.internal_secret = internal_secret
    app = build_app(cfg)
    mf = mf or CallerMF()
    monkeypatch.setattr(core_client, "client_for", lambda identity, config: mf)
    monkeypatch.setattr(db, "provision_tenant", lambda config, tenant: f"tenant_{tenant}")
    app.state.permission_gate = Gate(readable)
    app.state.media_jobs = MemoryMediaJobStore()
    doc = types.SimpleNamespace(mime=sniffed_mime) if sniffed_mime else None
    app.state.ingestor = types.SimpleNamespace(
        store=types.SimpleNamespace(get_status=lambda t, u: doc),
        pipeline=types.SimpleNamespace(writer=types.SimpleNamespace(
            names_for_version=lambda u, v, t: [f"{v}-preview.webm", *published],
            mf=AgentMF(children))))
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


_PUBLISHED = ("v7-media.webm", "v7-media_sd.webm", "v7-emailposter.gif", "v7-preview.webm",
              "v7-poster.webp")


def test_unpublish_refuses_to_remove_a_published_copy_it_cannot_prove_unused(monkeypatch):
    c, h, app, _ = _setup(monkeypatch, live_links=None, children=_PUBLISHED)
    assert c.delete("/documents/f1/media", headers=h).status_code == 409
    assert app.state.ingestor.pipeline.writer.mf.removed == []


def test_unpublish_refuses_while_a_live_link_plays_the_file(monkeypatch):
    c, h, app, _ = _setup(monkeypatch, live_links=2, children=_PUBLISHED)
    r = c.delete("/documents/f1/media", headers=h)
    assert r.status_code == 409 and "2 live share link" in r.json()["detail"]
    assert app.state.ingestor.pipeline.writer.mf.removed == []


def test_unpublish_removes_only_published_copies_once_no_link_plays_them(monkeypatch):
    c, h, app, _ = _setup(monkeypatch, live_links=0, children=_PUBLISHED)
    r = c.delete("/documents/f1/media", headers=h)
    assert r.status_code == 200
    assert sorted(r.json()["removed"]) == ["v7-emailposter.gif", "v7-media.webm", "v7-media_sd.webm"]
    left = set(app.state.ingestor.pipeline.writer.mf.children)
    assert left == {"v7-preview.webm", "v7-poster.webp"}        # the browser's, never ours


def test_unpublish_with_nothing_published_is_404(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch, live_links=0, children=("v7-preview.webm",))
    assert c.delete("/documents/f1/media", headers=h).status_code == 404


def test_unpublish_cancels_but_keeps_copies_a_link_plays(monkeypatch):
    c, h, app, _ = _setup(monkeypatch, live_links=1, children=_PUBLISHED)
    c.post("/documents/f1/media", headers=h, json={})
    r = c.delete("/documents/f1/media", headers=h)
    assert r.status_code == 200 and len(r.json()["cancelled"]) == 3
    assert r.json()["removed"] == [] and "live share link" in r.json()["kept"]


# ── share_service's internal republish (MS3, §6.2 rule 3) ────────────────────

def _internal(c, secret, body):
    return c.post("/internal/documents/f1/media", json=body,
                  headers={"X-Internal-Auth": secret} if secret is not None else {})


def test_internal_republish_is_disabled_without_a_secret(monkeypatch):
    c, _h, _a, _m = _setup(monkeypatch, internal_secret="")
    assert _internal(c, "anything", {"user": "ann", "tenant": "acme"}).status_code == 404


def test_internal_republish_refuses_a_wrong_or_missing_secret(monkeypatch):
    c, _h, app, _ = _setup(monkeypatch, internal_secret="s3cret")
    assert _internal(c, "nope", {"user": "ann", "tenant": "acme"}).status_code == 403
    assert _internal(c, None, {"user": "ann", "tenant": "acme"}).status_code == 403
    assert app.state.media_jobs.for_file("acme", "f1") == []


def test_internal_republish_queues_as_the_link_creator_on_read(monkeypatch):
    mf = CallerMF(write=False)                     # a reader, not an editor
    c, _h, app, _ = _setup(monkeypatch, mf=mf, internal_secret="s3cret")
    r = _internal(c, "s3cret", {"user": "ann", "tenant": "acme", "roles": ["users"],
                                "link_uid": "L1"})
    assert r.status_code == 202, r.text
    assert [j["requested_by"] for j in r.json()["jobs"]] == ["ann"] * 3
    assert "r" in mf.asked and "w" not in mf.asked


def test_internal_republish_refuses_a_creator_who_lost_access(monkeypatch):
    c, _h, app, _ = _setup(monkeypatch, mf=CallerMF(exists=False), internal_secret="s3cret")
    assert _internal(c, "s3cret", {"user": "ann", "tenant": "acme"}).status_code == 403
    assert app.state.media_jobs.for_file("acme", "f1") == []


def test_internal_republish_needs_a_named_principal(monkeypatch):
    c, _h, _a, _m = _setup(monkeypatch, internal_secret="s3cret")
    assert _internal(c, "s3cret", {"tenant": "acme"}).status_code == 400


def test_unpublish_requires_write(monkeypatch):
    c, h, _a, _m = _setup(monkeypatch, mf=CallerMF(write=False))
    assert c.delete("/documents/f1/media", headers=h).status_code == 403


def test_the_routes_require_authentication(monkeypatch):
    c, _h, _a, _m = _setup(monkeypatch)
    assert c.post("/documents/f1/media", json={}).status_code == 401
    assert c.get("/documents/f1/media").status_code == 401
