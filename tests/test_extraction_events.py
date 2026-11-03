"""Extraction events and automation jobs (EXTRACTION_PROMPT_SETS.md §6.0, §6.1).

- ``extraction.complete`` once per newly written entry, naming the sidecar and
  the entry; never a value, quote or field name on the bus; none for a cache
  hit or a builder test; ``extraction.failed`` with a reason for an attempt
  that produced nothing;
- jobs: only a service account may queue; the worker acks a finished job and
  leaves a deferred one; a job over the automation budget waits, then fails;
  a job for a retired set fails at once;
- ``GET /v1/documents/{uid}/extractions`` returns the entries with what they
  mean, READ-checked."""
import json
import time

import pytest

from convert_search_ai.headless.events import ExtractionEvents
from convert_search_ai.headless.jobs import JobRunner, JobWorker, automation_identity
from convert_search_ai.headless.promptset_store import MemoryPromptSetStore

from test_headless_extract import SCHEMA, Model, good_reply
from test_headless_routes import SECRET, _b64, env, sse_events  # noqa: F401 (fixture)
from test_markup_routes import FakeMarkupStore, datasheet_pdf
from test_promptset_routes import SET


class Cache:
    def __init__(self):
        self.store = {}

    def get(self, file_uid, version, tenant, key):
        return self.store.get((file_uid, version), {}).get(key)

    def put(self, file_uid, version, tenant, key, entry):
        self.store.setdefault((file_uid, version), {})[key] = entry
        return f"sidecar-{file_uid}"

    def entries(self, file_uid, version, tenant):
        return dict(self.store.get((file_uid, version), {}))


@pytest.fixture
def ctx(env):
    client, app, auth, model = env
    events = []
    app.state.promptset_store = MemoryPromptSetStore()
    app.state.promptsets = None
    app.state.headless_events = ExtractionEvents(publish=events.append)
    app.state.headless_markup = FakeMarkupStore(datasheet_pdf())
    cache = Cache()
    app.state.headless_cache = cache
    app.state.headless = None
    return client, app, auth, model, events, cache


def _publish(client, auth, slug="reviera.door"):
    return client.put(f"/v1/prompt-sets/slug/{slug}", headers=auth, json=SET).json()["id"]


def _extract(client, auth, body):
    r = client.post("/v1/extract", headers={**auth, "Accept": "application/json"}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_written_entry_is_announced_without_values(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    out = _extract(client, auth, {"files": ["d1"], "prompt_set": sid})
    (e,) = events
    assert e["type"] == "extraction.complete" and e["source"] == "headless"
    assert e["file_uid"] == "d1" and e["version"] == "v1" and e["actor"] == "fesk_desk"
    assert e["values_present"] is True
    assert e["counts"] == {"fields": 3, "found": 2, "not_found": 1, "dropped": 0}
    assert e["prompt_set"] == {"id": sid, "name": "Door datasheet", "revision": 1}
    assert e["payload"]["name"] == "v1-extract.json" and e["payload"]["uid"] == "sidecar-d1"
    assert e["payload"]["entry"] in cache.store[("d1", "v1")]
    assert e["payload"]["schema"] == "fileengine.extract_cache.v1"
    assert e["markup"]["status"] == "written" and e["run_id"] == out["run_id"]
    text = json.dumps(e)
    for leaked in ("EI 60", "926", "Fire resistance", "fire_rating", "width_mm"):
        assert leaked not in text


def test_a_cache_hit_announces_nothing_and_a_raw_schema_run_announces_without_a_set(ctx):
    client, app, auth, model, events, cache = ctx
    _extract(client, auth, {"files": ["d1"], "schema": SCHEMA})
    _extract(client, auth, {"files": ["d1"], "schema": SCHEMA})          # cache hit
    (e,) = events
    assert e["prompt_set"] is None and e["values_present"] is True
    entry = cache.store[("d1", "v1")][e["payload"]["entry"]]
    assert entry["schema"] == SCHEMA and entry["prompt_set"] is None    # what the values mean


def test_nothing_found_is_still_a_result(ctx):
    client, app, auth, model, events, cache = ctx
    nothing = {k: {"found": False, "value": None, "unit": None, "evidence": None}
               for k in ("fire_rating", "width_mm", "epd_number")}
    model.replies[:] = [nothing] * 5
    _extract(client, auth, {"files": ["d1"], "schema": SCHEMA})
    (e,) = events
    assert e["type"] == "extraction.complete" and e["values_present"] is False


def test_an_unusable_reply_is_announced_as_failed(ctx):
    client, app, auth, model, events, cache = ctx
    model.replies[:] = ["not json"] * 5
    _extract(client, auth, {"files": ["d1"], "schema": SCHEMA})
    (e,) = events
    assert e["type"] == "extraction.failed" and e["reason"] == "model_error"
    assert cache.store == {}


def test_a_builder_test_announces_nothing(ctx):
    client, app, auth, model, events, cache = ctx
    from test_promptset_routes import _editor_headers
    client.post("/v1/prompt-sets/test", headers={**_editor_headers(), "Accept": "application/json"},
                json={"set": SET, "file_uid": "d1"})
    assert events == []


# ------------------------------------------------------------------ jobs

def _service_headers(roles=("users", "file_services"), user="svc-fa@x.com"):
    import hashlib
    import hmac
    h = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = _b64(json.dumps({"sub": user, "tenant": "default", "exp": 4102444800,
                         "roles": {"default": list(roles)}}).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return {"Authorization": f"Bearer {h}.{p}.{sig}", "X-Tenant": "default"}


class Queue:
    def __init__(self):
        self.jobs, self.pending, self.acked = [], {}, []

    def enqueue(self, job):
        job = {**job, "job_id": f"xj_{len(self.jobs) + 1}", "enqueued_at": time.time()}
        self.jobs.append(job)
        self.pending[job["job_id"]] = job
        return job["job_id"]

    def read(self, consumer, block_ms=0):
        out = [(j["job_id"], j) for j in self.jobs if j["job_id"] in self.pending
               and not j.get("_seen")]
        for _, j in out:
            j["_seen"] = True
        return out

    def claim(self, consumer, min_idle_ms):
        """Like XAUTOCLAIM: only entries idle past the retry delay, which the
        test marks by calling idle()."""
        return [(k, j) for k, j in self.pending.items() if j.get("_idle")]

    def idle(self):
        for j in self.pending.values():
            j["_idle"] = True

    def ack(self, msg_id):
        self.pending.pop(msg_id, None)
        self.acked.append(msg_id)


def test_only_a_service_account_may_queue_a_job(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    q = Queue()
    app.state.headless_jobs = q
    app.state.headless = None
    body = {"file_uid": "d1", "version": "v1", "prompt_set": sid}
    assert client.post("/v1/extract/jobs", headers=auth, json=body).status_code == 403
    assert client.post("/v1/extract/jobs", headers=_service_headers(roles=("users",)),
                       json=body).status_code == 403
    r = client.post("/v1/extract/jobs", headers=_service_headers(), json=body)
    assert r.status_code == 202 and r.json()["job_id"] == "xj_1"
    assert q.jobs[0]["user"] == "svc-fa@x.com" and "file_services" in q.jobs[0]["roles"]
    r = client.post("/v1/extract/jobs", headers=_service_headers(),
                    json={**body, "prompt_set": "ps_nope"})
    assert r.status_code == 404


def test_the_worker_runs_a_job_as_automation_and_announces_it(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    q = Queue()
    app.state.headless_jobs = q
    app.state.headless = None
    client.post("/v1/extract/jobs", headers=_service_headers(),
                json={"file_uid": "d1", "version": "v1", "prompt_set": sid, "binding_id": "b1"})
    # the job runs as the service account, which can read d1 in the fake gate
    w = JobWorker(app, q, consumer="t")
    assert w.step(block_ms=0) == 1
    assert q.pending == {} and q.acked == ["xj_1"]
    (e,) = events
    assert e["source"] == "automation" and e["job_id"] == "xj_1" and e["actor"] == "auto"
    meter = app.state.headless.meter
    used = meter.status(automation_identity("svc-fa@x.com", "default", []))["limits"]["tokens"]["used"]
    assert used > 0                                               # the tenant's automation budget


def test_a_job_over_budget_waits_then_fails(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    from convert_search_ai.headless import routes as hr
    s = hr._svc(app)
    s.meter.tokens_exhausted = lambda ident: True
    runner = JobRunner(app)
    job = {"job_id": "xj_9", "tenant": "default", "user": "svc", "roles": ["file_services"],
           "file_uid": "d1", "version": "v1", "prompt_set": sid, "enqueued_at": time.time()}
    assert runner.run(job) is False and events == []              # left for a retry
    job["enqueued_at"] = time.time() - 49 * 3600
    assert runner.run(job) is True
    (e,) = events
    assert e["type"] == "extraction.failed" and e["reason"] == "budget"


def test_a_job_for_a_retired_set_fails_at_once(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    client.post("/v1/prompt-sets/slug/reviera.door/retire", headers=auth)
    job = {"job_id": "xj_1", "tenant": "default", "user": "svc", "roles": ["file_services"],
           "file_uid": "d1", "version": "v1", "prompt_set": sid, "enqueued_at": time.time()}
    assert JobRunner(app).run(job) is True
    (e,) = events
    assert e["type"] == "extraction.failed" and e["reason"] == "prompt_set_retired"


# ------------------------------------------------------------------ reading values

def test_the_extractions_of_a_document(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    _extract(client, auth, {"files": ["d1"], "prompt_set": sid})
    _extract(client, auth, {"files": ["d1"], "schema": SCHEMA})
    r = client.get("/v1/documents/d1/extractions", headers=auth, params={"version": "v1"})
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    by_set = {bool(e["prompt_set"]): e for e in entries}
    assert by_set[True]["prompt_set"]["name"] == "Door datasheet"
    assert [f["name"] for f in by_set[True]["fields"]] == ["fire_rating", "width_mm", "epd_number"]
    assert by_set[True]["data"]["fire_rating"]["value"] == "EI 60"
    assert by_set[False]["schema"] == SCHEMA
    one = client.get("/v1/documents/d1/extractions", headers=auth,
                     params={"version": "v1", "entry": events[0]["payload"]["entry"]}).json()
    assert len(one["entries"]) == 1
    assert client.get("/v1/documents/secret/extractions", headers=auth,
                      params={"version": "v1"}).status_code == 404


def test_an_entry_names_its_marked_up_pdf_and_where_each_value_is(ctx):
    """The preview's extraction view: the PDF to show, the page to jump to
    and the colour each value is marked in."""
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    _extract(client, auth, {"files": ["d1"], "prompt_set": sid})
    (e,) = client.get("/v1/documents/d1/extractions", headers=auth,
                      params={"version": "v1"}).json()["entries"]
    m = e["markup"]
    assert m["name"] == f"v1-extractpdf_{sid[3:]}.pdf" and m["uid"]
    assert m["fields"]["fire_rating"]["located"] is True and m["fields"]["fire_rating"]["page"] >= 1
    assert m["fields"]["epd_number"] == {"located": False, "reason": "not_found"}
    assert set(m["colours"]) == {"fire_rating", "width_mm", "epd_number"}
    stored = cache.store[("d1", "v1")][events[0]["payload"]["entry"]]["markup"]
    assert "uid" not in stored and stored["fields"] == m["fields"]


def test_a_pruned_or_unwritten_pdf_is_not_offered(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    _extract(client, auth, {"files": ["d1"], "prompt_set": sid})
    app.state.headless_markup.files.clear()                      # pruned with its version
    (e,) = client.get("/v1/documents/d1/extractions", headers=auth,
                      params={"version": "v1"}).json()["entries"]
    assert e["markup"] is None and e["data"]["fire_rating"]["value"] == "EI 60"


def test_an_older_entry_still_finds_its_pdf_without_pages(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    _extract(client, auth, {"files": ["d1"], "prompt_set": sid})
    for entry in cache.store[("d1", "v1")].values():
        entry.pop("markup")                                       # written before 1.9.72
    (e,) = client.get("/v1/documents/d1/extractions", headers=auth,
                      params={"version": "v1"}).json()["entries"]
    assert e["markup"]["fields"] == {} and list(e["markup"]["colours"]) == [
        "fire_rating", "width_mm", "epd_number"]


def test_a_deferred_job_is_claimed_again_and_runs(ctx):
    client, app, auth, model, events, cache = ctx
    sid = _publish(client, auth)
    q = Queue()
    app.state.headless_jobs = q
    app.state.headless = None
    client.post("/v1/extract/jobs", headers=_service_headers(),
                json={"file_uid": "d1", "version": "v1", "prompt_set": sid})
    from convert_search_ai.headless import routes as hr
    meter = hr._svc(app).meter
    real = meter.tokens_exhausted
    meter.tokens_exhausted = lambda ident: True
    w = JobWorker(app, q, consumer="t")
    w.step(block_ms=0)
    assert "xj_1" in q.pending and events == []                   # deferred, not acked
    meter.tokens_exhausted = real                                 # the budget has reset
    w.step(block_ms=0)
    assert "xj_1" in q.pending                                    # not due yet
    q.idle()
    w.step(block_ms=0)
    assert q.pending == {} and events[0]["type"] == "extraction.complete"
