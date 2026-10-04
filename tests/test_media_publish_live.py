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

"""Media publishing end to end, against the REAL core (MEDIA_SHARE.md MS0–MS2).

Upload → ``POST /documents/{uid}/media`` → the media worker → renditions written
into the core → measured with ffprobe → a correction re-publishes and retires
the old set → and, when a bridge is reachable, the published rendition plays
through a playback ticket with a correct 206.

Every unit test in this area runs against doubles. This is the test that would
have caught the first-frame range defect: the pieces each passed alone.

Opt-in, because it writes to the core and needs a THROWAWAY CSAI database (the
tenant schema is provisioned there and dropped afterwards)::

  CSAI_MEDIA_LIVE_E2E=1 CSAI_PG_PORT=5434 CSAI_PG_USER=postgres \\
  CSAI_PG_PASSWORD=postgres CSAI_PG_DATABASE=csai_media_jobs_test \\
  FILEENGINE_SERVICE_TOKEN_FILE=~/temp/fileengine/service-tokens/csai \\
  [FE_USER=... FE_PASS=... BRIDGE_URL=http://localhost:8090] \\
  pytest -m live tests/test_media_publish_live.py

The agent acts on its SERVICE TOKEN, as the deployed worker does — the dev
agent has no LDAP password, which is why suites gated on one never run.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid

import pytest

TENANT = os.environ.get("FILEENGINE_CSAI_TENANT", "default")
CALLER = os.environ.get("CSAI_MEDIA_LIVE_USER", "testuser@rationalboxes.com")


def _blocker() -> str:
    if not os.environ.get("CSAI_MEDIA_LIVE_E2E"):
        return "set CSAI_MEDIA_LIVE_E2E=1 (writes to the core; needs a throwaway CSAI_PG_DATABASE)"
    if (os.environ.get("CSAI_PG_DATABASE") or "fileengine") == "fileengine":
        return "point CSAI_PG_DATABASE at a throwaway database, not the shared one"
    tok = os.path.expanduser(os.environ.get("FILEENGINE_SERVICE_TOKEN_FILE", ""))
    if not tok or not os.path.isfile(tok):
        return "FILEENGINE_SERVICE_TOKEN_FILE must name the csai service token"
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            return f"{exe} not found"
    try:
        from convert_search_ai.config import Config
        from convert_search_ai.core_client import agent_client
        agent_client(Config()).dir("", tenant=TENANT)
    except Exception as e:  # noqa: BLE001
        return f"core unavailable: {type(e).__name__}: {e}"
    try:
        from convert_search_ai import db
        from convert_search_ai.config import Config
        db.connect(Config()).close()
    except Exception as e:  # noqa: BLE001
        return f"Postgres unavailable: {type(e).__name__}"
    return ""


os.environ["FILEENGINE_SERVICE_TOKEN_FILE"] = os.path.expanduser(
    os.environ.get("FILEENGINE_SERVICE_TOKEN_FILE", ""))
_SKIP = _blocker()
pytestmark = [pytest.mark.live, pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "live")]


# --- helpers -----------------------------------------------------------------

def _make_clip(path: str, *, seconds: int, colour: str) -> None:
    """1080p MPEG-4 Part 2 + AAC: always encodable by a stock FFmpeg, and never
    'conformant', so the publish path must really transcode and scale it."""
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30,hue=h={colour}",
         "-f", "lavfi", "-i", "sine=frequency=330",
         "-t", str(seconds), "-c:v", "mpeg4", "-q:v", "4", "-c:a", "aac",
         "-shortest", path], check=True)


def _probe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", path],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _vstream(p: dict) -> dict:
    return next(s for s in p["streams"] if s["codec_type"] == "video")


def _children(mf, file_uid: str) -> dict:
    return {e.name: e.uid for e in (mf.dir(file_uid, tenant=TENANT) or [])}


def _fetch(mf, uid: str, dest: str) -> str:
    with open(dest, "wb") as fh:
        for chunk in mf.get_stream(uid, tenant=TENANT):
            fh.write(chunk)
    return dest


def _drain(worker, limit: int = 20) -> int:
    n = 0
    while n < limit and worker.run_once():
        n += 1
    return n


def _upload(mf, uid: str, path: str) -> None:
    def chunks():
        with open(path, "rb") as fh:
            while True:
                b = fh.read(1 << 20)
                if not b:
                    return
                yield b
    mf.put_stream(uid, chunks(), tenant=TENANT)


# --- the world ----------------------------------------------------------------

@pytest.fixture(scope="module")
def world():
    from fastapi.testclient import TestClient

    from convert_search_ai import db
    from convert_search_ai.app import build_app
    from convert_search_ai.config import Config
    from convert_search_ai.core_client import agent_client
    from convert_search_ai.ldap_auth import Identity
    from convert_search_ai.media_jobs import PostgresMediaJobStore
    from convert_search_ai.media_worker import MediaWorker
    from convert_search_ai.renditions import RenditionWriter
    from convert_search_ai.store import DocumentStore

    cfg = Config()
    schema = db.provision_tenant(cfg, TENANT)
    mf = agent_client(cfg)
    work = tempfile.mkdtemp(prefix="csai_media_e2e_")
    folder = mf.mkdir("", f"csai_media_e2e_{uuid.uuid4().hex[:8]}", tenant=TENANT)

    app = build_app(cfg)
    tok = app.state.token_store.issue(Identity(
        user=CALLER, roles=["users", "administrators"], tenant=TENANT, authenticated=True))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {tok}", "X-Tenant": TENANT}
    worker = MediaWorker(cfg, PostgresMediaJobStore(cfg), mf, DocumentStore(cfg),
                         RenditionWriter(mf))
    try:
        yield {"cfg": cfg, "mf": mf, "dir": folder, "work": work, "c": client,
               "h": headers, "worker": worker}
    finally:
        try:
            mf.remove(folder, tenant=TENANT)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(work, ignore_errors=True)
        with db.connect(cfg) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()


@pytest.fixture(scope="module")
def published(world):
    """A 1080p clip uploaded and published once; the state every test reads."""
    mf, w = world["mf"], world
    src = os.path.join(w["work"], "v1.mp4")
    _make_clip(src, seconds=4, colour=0)
    uid = mf.touch(w["dir"], "intro.mp4", tenant=TENANT)
    _upload(mf, uid, src)
    r = w["c"].post(f"/documents/{uid}/media", headers=w["h"], json={})
    assert r.status_code == 202, r.text
    first = r.json()
    again = w["c"].post(f"/documents/{uid}/media", headers=w["h"], json={}).json()
    ran = _drain(w["worker"])
    state = w["c"].get(f"/documents/{uid}/media", headers=w["h"]).json()
    return {"uid": uid, "src": src, "first": first, "again": again, "ran": ran,
            "state": state, "version": first["source_version"]}


# --- the publish ----------------------------------------------------------------

def test_publishing_queues_the_three_video_profiles_once(published):
    jobs = published["first"]["jobs"]
    assert [j["profile"] for j in jobs] == [
        "video-720p-vp9", "video-480p-vp9", "video-emailposter"]
    assert all(j["created"] and j["status"] == "queued" for j in jobs)
    assert published["first"]["source_version"]          # the core's real version
    # Asking twice is the same jobs, not a second encode.
    assert [j["job_uid"] for j in published["again"]["jobs"]] == [j["job_uid"] for j in jobs]
    assert not any(j["created"] for j in published["again"]["jobs"])
    assert published["ran"] == 3


def test_every_job_succeeded_and_the_state_lists_the_published_set(published):
    st = published["state"]
    assert {j["profile"]: j["status"] for j in st["jobs"]} == {
        "video-720p-vp9": "succeeded", "video-480p-vp9": "succeeded",
        "video-emailposter": "succeeded"}, st["jobs"]
    from convert_search_ai.renditions import parse_rendition_name
    parsed = [parse_rendition_name(n) for n in st["renditions"]]
    assert all(parsed), st["renditions"]
    assert len({p[0] for p in parsed}) == 1          # one version's set
    assert sorted(p[1] for p in parsed) == ["emailposter", "media", "media_sd"]


def test_the_720p_rendition_is_vp9_opus_scaled_and_capped(world, published):
    mf, cfg = world["mf"], world["cfg"]
    kids = _children(mf, published["uid"])
    name = next(n for n in kids if n.endswith("-media.webm"))
    p = _probe(_fetch(mf, kids[name], os.path.join(world["work"], "m.webm")))
    v = _vstream(p)
    assert v["codec_name"] == "vp9"
    assert (int(v["width"]), int(v["height"])) == (1280, 720)
    assert any(s["codec_type"] == "audio" and s["codec_name"] == "opus" for s in p["streams"])
    assert abs(float(p["format"]["duration"]) - 4.0) < 0.5
    from convert_search_ai.media_encode import MediaSettings, bitrate_bps
    cap = bitrate_bps(MediaSettings.from_config(cfg).video_max_bitrate)
    # Container bitrate includes audio and mux overhead; the video cap is the bound.
    assert int(p["format"]["bit_rate"]) < cap * 1.25


def test_the_480p_rendition_is_480p(world, published):
    mf = world["mf"]
    kids = _children(mf, published["uid"])
    name = next(n for n in kids if n.endswith("-media_sd.webm"))
    v = _vstream(_probe(_fetch(mf, kids[name], os.path.join(world["work"], "sd.webm"))))
    assert v["codec_name"] == "vp9" and int(v["height"]) == 480


def test_the_email_poster_is_a_small_gif_at_the_configured_width(world, published):
    mf, cfg = world["mf"], world["cfg"]
    from convert_search_ai.media_encode import MediaSettings
    s = MediaSettings.from_config(cfg)
    kids = _children(mf, published["uid"])
    name = next(n for n in kids if n.endswith("-emailposter.gif"))
    path = _fetch(mf, kids[name], os.path.join(world["work"], "p.gif"))
    assert os.path.getsize(path) <= s.gif_max_bytes
    v = _vstream(_probe(path))
    assert v["codec_name"] == "gif" and int(v["width"]) <= s.gif_width


def test_the_source_is_untouched(world, published):
    path = _fetch(world["mf"], published["uid"], os.path.join(world["work"], "back.mp4"))
    with open(path, "rb") as a, open(published["src"], "rb") as b:
        assert a.read() == b.read()


# --- playback through the bridge (the drawer's path) ------------------------------

def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read() or b"{}")


def _bridge_login():
    """A session from the bridge, completing an email 2FA challenge through
    MailHog when the tenant requires one — the same shape as the bridge's
    tests/lib_login.sh, since a fixture user may be enrolled."""
    base = os.environ.get("BRIDGE_URL", "http://localhost:8090")
    mailhog = os.environ.get("MAILHOG_URL", "http://localhost:8025")
    user, pw = os.environ.get("FE_USER"), os.environ.get("FE_PASS")
    if not user or not pw:
        pytest.skip("set FE_USER / FE_PASS to check playback through the bridge")
    req = urllib.request.Request(f"{base}/v1/auth/token", method="POST", headers={
        "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
        "X-Tenant": TENANT})
    try:
        body = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"bridge unreachable at {base}: {e}")
    if body.get("token"):
        return base, body["token"]
    mfa = body.get("mfa_token")
    assert mfa, f"no session and no 2FA challenge: {body}"
    import quopri
    import re
    import time
    urllib.request.urlopen(urllib.request.Request(f"{mailhog}/api/v1/messages",
                                                  method="DELETE"), timeout=10)
    _post_json(f"{base}/v1/auth/2fa", {"mfa_token": mfa, "action": "send", "method": "email"})
    code = ""
    for _ in range(10):
        time.sleep(1)
        items = json.loads(urllib.request.urlopen(f"{mailhog}/api/v2/messages",
                                                  timeout=10).read()).get("items", [])
        if items:
            text = quopri.decodestring(items[0]["Content"]["Body"]).decode("utf-8", "ignore")
            code = (re.findall(r"\b(\d{6})\b", text) or [""])[0]
            if code:
                break
    assert code, "no 2FA code arrived in MailHog"
    done = _post_json(f"{base}/v1/auth/2fa", {"mfa_token": mfa, "method": "email", "code": code})
    assert done.get("token"), f"2FA completion gave no session: {done}"
    return base, done["token"]


def test_the_published_rendition_streams_through_a_playback_ticket(world, published):
    base, tok = _bridge_login()
    kids = _children(world["mf"], published["uid"])
    name = next(n for n in kids if n.endswith("-media.webm"))
    rend = kids[name]
    req = urllib.request.Request(f"{base}/v1/files/{rend}/playback-ticket", method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "X-Tenant": TENANT})
    ticket = json.loads(urllib.request.urlopen(req, timeout=20).read())["ticket"]
    req = urllib.request.Request(f"{base}/v1/files/{rend}/content?ticket={ticket}",
                                 headers={"Range": "bytes=0-1023"})
    resp = urllib.request.urlopen(req, timeout=20)
    total = int(world["mf"].stat(rend, tenant=TENANT).size)
    assert resp.status == 206
    assert resp.headers["Content-Type"] == "video/webm"
    assert resp.headers["Content-Range"] == f"bytes 0-1023/{total}"
    assert len(resp.read()) == 1024
    # The ticket names the rendition; it opens nothing else, not even the source.
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{base}/v1/files/{published['uid']}/content?ticket={ticket}",
                               timeout=20)
    assert e.value.code == 401
    urllib.request.urlopen(urllib.request.Request(
        f"{base}/v1/auth/token", method="DELETE",
        headers={"Authorization": f"Bearer {tok}"}), timeout=20)


# --- unpublish is fail-safe until share_service answers ------------------------------

def test_unpublishing_a_finished_set_is_refused_and_removes_nothing(world, published):
    before = set(_children(world["mf"], published["uid"]))
    r = world["c"].delete(f"/documents/{published['uid']}/media", headers=world["h"])
    assert r.status_code == 409
    assert set(_children(world["mf"], published["uid"])) == before


# --- a correction (Q14, revised): the newest version that finished publishing ---------

def test_a_new_version_publishes_and_retires_the_old_set(world, published):
    mf, w = world["mf"], world
    src2 = os.path.join(w["work"], "v2.mp4")
    _make_clip(src2, seconds=4, colour=120)
    _upload(mf, published["uid"], src2)
    st = w["c"].get(f"/documents/{published['uid']}/media", headers=w["h"]).json()
    v2 = st["source_version"]
    assert v2 and v2 != published["version"]
    assert st["jobs"] == []                      # nothing published for v2 yet...
    old = [n for n in _children(mf, published["uid"]) if n.endswith(("-media.webm", "-media_sd.webm"))]
    assert len(old) == 2                         # ...and v1's set is still serving

    r = w["c"].post(f"/documents/{published['uid']}/media", headers=w["h"], json={})
    assert r.status_code == 202 and all(j["created"] for j in r.json()["jobs"])
    assert _drain(w["worker"]) == 3

    st = w["c"].get(f"/documents/{published['uid']}/media", headers=w["h"]).json()
    assert all(j["status"] == "succeeded" for j in st["jobs"]), st["jobs"]
    from convert_search_ai.renditions import PUBLISHED_FMTS, parse_rendition_name
    published_now = [parse_rendition_name(n) for n in _children(mf, published["uid"])]
    published_now = [p for p in published_now if p and p[1] in PUBLISHED_FMTS]
    versions = {p[0] for p in published_now}
    assert len(versions) == 1, f"superseded published copies were not retired: {published_now}"
    assert sorted(p[1] for p in published_now) == ["emailposter", "media", "media_sd"]


# --- the length rule (Q16) ---------------------------------------------------------

def test_a_source_over_the_length_limit_is_refused_with_the_publish_elsewhere_pointer(world):
    mf, w = world["mf"], world
    src = os.path.join(w["work"], "long.mp4")
    _make_clip(src, seconds=4, colour=240)
    uid = mf.touch(w["dir"], "long.mp4", tenant=TENANT)
    _upload(mf, uid, src)
    r = w["c"].post(f"/documents/{uid}/media", headers=w["h"],
                    json={"profile": "video-480p-vp9"})
    assert r.status_code == 202
    # The worker reads its encode settings once, at start — as deployed.
    import dataclasses
    old = w["worker"].settings
    w["worker"].settings = dataclasses.replace(old, max_duration_seconds=2)
    try:
        _drain(w["worker"])
    finally:
        w["worker"].settings = old
    job = w["c"].get(f"/documents/{uid}/media", headers=w["h"]).json()["jobs"][0]
    assert job["status"] in ("failed", "skipped")
    assert "PeerTube" in (job["detail"] or "")
    assert not any(n.endswith("-media_sd.webm") for n in _children(mf, uid))
