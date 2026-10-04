"""PostgresMediaJobStore against a REAL Postgres (MEDIA_SHARE.md §4.4).

An in-memory double once passed every test while the real psycopg3 query
failed (`IN %s` with a tuple), so the store's SQL is exercised here directly.
Point it at a THROWAWAY database — tenant schemas are created and dropped:

  CSAI_MEDIA_LIVE_PG=1 CSAI_PG_PORT=5434 CSAI_PG_USER=postgres \
  CSAI_PG_PASSWORD=postgres CSAI_PG_DATABASE=csai_media_jobs_test pytest -m live ...
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("CSAI_MEDIA_LIVE_PG"),
                                 reason="set CSAI_MEDIA_LIVE_PG=1 and CSAI_PG_* for a throwaway DB")]


@pytest.fixture
def world():
    from convert_search_ai.config import Config
    from convert_search_ai.db import connect, provision_tenant
    from convert_search_ai.media_jobs import PostgresMediaJobStore
    config = Config()
    tenant = "mj" + uuid.uuid4().hex[:8]
    provision_tenant(config, tenant)
    yield PostgresMediaJobStore(config), tenant, config
    with connect(config) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "tenant_{tenant}" CASCADE')
        conn.commit()


def _sql(config, tenant, q, args=()):
    from convert_search_ai.db import connect_for_tenant
    with connect_for_tenant(config, tenant) as conn, conn.cursor() as cur:
        cur.execute(q, args)
        conn.commit()


def test_request_is_idempotent_and_a_new_version_is_a_new_job(world):
    s, t, _ = world
    a, ca = s.request(t, "f", "v1", "video-720p-vp9", "ann")
    b, cb = s.request(t, "f", "v1", "video-720p-vp9", "bob")
    c, cc = s.request(t, "f", "v2", "video-720p-vp9", "ann")
    assert ca and not cb and a.job_uid == b.job_uid and b.requested_by == "ann"
    assert cc and c.job_uid != a.job_uid
    assert len(s.for_file(t, "f")) == 2 and len(s.for_file(t, "f", "v1")) == 1


def test_claim_heartbeat_finish(world):
    s, t, _ = world
    j, _ = s.request(t, "f", "v1", "video-720p-vp9", "ann")
    got = s.claim(t)
    assert got.job_uid == j.job_uid and got.status == "running" and got.attempts == 1
    assert s.claim(t) is None
    assert s.heartbeat(t, j.job_uid, 40) == "running"
    assert s.heartbeat(t, j.job_uid, 10) == "running"     # progress never goes back
    assert s.for_file(t, "f")[0].progress_pct == 40
    done = s.finish(t, j.job_uid, "succeeded", rendition_name="v1-media.webm",
                    output_bytes=123, duration_ms=4500, width=1280, height=720,
                    encoder="libvpx-vp9")
    assert done.status == "succeeded" and done.progress_pct == 100 and done.finished_at
    assert s.finish(t, j.job_uid, "failed") is None        # only a running job finishes


def test_a_failed_job_restarts_on_request(world):
    s, t, _ = world
    j, _ = s.request(t, "f", "v1", "audio-mp3", "ann")
    s.claim(t)
    s.finish(t, j.job_uid, "failed", detail="boom")
    again, created = s.request(t, "f", "v1", "audio-mp3", "ann")
    assert created and again.status == "queued" and again.attempts == 0 and again.detail is None


def test_concurrent_claims_never_take_the_same_job(world):
    s, t, _ = world
    for p in ("video-720p-vp9", "video-480p-vp9"):
        s.request(t, "f", "v1", p, "ann")
    got, barrier = [], threading.Barrier(2)

    def take():
        barrier.wait()
        got.append(s.claim(t))
    ts = [threading.Thread(target=take) for _ in range(2)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    uids = [g.job_uid for g in got if g is not None]
    assert len(uids) == 2 and len(set(uids)) == 2


def test_stale_jobs_requeue_up_to_the_cap_then_fail(world):
    s, t, config = world
    j, _ = s.request(t, "f", "v1", "video-720p-vp9", "ann")
    for attempt in (1, 2, 3):
        claimed = s.claim(t)
        assert claimed.attempts == attempt
        _sql(config, t, "UPDATE media_jobs SET heartbeat_at = now() - interval '1 hour'")
        out = s.requeue_stale(t, 300, 3)
    assert out == {"requeued": [], "failed": [j.job_uid]}
    (job,) = s.for_file(t, "f")
    assert job.status == "failed" and job.detail == "repeatedly crashed"


def test_a_fresh_heartbeat_is_not_requeued(world):
    s, t, _ = world
    s.request(t, "f", "v1", "video-720p-vp9", "ann")
    s.claim(t)
    assert s.requeue_stale(t, 300, 3) == {"requeued": [], "failed": []}


def test_cancel_reaches_queued_and_running_and_the_worker_sees_it(world):
    s, t, _ = world
    a, _ = s.request(t, "f", "v1", "video-720p-vp9", "ann")
    s.request(t, "f", "v1", "video-480p-vp9", "ann")
    s.claim(t)
    assert len(s.cancel(t, "f")) == 2
    assert s.heartbeat(t, a.job_uid, 50) == "cancelled"
    assert s.finish(t, a.job_uid, "succeeded") is None     # a cancel is not overwritten


def test_tenants_with_jobs_finds_the_tenant(world):
    s, t, _ = world
    assert t in s.tenants_with_jobs()
