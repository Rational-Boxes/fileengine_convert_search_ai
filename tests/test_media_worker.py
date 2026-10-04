"""MS2 (MEDIA_SHARE.md §4.4): the durable job and the worker that runs it.

Driven with the in-memory job store, the fake core client and a scripted
encoder, so each rule is exercised directly: the attempt cap, idempotency,
the erasure race, cancellation, version pinning, and the interrupted-write
guard. The Postgres store is tested against a real database separately.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import types

import pytest

from convert_search_ai.media_encode import EncodeResult, MediaSettings
from convert_search_ai.media_jobs import MemoryMediaJobStore
from convert_search_ai.media_worker import MediaWorker
from convert_search_ai.plugins.base import Rendition
from convert_search_ai.renditions import PUBLISHED_FMTS, RenditionWriter
from fakes import FakeMF, FakeStore

T = "acme"


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += dt.timedelta(seconds=seconds)


def _cfg(**over):
    c = types.SimpleNamespace(media_stale_seconds=300, media_max_attempts=3,
                              media_max_input_bytes=0, media_poll_seconds=0.01)
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _ok_encoder(record=None, *, on_encode=None, status="ok", detail=""):
    """An encoder that writes a small file-backed rendition."""
    def encode(profile, src, settings, *, on_progress=None, should_cancel=None, **kw):
        if record is not None:
            with open(src, "rb") as f:
                record.append((profile, f.read()))
        if on_encode:
            on_encode()
        if on_progress:
            on_progress(50)
        if should_cancel and should_cancel():
            return EncodeResult("cancelled", detail="cancelled")
        if status != "ok":
            return EncodeResult(status, detail=detail)
        fd, path = tempfile.mkstemp(suffix=".webm")
        os.write(fd, b"WEBMDATA")
        os.close(fd)
        fmt = {"video-720p-vp9": "media", "video-480p-vp9": "media_sd",
               "audio-mp3": "audio"}.get(profile, "media")
        ext = "mp3" if fmt == "audio" else "webm"
        return EncodeResult("ok", Rendition.from_path(fmt, ext, path, "video/webm",
                                                       cleanup=lambda: os.path.exists(path) and os.remove(path)),
                            encoder="libvpx-vp9", output_bytes=8, duration_ms=4321,
                            width=1280, height=720)
    return encode


def _world(encoder=None, **cfg):
    clock = Clock()
    jobs = MemoryMediaJobStore(clock=clock)
    mf = FakeMF()
    mf.add_file("vid", "intro.mp4", content=b"SOURCE-V2", version="v2")
    # The OLD version's bytes, served when that version is asked for by name.
    old = {"v1": b"SOURCE-V1", "v2": b"SOURCE-V2"}
    base_stream = mf.get_stream

    def get_stream(uid, version="", tenant=None, **kw):
        if uid == "vid" and version in old:
            mf.streams.append(uid)
            yield old[version]
            return
        yield from base_stream(uid, version=version, tenant=tenant, **kw)
    mf.get_stream = get_stream
    store = FakeStore()
    emitted = []
    emitter = types.SimpleNamespace(publish=lambda etype, **kw: emitted.append((etype, kw)))
    w = MediaWorker(_cfg(**cfg), jobs, mf, store, RenditionWriter(mf), emitter=emitter,
                    settings=MediaSettings(), encode_fn=encoder or _ok_encoder())
    return types.SimpleNamespace(w=w, jobs=jobs, mf=mf, store=store, clock=clock,
                                 emitted=emitted)


# ── idempotency ─────────────────────────────────────────────────────────────


def test_two_requests_for_the_same_file_version_profile_yield_one_job():
    jobs = MemoryMediaJobStore()
    a, created_a = jobs.request(T, "vid", "v2", "video-720p-vp9", "u")
    b, created_b = jobs.request(T, "vid", "v2", "video-720p-vp9", "u")
    assert created_a and not created_b and a.job_uid == b.job_uid
    assert len(jobs.for_file(T, "vid")) == 1


def test_a_new_source_version_is_a_new_job():
    jobs = MemoryMediaJobStore()
    a, _ = jobs.request(T, "vid", "v1", "video-720p-vp9", "u")
    b, _ = jobs.request(T, "vid", "v2", "video-720p-vp9", "u")
    assert a.job_uid != b.job_uid


def test_a_failed_job_is_restarted_by_a_new_request():
    jobs = MemoryMediaJobStore()
    j, _ = jobs.request(T, "vid", "v2", "video-720p-vp9", "u")
    jobs.claim(T)
    jobs.finish(T, j.job_uid, "failed", detail="boom")
    again, created = jobs.request(T, "vid", "v2", "video-720p-vp9", "u")
    assert created and again.status == "queued" and again.attempts == 0


# ── the happy path ──────────────────────────────────────────────────────────


def test_a_job_publishes_the_rendition_and_records_it():
    seen = []
    x = _world(_ok_encoder(seen))
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    assert x.w.run_once() is True
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "succeeded" and job.progress_pct == 100
    assert job.rendition_name == "v2-media.webm"
    assert (job.output_bytes, job.duration_ms, job.width, job.height, job.encoder) == \
        (8, 4321, 1280, 720, "libvpx-vp9")
    assert "v2-media.webm" in x.mf.renditions["vid"]
    assert x.emitted and x.emitted[0][0] == "media.published"
    assert x.emitted[0][1]["renditions"] == ["v2-media.webm"]


def test_the_job_publishes_the_version_it_names_not_the_current_one():
    seen = []
    x = _world(_ok_encoder(seen))
    x.jobs.request(T, "vid", "v1", "video-720p-vp9", "ann")    # current is v2
    x.w.run_once()
    assert seen == [("video-720p-vp9", b"SOURCE-V1")]
    (job,) = x.jobs.for_file(T, "vid")
    assert job.rendition_name == "v1-media.webm"


def test_nothing_to_do_returns_false():
    assert _world().w.run_once() is False


# ── skipped and failed ──────────────────────────────────────────────────────


def test_a_skip_is_recorded_as_skipped_and_writes_nothing():
    x = _world(_ok_encoder(status="skipped", detail="already standard definition"))
    x.jobs.request(T, "vid", "v2", "video-480p-vp9", "ann")
    x.w.run_once()
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "skipped" and "standard definition" in job.detail
    assert not x.mf.renditions.get("vid")
    assert x.emitted == []


def test_a_failure_is_recorded_with_its_reason_and_announced():
    x = _world(_ok_encoder(status="failed", detail="no usable video encoder"))
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.w.run_once()
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "failed" and job.detail == "no usable video encoder"
    assert x.emitted[0][0] == "media.publish_failed"


def test_a_source_over_the_input_limit_fails_without_encoding():
    seen = []
    x = _world(_ok_encoder(seen), media_max_input_bytes=4)
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.w.run_once()
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "failed" and "limit" in job.detail and seen == []


# ── crash recovery and the attempt cap ──────────────────────────────────────


def test_a_crashed_job_is_requeued_exactly_max_attempts_times_then_fails():
    x = _world(max_attempts=3)
    j, _ = x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    for attempt in range(1, 4):
        claimed = x.jobs.claim(T)                 # a worker takes it ...
        assert claimed.attempts == attempt
        x.clock.advance(301)                      # ... and dies: no heartbeat
        x.jobs.requeue_stale(T, 300, 3)
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "failed" and job.detail == "repeatedly crashed"
    assert x.jobs.claim(T) is None


def test_a_job_with_a_fresh_heartbeat_is_not_requeued():
    x = _world()
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    j = x.jobs.claim(T)
    x.clock.advance(200)
    x.jobs.heartbeat(T, j.job_uid, 40)
    x.clock.advance(200)
    assert x.jobs.requeue_stale(T, 300, 3) == {"requeued": [], "failed": []}


# ── the erasure race and cancellation ───────────────────────────────────────


def test_an_erasure_landing_mid_encode_discards_the_output():
    holder = {}
    x = _world(_ok_encoder(on_encode=lambda: holder["x"].store.erased.add((T, "vid"))))
    holder["x"] = x
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.w.run_once()
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "cancelled" and "erased" in job.detail
    assert not x.mf.renditions.get("vid")


def test_an_erased_file_is_refused_before_anything_is_read():
    x = _world()
    x.store.erased.add((T, "vid"))
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.w.run_once()
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "cancelled" and x.mf.streams == []


def test_a_cancelled_job_kills_the_encode_and_writes_nothing():
    holder = {}
    x = _world(_ok_encoder(on_encode=lambda: holder["x"].jobs.cancel(T, "vid")))
    holder["x"] = x
    # Every progress tick checks for a cancel, so the worker sees it at once.
    import convert_search_ai.media_worker as mw
    old = mw.HEARTBEAT_SECONDS
    mw.HEARTBEAT_SECONDS = 0
    try:
        x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
        x.w.run_once()
    finally:
        mw.HEARTBEAT_SECONDS = old
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "cancelled"
    assert not x.mf.renditions.get("vid")
    assert x.emitted == []


# ── the interrupted write ───────────────────────────────────────────────────


def test_an_empty_child_left_by_an_interrupted_write_is_replaced():
    x = _world()
    # A previous attempt touched the child and died before streaming into it.
    x.mf.renditions["vid"] = {"v2-media.webm": "rend-empty"}
    x.mf.add_file("rend-empty", "v2-media.webm", content=b"")
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.w.run_once()
    (job,) = x.jobs.for_file(T, "vid")
    assert job.status == "succeeded"
    assert x.mf.renditions["vid"]["v2-media.webm"] != "rend-empty"
    assert any(payload == b"WEBMDATA" for _uid, payload in x.mf.puts)


def test_a_child_that_cannot_be_inspected_is_never_deleted():
    x = _world()
    x.mf.renditions["vid"] = {"v2-media.webm": "rend-unknown"}   # stat will fail
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.w.run_once()
    assert x.mf.renditions["vid"]["v2-media.webm"] == "rend-unknown"


# ── §4.3: ingest never publishes ────────────────────────────────────────────


def test_ingest_of_a_video_produces_only_poster_and_preview():
    """The default registry's video converter emits no published fmt — the
    full-length renditions come only from a publish job (§4.3)."""
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    import subprocess
    from convert_search_ai.pipeline import ConversionPipeline
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "clip.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=size=320x180:rate=25:duration=2", "-c:v", "libopenh264", src],
                       check=True)
        with open(src, "rb") as f:
            body = f.read()
    mf = FakeMF()
    mf.add_file("v", "clip.mp4", content=body, version="v1")
    out = ConversionPipeline(mf=mf, store=FakeStore()).convert("v", "default")
    fmts = {n.split("-", 1)[1].rsplit(".", 1)[0] for n in out.renditions_written}
    assert fmts and fmts <= {"poster", "preview"}
    assert not fmts & PUBLISHED_FMTS


# ── the orphan reaper (§4.3.1) ──────────────────────────────────────────────


def _reaper_world(live, *, days_ago=40):
    from convert_search_ai.media_worker import reap_orphans
    x = _world()
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.w.run_once()                              # publishes v2-media.webm
    x.clock.advance(days_ago * 86400)
    x.mf.renditions["vid"]["v2-preview.webm"] = "rend-preview"
    refs = types.SimpleNamespace(live_links=lambda t, f: live)
    cfg = types.SimpleNamespace(media_orphan_days=30)
    return x, lambda: reap_orphans(cfg, x.jobs, x.mf, refs, now=x.clock())


def test_the_reaper_removes_a_published_copy_no_live_link_has_needed_past_the_grace():
    x, reap = _reaper_world(0)
    assert reap() == ["v2-media.webm"]
    assert "v2-media.webm" not in x.mf.renditions["vid"]
    assert "v2-preview.webm" in x.mf.renditions["vid"]       # never the preview


def test_the_reaper_keeps_a_copy_a_live_link_still_plays():
    x, reap = _reaper_world(1)
    assert reap() == [] and "v2-media.webm" in x.mf.renditions["vid"]


def test_the_reaper_keeps_everything_within_the_grace_period():
    x, reap = _reaper_world(0, days_ago=5)
    assert reap() == [] and "v2-media.webm" in x.mf.renditions["vid"]


def test_the_reaper_keeps_everything_when_share_service_cannot_answer():
    x, reap = _reaper_world(None)               # unset, unreachable, or unintelligible
    assert reap() == [] and "v2-media.webm" in x.mf.renditions["vid"]


def test_the_refs_client_with_no_url_cannot_answer():
    from convert_search_ai.media_worker import ShareRefs
    refs = ShareRefs(types.SimpleNamespace(media_refs_url="", internal_secret="s"))
    assert refs.live_links(T, "vid") is None


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Http:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, timeout, headers):
        self.calls.append((url, headers))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _refs(resp, secret="s"):
    from convert_search_ai.media_worker import ShareRefs
    http = _Http(resp)
    cfg = types.SimpleNamespace(media_refs_url="http://share/share/v1/internal/media-refs/",
                                internal_secret=secret)
    return ShareRefs(cfg, http=http), http


def test_the_refs_client_reads_the_live_link_count_and_authenticates():
    refs, http = _refs(_Resp(200, {"file_uid": "vid", "live_links": 2}))
    assert refs.live_links(T, "vid") == 2
    url, headers = http.calls[0]
    assert url == "http://share/share/v1/internal/media-refs/vid"
    assert headers == {"X-Tenant": T, "X-Internal-Auth": "s"}


def test_the_refs_client_answers_none_for_anything_it_cannot_trust():
    for resp in (_Resp(503, {}), _Resp(200, {}), _Resp(200, {"live_links": "2"}),
                 _Resp(200, {"live_links": -1}), _Resp(200, {"live_links": True}),
                 _Resp(200, None), OSError("down")):
        refs, _ = _refs(resp)
        assert refs.live_links(T, "vid") is None, resp


def test_the_refs_client_with_no_secret_does_not_ask():
    refs, http = _refs(_Resp(200, {"live_links": 0}), secret="")
    assert refs.live_links(T, "vid") is None and http.calls == []


# ── a link plays the newest PUBLISHED version (2026-10-03, supersedes §6.2) ──
#
# A new upload is a correction the outside viewer should see. Links follow the
# newest version that has FINISHED publishing — the old copy keeps playing until
# then, so a correction never takes a link dark — and the superseded published
# copies are removed once the new version's set is complete. All of them, so a
# new source too small for 480p does not leave the old 480p on offer.

def _publish(x, version, profiles=("video-720p-vp9", "video-480p-vp9")):
    for p in profiles:
        x.jobs.request(T, "vid", version, p, "ann")
    while x.w.run_once():
        pass


def test_old_published_copies_stay_until_the_new_version_is_published():
    x = _world()
    _publish(x, "v1")
    assert {"v1-media.webm", "v1-media_sd.webm"} <= set(x.mf.renditions["vid"])
    x.jobs.request(T, "vid", "v2", "video-720p-vp9", "ann")
    x.jobs.request(T, "vid", "v2", "video-480p-vp9", "ann")
    x.w.run_once()                       # v2 720p done, v2 480p still queued
    left = set(x.mf.renditions["vid"])
    assert "v1-media.webm" in left and "v1-media_sd.webm" in left


def test_the_superseded_copies_go_once_the_new_set_is_complete():
    x = _world()
    _publish(x, "v1")
    _publish(x, "v2")
    left = set(x.mf.renditions["vid"])
    assert {"v2-media.webm", "v2-media_sd.webm"} <= left
    assert not {"v1-media.webm", "v1-media_sd.webm"} & left


def test_an_old_size_the_new_version_lacks_is_removed_too():
    x = _world()
    _publish(x, "v1")
    # v2's source is already standard definition: its 480p is skipped.
    x.w.encode_fn = _ok_encoder()
    def enc(profile, src, settings, **kw):
        if profile == "video-480p-vp9":
            return EncodeResult("skipped", detail="the source is already standard definition")
        return _ok_encoder()(profile, src, settings, **kw)
    x.w.encode_fn = enc
    _publish(x, "v2")
    left = set(x.mf.renditions["vid"])
    assert "v2-media.webm" in left
    assert "v1-media_sd.webm" not in left        # no stale SD left on offer


def test_a_failed_new_publish_keeps_the_old_copies_playing():
    x = _world()
    _publish(x, "v1")
    x.w.encode_fn = _ok_encoder(status="failed", detail="encoder crashed")
    _publish(x, "v2")
    left = set(x.mf.renditions["vid"])
    assert {"v1-media.webm", "v1-media_sd.webm"} <= left


def test_a_newer_version_is_never_removed_by_an_older_publish_finishing():
    x = _world()
    _publish(x, "v2")
    _publish(x, "v1")                    # a late retry of an older version
    assert {"v2-media.webm", "v2-media_sd.webm"} <= set(x.mf.renditions["vid"])
