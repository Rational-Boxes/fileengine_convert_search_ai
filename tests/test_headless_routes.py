"""The headless HTTP surface (HEADLESS_RESEARCH_API.md §3).

Through the real app and routing, with the core, store, cache, run store and
model replaced by fakes, signed in exactly as a desktop application is: an
http_bridge HS256 session token minted from a generated key (``skid``,
``skname``). Pinned here:

- authentication shadows the REST door; a key sent straight to csai is refused
  with a pointer to the bridge's token route;
- SSE event order (usage, plan, item…, usage, done) and the JSON alternative;
- usage headers on every response; 429 with Retry-After and the usage object;
- the run is recorded per item under the key, and listed per application;
- headless runs cannot be deleted from the conversation API;
- an approval answer from anyone but the run's owner is 404."""
import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.headless import usage as u
from convert_search_ai.store import TextRow

from test_headless_extract import DATASHEET, SCHEMA, Core, Gate, Model, Store, good_reply

SECRET = "test-jwt-secret"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def bridge_token(user="alice@x.com", tenant="default", skid="fesk_desk", skname="Reviera laptop"):
    h = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {"sub": user, "tenant": tenant, "exp": 4102444800,
              "roles": {tenant: ["users"]}, "amr": ["svc"]}
    if skid:
        claims.update(skid=skid, skname=skname)
    p = _b64(json.dumps(claims).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


class Runs:
    """In-memory RunStore."""

    def __init__(self):
        self.runs = {}

    def create(self, identity, *, title, request):
        rid = f"run{len(self.runs) + 1}"
        self.runs[rid] = {"user": identity.user, "key": identity.client_key_id,
                          "label": identity.client_label, "title": title,
                          "request": request, "results": [], "messages": []}
        return rid

    def add_result(self, identity, rid, item):
        self.runs[rid]["results"].append(item)

    def append(self, identity, rid, role, content, citations=None):
        self.runs[rid]["messages"].append((role, content))

    def finish(self, identity, rid, summary):
        self.runs[rid]["summary"] = summary

    def applications(self, tenant, user):
        apps = {}
        for rid, r in self.runs.items():
            if r["user"] == user:
                a = apps.setdefault(r["key"], {"client_key_id": r["key"], "label": r["label"],
                                               "runs": [], "run_count": 0, "last_run": ""})
                a["runs"].append({"id": rid, "title": r["title"]})
                a["run_count"] += 1
        return list(apps.values())

    def get(self, tenant, user, rid):
        r = self.runs.get(rid)
        return None if not r or r["user"] != user else {"id": rid, "results": r["results"]}

    def is_headless(self, tenant, user, rid):
        return rid in self.runs


class Chat:
    def __init__(self, model):
        self.chat = model

    def answer(self, identity, *, message, **kw):
        assert kw.get("allow_mcp") is False and kw.get("max_chars") == 20000
        from convert_search_ai.providers.base import report_usage
        report_usage(50, 10)
        yield {"type": "token", "text": "EI 60 "}
        yield {"type": "token", "text": "(file d1)"}
        yield {"type": "citations", "citations": [{"marker": 1, "kind": "doc", "file_uid": "d1"}]}


@pytest.fixture
def env():
    cfg = Config()
    cfg.jwt_secret = SECRET
    cfg.headless_requests_per_minute = 30
    core = Core()
    core.add("", "proj", "Project", folder=True)
    core.add("proj", "d1", "door-datasheet.pdf", "v1")
    store = Store()
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed")}
    model = Model(*[good_reply()] * 20)
    app = build_app(cfg)
    app.state.chat = Chat(model)
    app.state.permission_gate = Gate({"d1", "proj"})
    app.state.headless_usage_backend = u.MemoryBackend()
    app.state.headless_runs = Runs()
    app.state.headless_store = store
    app.state.headless_cache = None
    app.state.headless_client_for = lambda identity: core
    app.state.headless_convert = lambda uid, tenant: None
    from convert_search_ai.headless.consent_relay import ConsentRelay
    app.state.headless_relay = ConsentRelay(None)       # local delivery only

    class NoCache:
        def get(self, *a):
            return None

        def put(self, *a):
            return True
    app.state.headless_cache = NoCache()
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {bridge_token()}", "X-Tenant": "default"}
    return client, app, auth, model


def sse_events(text):
    out = []
    for block in text.split("\n\n"):
        lines = [l for l in block.split("\n") if l and not l.startswith(":")]
        if not lines:
            continue
        ev = next(l[7:] for l in lines if l.startswith("event: "))
        data = json.loads(next(l[6:] for l in lines if l.startswith("data: ")))
        out.append((ev, data))
    return out


def test_extract_streams_usage_plan_items_usage_done(env):
    client, app, auth, model = env
    r = client.post("/v1/extract", headers=auth, json={"folder_uid": "proj", "schema": SCHEMA})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no" and r.headers["x-usage-state"] == "ok"
    events = sse_events(r.text)
    assert [e for e, _ in events] == ["usage", "plan", "item", "usage", "done"]
    item = events[2][1]
    assert item["file_uid"] == "d1" and item["data"]["fire_rating"]["value"] == "EI 60"
    done = events[-1][1]
    assert done["run_id"] == "run1" and done["extracted"] == 1
    assert events[-2][1]["limits"]["tokens"]["used"] == 120
    run = app.state.headless_runs.runs["run1"]
    assert run["key"] == "fesk_desk" and run["label"] == "Reviera laptop"
    assert run["request"]["kind"] == "extract" and len(run["results"]) == 1


def test_extract_json_mode_returns_one_result(env):
    client, _, auth, _ = env
    r = client.post("/v1/extract", headers={**auth, "Accept": "application/json"},
                    json={"files": ["d1", "missing"], "schema": SCHEMA})
    assert r.status_code == 200 and r.headers["x-usage-state"] == "ok"
    body = r.json()
    assert body["plan"]["skipped"] == [{"file_uid": "missing", "reason": "not found or not readable"}]
    assert [i["file_uid"] for i in body["items"]] == ["d1", "missing"]
    assert body["summary"]["extracted"] == 1 and body["usage"]["state"] == "ok"


def test_bad_extract_request_is_400(env):
    client, _, auth, _ = env
    r = client.post("/v1/extract", headers=auth,
                    json={"folder_uid": "proj", "files": ["d1"], "schema": SCHEMA})
    assert r.status_code == 400 and "exactly one target" in r.json()["detail"]


def test_generated_key_sent_to_csai_is_refused_with_a_pointer(env):
    client, _, _, _ = env
    basic = base64.b64encode(b"fesk_desk:fesks_secret").decode()
    r = client.post("/v1/extract", headers={"Authorization": f"Basic {basic}"},
                    json={"files": ["d1"], "schema": SCHEMA})
    assert r.status_code == 401 and "/api/v1/auth/token" in r.json()["detail"]


def test_unauthenticated_is_401(env):
    client, _, _, _ = env
    assert client.get("/v1/usage").status_code == 401


def test_rate_limit_is_429_with_retry_after_and_usage(env):
    client, app, auth, _ = env
    app.state.config.headless_requests_per_minute = 2
    app.state.headless = None
    for _ in range(2):
        assert client.get("/v1/usage", headers=auth).status_code == 200
        client.post("/search", headers=auth, json={"query": "door"})
    r = client.post("/v1/extract", headers=auth, json={"files": ["d1"], "schema": SCHEMA})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "60" and r.headers["x-usage-state"] == "exceeded"
    body = r.json()
    assert body["kind"] == "rate_limited" and body["limit"] == "requests"
    assert body["usage"]["limits"]["requests"]["state"] == "exceeded"


def test_usage_endpoint_and_runs_listing_per_application(env):
    client, _, auth, _ = env
    client.post("/v1/extract", headers=auth, json={"files": ["d1"], "schema": SCHEMA})
    st = client.get("/v1/usage", headers=auth).json()
    assert st["subject"] == "key" and st["limits"]["tokens"]["used"] == 120
    apps = client.get("/v1/runs", headers=auth).json()["applications"]
    assert apps[0]["client_key_id"] == "fesk_desk" and apps[0]["label"] == "Reviera laptop"
    assert apps[0]["run_count"] == 1 and apps[0]["usage"]["limits"]["tokens"]["used"] == 120
    run = client.get(f"/v1/runs/{apps[0]['runs'][0]['id']}", headers=auth).json()
    assert run["results"][0]["file_uid"] == "d1"
    other = {"Authorization": f"Bearer {bridge_token(user='bob@x.com')}", "X-Tenant": "default"}
    assert client.get(f"/v1/runs/{apps[0]['runs'][0]['id']}", headers=other).status_code == 404


def test_headless_runs_cannot_be_deleted(env):
    client, _, auth, _ = env
    client.post("/v1/extract", headers=auth, json={"files": ["d1"], "schema": SCHEMA})
    r = client.delete("/conversations/run1", headers=auth)
    assert r.status_code == 409


def test_research_streams_tokens_and_a_result(env):
    client, app, auth, _ = env
    r = client.post("/v1/research", headers=auth, json={"message": "What is the fire rating?"})
    events = sse_events(r.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "usage" and kinds[-2:] == ["usage", "done"]
    assert kinds.count("token") == 2 and "citations" in kinds
    result = next(d for e, d in events if e == "result")
    assert result["answer"] == "EI 60 (file d1)" and result["tokens"] == 60
    run = app.state.headless_runs.runs["run1"]
    assert run["request"]["kind"] == "research"
    assert run["messages"] == [("assistant", "EI 60 (file d1)")]


def test_research_message_cap(env):
    client, _, auth, _ = env
    r = client.post("/v1/research", headers=auth, json={"message": "x" * 20001})
    assert r.status_code == 400


def test_consent_answer_for_an_unknown_request_is_404(env):
    client, _, auth, _ = env
    r = client.post("/v1/runs/run1/consent", headers=auth,
                    json={"id": "made-up", "decision": True})
    assert r.status_code == 404


def test_search_carries_usage_headers_for_a_key_session_only(env):
    client, app, auth, _ = env

    class S:
        def search(self, identity, q, **kw):
            assert kw["folder_uids"] == ["proj"] and kw["offset"] == 5
            return []
    app.state.search = S()
    r = client.post("/search", headers=auth,
                    json={"query": "door", "folder_uids": ["proj"], "offset": 5})
    assert r.status_code == 200 and r.json()["offset"] == 5
    assert r.headers["x-usage-state"] == "ok"
    person = {"Authorization": f"Bearer {bridge_token(skid='')}", "X-Tenant": "default"}
    r = client.post("/search", headers=person,
                    json={"query": "door", "folder_uids": ["proj"], "offset": 5})
    assert r.status_code == 200 and "x-usage-state" not in r.headers


class ToolChat(Chat):
    """A research answer whose model calls an external (MCP) tool, so the run
    must pause for the desktop user's approval."""

    def __init__(self, model):
        super().__init__(model)
        self.decision = None

    def answer(self, identity, *, message, **kw):
        assert kw["allow_mcp"] is True and callable(kw["consent"])
        from convert_search_ai.mcp_client import ConsentRequest
        req = ConsentRequest(integration="Jira", slug="jira", tool="create_issue",
                             tool_full="mcp__jira__create_issue", args_summary="title=Door")
        self.decision = kw["consent"](req)
        yield {"type": "token", "text": "approved" if self.decision else "declined"}
        yield {"type": "citations", "citations": []}


def _consent_flow(client, app, auth, answer_auth, decision=True):
    """Run a research request that pauses for approval; answer it from another
    thread as soon as it is pending (TestClient buffers a streamed body, so the
    answer cannot wait to read the event). Returns (events, consent status)."""
    import threading
    import time
    out = {}

    def answer():
        deadline = time.time() + 10
        while time.time() < deadline:
            relay = getattr(app.state, "headless", None) and app.state.headless.relay
            pending = dict(relay._pending) if relay else {}
            if pending:
                rid, (_broker, owner) = next(iter(pending.items()))
                resp = TestClient(app).post(f"/v1/runs/{owner.run_id}/consent",
                                            headers=answer_auth,
                                            json={"id": rid, "decision": decision})
                out["status"] = resp.status_code
                return
            time.sleep(0.02)

    t = threading.Thread(target=answer)
    t.start()
    r = client.post("/v1/research", headers=auth,
                    json={"message": "File an issue", "tools": {"mcp": "ask"}})
    t.join()
    return sse_events(r.text), out.get("status")


def test_mcp_approval_is_streamed_and_answered_by_the_runs_owner(env):
    client, app, auth, model = env
    app.state.chat = ToolChat(model)
    events, status = _consent_flow(client, app, auth, auth, decision=True)
    req = next(d for e, d in events if e == "tool_consent_request")
    assert req["integration"] == "Jira" and req["tool"] == "create_issue"
    assert len(req["id"]) >= 20 and not req["id"].startswith("consent-")   # random id
    assert status == 200
    assert next(d for e, d in events if e == "result")["answer"] == "approved"


def test_mcp_approval_from_another_key_is_404_and_the_call_is_denied(env):
    client, app, auth, model = env
    app.state.config.mcp_consent_timeout_ms = 1500
    app.state.chat = ToolChat(model)
    other_key = {"Authorization": f"Bearer {bridge_token(skid='fesk_other')}",
                 "X-Tenant": "default"}
    events, status = _consent_flow(client, app, auth, other_key, decision=True)
    assert status == 404
    assert next(d for e, d in events if e == "result")["answer"] == "declined"  # timed out
