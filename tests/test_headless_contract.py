"""Contract gaps the desktop simulator (tests/desktop_sim) found, pinned here.

- A folder with more documents than one run reads says so in the plan
  (``truncated``, ``limit``) instead of being cut short silently.
- A run that moves the key into warning or exceeded sends a ``usage`` event
  when it happens, not only at the end (§3.8).
- An MCP approval is accepted only from the run's own user and key, for an id
  issued to that run, once; anything else is refused (404) on every instance,
  not reported as accepted because some instance was subscribed (§3.9).
- The approval request carries what the tool would be sent (``args``) and an
  absolute ``expires_at`` (§3.9).
- The SSE response cancels its run as soon as the response ends, so a hang-up
  stops the work (and the model) at once.
"""
import asyncio
import calendar
import json
import threading
import time

from convert_search_ai.consent import ConsentBroker
from convert_search_ai.headless import usage as u
from convert_search_ai.headless.consent_relay import ConsentRelay
from convert_search_ai.headless.sse import EventRun, EventStreamResponse
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.mcp_client import ConsentRequest
from convert_search_ai.store import TextRow

from test_headless_extract import (DATASHEET, ID, Cfg, Core, Gate, Model, Store, good_reply,
                                   req)
from convert_search_ai.headless import extract as ex


# ------------------------------------------------------------------ plan

def _folder(n):
    core = Core()
    core.add("", "f", "Folder", folder=True)
    for i in range(n):
        core.add("f", f"d{i}", f"sheet-{i}.md", version=f"v{i}")
    return core


def _extractor(core, model=None, meter=None, cap=3):
    class C(Cfg):
        headless_files_per_run = cap
    store = Store()
    store.rows = {uid: TextRow(DATASHEET, info.version, "indexed")
                  for uid, info in core.info.items() if not info.is_dir}
    e = ex.Extractor(C(), client_for=lambda i: core, gate=Gate(set(core.info)), store=store,
                     chat=model or Model(*[good_reply()] * 10), meter=meter)
    return e


def _run(e, r):
    events = []
    summary = e.run(ID, r, lambda ev, d: events.append((ev, d)))
    return events, summary


def test_a_folder_over_the_cap_is_reported_as_truncated():
    events, summary = _run(_extractor(_folder(4)), req(folder_uid="f"))
    plan = dict(events)["plan"]
    assert len(plan["files"]) == 3
    assert plan["truncated"] is True and plan["limit"] == 3
    assert summary["truncated"] is True


def test_a_folder_at_the_cap_is_not_truncated():
    events, summary = _run(_extractor(_folder(3)), req(folder_uid="f"))
    plan = dict(events)["plan"]
    assert len(plan["files"]) == 3 and plan["truncated"] is False and "limit" not in plan
    assert summary["truncated"] is False


# ------------------------------------------------------------------ usage mid-run

def test_usage_event_when_the_state_changes_mid_run():
    # 140 tokens a day; each file spends 120: warning after the first, exceeded
    # after the second.
    meter = u.UsageMeter(u.Limits(60, 140, 2, 50), u.MemoryBackend())
    events, _ = _run(_extractor(_folder(2), meter=meter), req(folder_uid="f"))
    seq = [(ev, d.get("state")) for ev, d in events if ev in ("item", "usage")]
    assert seq == [("item", None), ("usage", "warning"), ("item", None), ("usage", "exceeded")]


def test_no_usage_event_while_the_state_holds():
    meter = u.UsageMeter(u.Limits(60, 1_000_000, 2, 50), u.MemoryBackend())
    events, _ = _run(_extractor(_folder(2), meter=meter), req(folder_uid="f"))
    assert [ev for ev, _ in events if ev == "usage"] == []


# ------------------------------------------------------------------ approvals

class FakeRedis:
    """The calls ConsentRelay makes on the answer side, shared by instances."""

    def __init__(self):
        self.kv, self.published = {}, []

    def set(self, k, v, ex=None):
        self.kv[k] = v

    def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)

    def eval(self, script, nkeys, key, owner):
        if self.kv.get(key) == owner:
            del self.kv[key]
            return 1
        return 0

    def publish(self, channel, msg):
        self.published.append((channel, json.loads(msg)))
        return 1        # some instance is always subscribed (the run's own)


class Broker:
    def __init__(self):
        self.resolved = []

    def resolve(self, rid, decision, remember=False):
        self.resolved.append((rid, decision, remember))


OWNER = Identity(user="alice", tenant="t", authenticated=True, client_key_id="fesk_a")
OTHER_KEY = Identity(user="alice", tenant="t", authenticated=True, client_key_id="fesk_b")
OTHER_USER = Identity(user="bob", tenant="t", authenticated=True, client_key_id="fesk_a")


def test_approval_on_another_instance_only_from_the_owner_and_once():
    r = FakeRedis()
    running, elsewhere = ConsentRelay(lambda: r), ConsentRelay(lambda: r)
    broker = Broker()
    running.register("rid1", broker, OWNER, "run1", ttl_s=10)
    assert not elsewhere.answer(OTHER_KEY, "run1", "rid1", True, False)
    assert not elsewhere.answer(OTHER_USER, "run1", "rid1", True, False)
    assert not elsewhere.answer(OWNER, "run2", "rid1", True, False)       # wrong run
    assert not elsewhere.answer(OWNER, "run1", "never-issued", True, False)
    assert r.published == []                                              # nothing relayed
    assert elsewhere.answer(OWNER, "run1", "rid1", False, False)
    assert len(r.published) == 1 and r.published[0][1]["decision"] is False
    assert not elsewhere.answer(OWNER, "run1", "rid1", True, False)       # single use


def test_approval_on_the_running_instance_refuses_strangers_without_relaying():
    r = FakeRedis()
    relay = ConsentRelay(lambda: r)
    broker = Broker()
    relay.register("rid1", broker, OWNER, "run1", ttl_s=10)
    assert not relay.answer(OTHER_KEY, "run1", "rid1", True, False)
    assert r.published == [] and broker.resolved == []
    assert relay.answer(OWNER, "run1", "rid1", True, True)
    assert broker.resolved == [("rid1", True, True)]
    assert "csai:consent:pending:rid1" not in r.kv                        # claimed
    assert not relay.answer(OWNER, "run1", "rid1", True, False)


def test_forgetting_a_run_drops_its_pending_records():
    r = FakeRedis()
    relay = ConsentRelay(lambda: r)
    relay.register("rid1", Broker(), OWNER, "run1", ttl_s=10)
    relay.forget_run("run1")
    assert r.kv == {}
    assert not ConsentRelay(lambda: r).answer(OWNER, "run1", "rid1", True, False)


def test_approval_request_carries_args_and_an_absolute_expiry():
    events = []
    broker = ConsentBroker(events.append, new_id=lambda: "rid", timeout_s=30)
    req_ = ConsentRequest(integration="Jira", slug="jira", tool="create_issue",
                          tool_full="mcp__jira__create_issue", args_summary="title=x",
                          args={"title": "x", "body": "long text"})
    t = threading.Thread(target=lambda: broker.request(req_))
    t.start()
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.01)
    broker.resolve("rid", False)
    t.join(5)
    ev = events[0]
    assert ev["args"] == {"title": "x", "body": "long text"}
    assert ev["expires_in"] == 30
    expires = calendar.timegm(time.strptime(ev["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    assert abs(expires - (time.time() + 30)) < 5


# ------------------------------------------------------------------ hang-up

def test_the_run_is_cancelled_when_the_client_hangs_up():
    """The send fails (the client is gone): the run must be cancelled then, not
    when the abandoned body is finalised."""
    produced = []

    def producer(emit, cancelled):
        while not cancelled() and len(produced) < 500:
            produced.append(1)
            emit("token", {"text": "x"})
            time.sleep(0.01)
        return {}

    run = EventRun(producer).start()
    resp = EventStreamResponse(run)
    sent = []

    async def send(msg):
        sent.append(msg)
        if len(sent) > 3:
            raise OSError("client went away")

    async def receive():
        await asyncio.sleep(3600)

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    try:
        asyncio.run(resp(scope, receive, send))
    except Exception:
        pass
    assert run.cancelled()
    time.sleep(0.2)
    n = len(produced)
    time.sleep(0.3)
    assert len(produced) == n < 500          # the producer stopped


# ------------------------------------------------------------------ Redis down

def test_an_unverifiable_session_is_503_not_401(monkeypatch):
    """Redis down: revocation is unknown and fails closed. The desktop must be
    told to retry (503), not that its session is invalid (401); a revoked token
    stays 401."""
    import jwt as pyjwt
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from convert_search_ai import token_revocation
    from convert_search_ai.api import _identity
    from convert_search_ai.app import build_app
    from convert_search_ai.bridge_auth import BridgeTokenVerifier

    secret = "s" * 40
    token = pyjwt.encode({"sub": "alice", "user": "alice", "tenant": "default", "jti": "j1",
                          "exp": int(time.time()) + 600}, secret, algorithm="HS256")
    state = {"v": token_revocation.UNAVAILABLE}
    monkeypatch.setattr(token_revocation, "verdict", lambda jti: state["v"])
    app = build_app()
    app.state.bridge_verifier = BridgeTokenVerifier("", jwt_secret=secret)

    @app.get("/_probe")
    def probe(identity=Depends(_identity)):
        return {"user": identity.user}

    c = TestClient(app)
    h = {"Authorization": "Bearer " + token}
    r = c.get("/_probe", headers=h)
    assert r.status_code == 503 and r.json()["kind"] == "auth_unavailable"
    assert r.headers["Retry-After"]
    state["v"] = token_revocation.REVOKED
    assert c.get("/_probe", headers=h).status_code == 401
    state["v"] = token_revocation.PERMIT
    assert c.get("/_probe", headers=h).json() == {"user": "alice"}
