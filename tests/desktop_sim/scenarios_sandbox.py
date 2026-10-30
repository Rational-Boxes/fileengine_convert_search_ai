"""Sandbox tier: the harness's own csai with the stub model, the stub MCP server
and low limits (``sandbox.SANDBOX_LIMITS``). Deterministic, so every protocol
path a desktop must handle can be asserted exactly."""
from __future__ import annotations

import threading
import time

from . import fixtures as fx
from .harness import scenario

S = fx.SANDBOX_SCHEMA


def _sb(ctx) -> dict:
    """The sandbox fixture folders, uploaded and indexed once per run."""
    def build():
        st = ctx.stack
        root = st.mkdir(f"desktop-sim-sandbox-{int(time.time())}")
        ctx.env.at_exit(lambda: st.rmdir(root))
        G = fx.GOOD_ROWS
        layout = {
            "replies": {
                "a-good.md": fx.md("Good", G),
                "b-garbage.md": fx.md("Garbage", G, "STUB-GARBAGE"),
                "c-wrongtype.md": fx.md("Wrong type", G, "STUB-WRONGTYPE"),
                "d-wrongtype-once.md": fx.md("Wrong once", G, "STUB-WRONGTYPE-ONCE"),
                "e-invent.md": fx.md("Invent", {"Manufacturer": "Nordwood Doors AB"}, "STUB-INVENT"),
                "f-empty.md": fx.md("Empty", G, "STUB-EMPTY"),
                "g-fails.md": fx.md("Fails", G, "STUB-HTTP500"),
            },
            "slow": {f"slow-{i}.md": fx.md(f"Slow {i}", G, "STUB-SLEEP=3") for i in range(3)},
            "budget": {f"budget-{i}.md": fx.md(f"Budget {i}", G, "STUB-TOKENS=2000") for i in range(4)},
            "warn": {"warn.md": fx.md("Warn", G, "STUB-TOKENS=4100")},
            "big": {f"big-{i:02d}.md": fx.md(f"Big {i}", G) for i in range(10)},
            "long": {"long-manual.md": fx.long_document(G, 9000)},
        }
        folders, uids = {}, {}
        for sub, files in layout.items():
            folders[sub] = st.mkdir(sub, parent=root)
            for name, data in files.items():
                uids[name] = st.put_file(folders[sub], name, data)
        texts = st.wait_indexed(uids.values())
        return {"root": root, "folders": folders, "uids": uids, "texts": texts}
    return ctx.env.fixture("sandbox", build)


def _val(item, field):
    return ((item or {}).get("data") or {}).get(field, {}).get("value")


def _model_calls(ctx, doc_name: str) -> list:
    return [r for r in ctx.env.model.requests("json") if r.get("document") == doc_name]


# ------------------------------------------------------------------ model replies

@scenario("model replies: invalid, repaired, invented, empty, failing", "sandbox")
def model_replies(ctx):
    """What csai does with every kind of bad answer a model gives."""
    f = _sb(ctx)
    a = ctx.app("Sim replies")
    ctx.env.model.reset()
    s = a.extract({"folder_uid": f["folders"]["replies"], "schema": S})
    it = s.items()
    good = it.get("a-good.md")
    ctx.check("good: valid, every value typed", good and good.get("valid")
              and _val(good, "width_mm") == 926 and _val(good, "fire_rating") == "EI 60", good)
    ctx.check("extraction asks for JSON mode",
              all(r["json_mode"] for r in ctx.env.model.requests("json")))
    g = it.get("b-garbage.md")
    ctx.check("garbage: invalid with problems, after exactly one repair",
              g and g.get("valid") is False and g.get("problems")
              and len(_model_calls(ctx, "b-garbage.md")) == 2, (g, len(_model_calls(ctx, "b-garbage.md"))))
    w = it.get("c-wrongtype.md")
    ctx.check("wrong type twice: invalid", w and w.get("valid") is False and w.get("problems"), w)
    w1 = it.get("d-wrongtype-once.md")
    ctx.check("wrong type once: valid after the repair", w1 and w1.get("valid")
              and _val(w1, "width_mm") == 926 and len(_model_calls(ctx, "d-wrongtype-once.md")) == 2, w1)
    inv = it.get("e-invent.md")
    dropped = [k for k, v in ((inv or {}).get("data") or {}).items() if v.get("dropped")]
    ctx.check("invented values without evidence are dropped, not returned",
              inv and set(dropped) == {"fire_rating", "width_mm", "epd_number"}
              and all(not inv["data"][k]["found"] for k in dropped)
              and _val(inv, "manufacturer") == "Nordwood Doors AB", inv)
    e = it.get("f-empty.md")
    ctx.check("an empty reply counts as nothing found (valid)",
              e and e.get("valid") and not any(v.get("found") for v in e["data"].values()), e)
    errs = s.of("error")
    ctx.check("a failing model call is an error for that file; the run goes on",
              any(x.get("name") == "g-fails.md" or x.get("file_uid") == f["uids"]["g-fails.md"]
                  for x in errs) and (s.one("done") or {}).get("files") == 7, (errs, s.one("done")))

    ctx.env.model.reset()
    again = a.extract_json({"folder_uid": f["folders"]["replies"], "schema": S}).json()
    by = {i.get("name"): i for i in again["items"]}
    ctx.check("valid results are served from the cache next time",
              all(by.get(n, {}).get("cached") for n in ("a-good.md", "d-wrongtype-once.md",
                                                        "e-invent.md", "f-empty.md")),
              {n: by.get(n, {}).get("cached") for n in by})
    ctx.check("invalid results are not cached: asked again",
              not by.get("b-garbage.md", {}).get("cached") and _model_calls(ctx, "b-garbage.md"))


@scenario("model replies: a long document is read in windows", "sandbox")
def long_document(ctx):
    f = _sb(ctx)
    a = ctx.app("Sim long")
    ctx.env.model.reset()
    r = a.extract_json({"files": [f["uids"]["long-manual.md"]], "schema": S}).json()
    item = r["items"][0]
    calls = _model_calls(ctx, "long-manual.md")
    ctx.check("the document is asked about in several parts", len(calls) >= 2, len(calls))
    ctx.check("values from the last part are found", _val(item, "fire_rating") == "EI 60"
              and _val(item, "width_mm") == 926, item)


# ------------------------------------------------------------------ limits

@scenario("limits: token budget runs out mid-run; later requests 429", "sandbox")
def token_budget(ctx):
    f = _sb(ctx)
    a = ctx.app("Sim budget")
    s = a.extract({"folder_uid": f["folders"]["budget"], "schema": S})
    items = s.of("item")
    skipped = [i for i in items if i.get("skipped") == "budget"]
    done_ok = [i for i in items if i.get("valid")]
    ctx.check("files extracted until the budget ran out, the rest skipped: budget",
              done_ok and skipped and len(done_ok) + len(skipped) == 4,
              [(i.get("name"), i.get("valid"), i.get("skipped")) for i in items])
    ctx.check("no half-extracted file", all(i.get("valid") or i.get("skipped") for i in items))
    last = s.of("usage")[-1] if s.of("usage") else {}
    ctx.check("the last usage event says exceeded", last.get("state") == "exceeded", last.get("state"))
    states = [u.get("state") for u in s.of("usage")]
    ctx.check("a usage event when the state changed mid-run, before the end",
              len(states) >= 3 and states[0] == "ok" and "exceeded" in states[1:-1], states)
    ctx.check("the desktop raised a warning from the stream",
              any(w.source == "event" and w.state == "exceeded" for w in a.warnings))
    r = a.extract_json({"folder_uid": f["folders"]["budget"], "schema": S})
    ctx.check("the next run is refused: 429 tokens with Retry-After",
              r.status_code == 429 and r.json().get("limit") == "tokens"
              and r.headers.get("retry-after"), (r.status_code, r.text[:200]))
    ctx.check("429 body carries the usage object and a message",
              r.status_code == 429 and r.json().get("usage", {}).get("state") == "exceeded"
              and r.json().get("message"))
    r = a.search("Nordwood", folder_uids=[f["folders"]["budget"]])
    ctx.check("search spends no tokens, so it still works (state exceeded in headers)",
              r.status_code == 200 and r.headers.get("x-usage-state") == "exceeded",
              (r.status_code, r.headers.get("x-usage-state")))


@scenario("limits: warning at 80% in headers, stream, /v1/usage and the web app", "sandbox")
def usage_warning(ctx):
    f = _sb(ctx)
    seen = []
    a = ctx.app("Sim warning", on_warning=seen.append)
    a.extract_json({"files": [f["uids"]["warn.md"]], "schema": S})
    u = a.usage()
    ctx.check("usage: warning, with a message to show as is",
              u.get("state") == "warning" and "%" in (u.get("message") or ""), u)
    ctx.check("tokens used is what the model reported", u["limits"]["tokens"]["used"] >= 4100,
              u["limits"]["tokens"])
    r = a.text(f["uids"]["warn.md"])
    ctx.check("any later response carries X-Usage-State: warning",
              r.headers.get("x-usage-state") == "warning", r.headers.get("x-usage-state"))
    ctx.check("the desktop's status bar got the warning", any(w.state == "warning" for w in seen),
              [(w.source, w.state) for w in seen])
    st = ctx.stack
    apps = st.http.get(f"{ctx.csai}/v1/runs", headers=st.user).json()["applications"]
    fold = next((x for x in apps if x["client_key_id"] == a.key_id), {})
    ctx.check("the web app's fold shows the same warning",
              (fold.get("usage") or {}).get("state") == "warning", fold.get("usage"))


@scenario("limits: requests per minute -> 429 with Retry-After and RateLimit headers", "sandbox")
def request_rate(ctx):
    a = ctx.app("Sim rate")
    codes, last_ok, first_429 = [], None, None
    for _ in range(45):
        r = a.search("Nordwood")     # counted; GET /v1/usage is not (a status panel polls it)
        codes.append(r.status_code)
        if r.status_code == 200:
            last_ok = r
        elif r.status_code == 429 and first_429 is None:
            first_429 = r
    ctx.check("40 admitted, then 429", codes.count(200) == 40 and codes[40:] == [429] * 5,
              codes)
    if last_ok is not None:
        ctx.check("RateLimit-Policy and RateLimit headers on admitted responses",
                  last_ok.headers.get("ratelimit-policy") and last_ok.headers.get("ratelimit"),
                  dict(last_ok.headers))
    if first_429 is not None:
        b = first_429.json()
        ctx.check("429: kind rate_limited, limit requests, Retry-After",
                  b.get("kind") == "rate_limited" and b.get("limit") == "requests"
                  and first_429.headers.get("retry-after"), b)
    other = ctx.app("Sim rate other key")
    ctx.check("another key of the same user is not affected (per-key limits)",
              other.search("Nordwood").status_code == 200)
    ctx.check("polling /v1/usage is not refused while the request limit is spent",
              a.call("GET", "/v1/usage").status_code == 200)


@scenario("limits: two concurrent runs per key; a third is 429 concurrent", "sandbox")
def concurrency(ctx):
    f = _sb(ctx)
    a = ctx.app("Sim concurrency")
    body = {"folder_uid": f["folders"]["slow"], "schema": S, "cache": "refresh"}
    results = []

    def run():
        results.append(a.extract(body))
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    third = a.extract_json(body)
    ctx.check("third concurrent run -> 429 concurrent, Retry-After",
              third.status_code == 429 and third.json().get("limit") == "concurrent"
              and third.headers.get("retry-after"), (third.status_code, third.text[:200]))
    for t in threads:
        t.join()
    ctx.check("both admitted runs completed", len(results) == 2
              and all(r.status == 200 and r.kinds()[-1] == "done" for r in results),
              [(r.status, r.kinds()[-1:]) for r in results])
    u = a.usage()
    ctx.check("slots released when the runs end", u["limits"].get("concurrent", {}).get("used") == 0,
              u["limits"].get("concurrent"))


@scenario("limits: a folder larger than files-per-run is not cut short silently", "sandbox")
def files_per_run(ctx):
    """10 documents, a cap of 8: the desktop must be told what was left out."""
    f = _sb(ctx)
    a = ctx.app("Sim files per run")
    r = a.extract_json({"folder_uid": f["folders"]["big"], "schema": S}).json()
    plan = r.get("plan") or {}
    n = len(plan.get("files", []))
    ctx.check("at most files-per-run documents are read", n <= 8, n)
    ctx.check("the plan says the folder had more than the cap",
              plan.get("truncated") is True and plan.get("limit") == 8,
              {k: (v if k != "files" else len(v)) for k, v in plan.items()})
    ctx.check("and so does the summary", (r.get("summary") or {}).get("truncated") is True)


# ------------------------------------------------------------------ cancellation

@scenario("cancel: hanging up stops the extraction and frees the slot", "sandbox")
def cancel_extract(ctx):
    f = _sb(ctx)
    a = ctx.app("Sim cancel extract")
    ctx.env.model.reset()
    s = a.extract({"folder_uid": f["folders"]["slow"], "schema": S, "cache": "refresh"},
                  cancel_when=lambda e, d, st: e == "item")
    ctx.check("the desktop hung up after the first file", s.cancelled and len(s.of("item")) == 1)
    time.sleep(7)    # the file in flight (3 s) finishes; no further file starts
    calls = ctx.env.model.requests("json")
    ctx.check("no more than the file in flight was asked after the hang-up", len(calls) <= 2,
              [c.get("document") for c in calls])
    used = ctx.wait("slot freed", lambda: a.usage()["limits"].get("concurrent", {}).get("used") == 0,
                    timeout=10)
    ctx.check("the concurrency slot is released", used)
    run_id = (s.one("done") or {}).get("run_id")
    st = ctx.stack
    apps = st.http.get(f"{ctx.csai}/v1/runs", headers=st.user).json()["applications"]
    fold = next((x for x in apps if x["client_key_id"] == a.key_id), {})
    ctx.check("the cancelled run is still recorded for review", fold.get("run_count") == 1, fold)
    if fold.get("runs"):
        run = st.http.get(f"{ctx.csai}/v1/runs/{fold['runs'][0]['id']}", headers=st.user).json()
        ctx.check("with the results produced before the hang-up", 1 <= len(run.get("results") or []) <= 2,
                  len(run.get("results") or []))
    _ = run_id


@scenario("cancel: hanging up on a research stream stops the model", "sandbox")
def cancel_research(ctx):
    a = ctx.app("Sim cancel research")
    ctx.env.model.reset()
    s = a.research({"message": "Tell me everything. STUB-R-SLOW"},
                   cancel_when=lambda e, d, st: len(st.of("token")) >= 5)
    ctx.check("hung up after five tokens", s.cancelled and len(s.of("token")) == 5)

    def aborted():
        rs = [r for r in ctx.env.model.requests() if r["kind"] in ("stream", "tools")
              and "R-SLOW" in r["directives"]]
        return rs and (rs[-1]["aborted"] or rs[-1]["completed"]) and rs[-1]
    rec = ctx.wait("model stream ended", aborted, timeout=25)
    ctx.check("the provider stream was closed early (model stopped generating)",
              rec and rec["aborted"] and rec["chunks"] < 400,
              rec and {k: rec[k] for k in ("aborted", "completed", "chunks")})
    used = ctx.wait("slot freed", lambda: a.usage()["limits"].get("concurrent", {}).get("used") == 0,
                    timeout=10)
    ctx.check("the concurrency slot is released", used)


# ------------------------------------------------------------------ MCP approvals

def _integration(ctx) -> str:
    def build():
        st = ctx.stack
        r = st.http.post(f"{ctx.csai}/v1/admin/mcp-integrations", headers=st.user, json={
            "name": "Desktop sim notes", "endpoint_url": ctx.env.mcp.url,
            "transport": "streamable-http", "enabled": True})
        r.raise_for_status()
        iid = r.json()["id"]
        ctx.env.at_exit(lambda: st.http.delete(f"{ctx.csai}/v1/admin/mcp-integrations/{iid}",
                                               headers=st.user))
        return iid
    return ctx.env.fixture("mcp_integration", build)


ASK = {"message": "Record a note saying hello. STUB-R-TOOL=record_note", "tools": {"mcp": "ask"}}


@scenario("approval: request streamed down, approved, tool runs", "sandbox")
def consent_approve(ctx):
    _integration(ctx)
    a = ctx.app("Sim consent approve")
    before = len(ctx.env.mcp.calls)
    s = a.research(ASK, consent=lambda ev: {"decision": True})
    req = s.one("tool_consent_request") or {}
    ctx.check("tool_consent_request carries id, run, integration, tool, args, expiry",
              all(req.get(k) for k in ("id", "run_id", "integration", "tool", "expires_at"))
              and req.get("args") == {"text": "hello 1"}, req)
    ctx.check("the id is random, not sequential", len(req.get("id", "")) >= 20
              and not req.get("id", "").startswith("consent-"), req.get("id"))
    ctx.check("the answer was accepted", s.consents and s.consents[0][2] == 200, s.consents)
    ctx.check("the tool ran once", len(ctx.env.mcp.calls) == before + 1)
    ans = (s.one("result") or {}).get("answer", "")
    ctx.check("the answer uses the tool's result", "receipt-" in ans, ans[:200])
    ctx.check("the run ends normally", s.kinds()[-1] == "done")
    rid = req.get("run_id")
    r = a.answer_consent(rid, req.get("id", ""), True)
    ctx.check("an id cannot be used twice -> 404", r.status_code == 404, r.status_code)


@scenario("approval: denied -> the tool never runs", "sandbox")
def consent_deny(ctx):
    _integration(ctx)
    a = ctx.app("Sim consent deny")
    before = len(ctx.env.mcp.calls)
    s = a.research(ASK, consent=lambda ev: {"decision": False})
    ctx.check("one request, answered", len(s.of("tool_consent_request")) == 1 and s.consents)
    ctx.check("the tool did not run", len(ctx.env.mcp.calls) == before)
    ctx.check("the run still finishes", s.kinds()[-1] == "done")


@scenario("approval: no answer -> denied at expiry, stream kept alive meanwhile", "sandbox")
def consent_timeout(ctx):
    _integration(ctx)
    a = ctx.app("Sim consent timeout")
    before = len(ctx.env.mcp.calls)
    s = a.research(ASK, consent=lambda ev: None)
    ctx.check("the run finishes after the expiry", s.kinds()[-1] == "done" and s.elapsed_s >= 10,
              (s.kinds()[-3:], round(s.elapsed_s, 1)))
    ctx.check("keep-alives arrived while waiting", s.keepalives >= 1, s.keepalives)
    ctx.check("the tool did not run", len(ctx.env.mcp.calls) == before)


@scenario("approval: only the run's own key can answer", "sandbox")
def consent_foreign(ctx):
    _integration(ctx)
    a = ctx.app("Sim consent owner")
    b = ctx.app("Sim consent stranger")
    before = len(ctx.env.mcp.calls)
    seen = {}

    def policy(ev):
        seen["stranger"] = b.answer_consent(ev["run_id"], ev["id"], True).status_code
        seen["bad_id"] = a.answer_consent(ev["run_id"], "not-an-issued-id", True).status_code
        seen["bad_run"] = a.answer_consent("00000000-0000-0000-0000-000000000000", ev["id"], True).status_code
        return {"decision": False}
    s = a.research(ASK, consent=policy)
    ctx.check("another key of the same user -> 404", seen.get("stranger") == 404, seen)
    ctx.check("an id never issued -> 404", seen.get("bad_id") == 404, seen)
    ctx.check("the wrong run -> 404", seen.get("bad_run") == 404, seen)
    ctx.check("the stranger's 'approve' did not count: the owner's deny stands",
              len(ctx.env.mcp.calls) == before and s.consents and s.consents[0][2] == 200,
              (len(ctx.env.mcp.calls) - before, s.consents))


@scenario("approval: remember applies for the rest of the run only", "sandbox")
def consent_remember(ctx):
    _integration(ctx)
    a = ctx.app("Sim consent remember")
    before = len(ctx.env.mcp.calls)
    body = {**ASK, "message": ASK["message"] + " STUB-R-TOOL-TWICE"}
    s = a.research(body, consent=lambda ev: {"decision": True, "remember": True})
    ctx.check("asked once, tool ran twice", len(s.of("tool_consent_request")) == 1
              and len(ctx.env.mcp.calls) == before + 2,
              (len(s.of("tool_consent_request")), len(ctx.env.mcp.calls) - before))
    s2 = a.research(ASK, consent=lambda ev: {"decision": False})
    ctx.check("a new run asks again", len(s2.of("tool_consent_request")) == 1)


@scenario("approval: the answer may land on another csai instance", "sandbox")
def consent_cross_instance(ctx):
    """The run streams from the sandbox; the answer goes to the dev csai, which
    relays it over Redis."""
    _integration(ctx)
    a = ctx.app("Sim consent relay")
    before = len(ctx.env.mcp.calls)
    s = a.research(ASK, consent=lambda ev: {"decision": True, "base": ctx.stack.csai})
    ctx.check("the other instance accepted the answer", s.consents and s.consents[0][2] == 200,
              s.consents)
    ctx.check("and the waiting run received it: the tool ran", len(ctx.env.mcp.calls) == before + 1)


@scenario("approval: default deny offers no MCP tools at all", "sandbox")
def consent_default(ctx):
    _integration(ctx)
    a = ctx.app("Sim consent default")
    ctx.env.model.reset()
    s = a.research({"message": ASK["message"]})
    offered = [t for r in ctx.env.model.requests() for t in r.get("tools", [])]
    ctx.check("no consent request", not s.of("tool_consent_request"))
    ctx.check("the model was offered no MCP tool", not any(t.startswith("mcp__") for t in offered),
              offered)
    ctx.check("the run finishes", s.kinds()[-1] == "done")


# ------------------------------------------------------------------ reports, deterministic

@scenario("report: the downloaded file is exactly what streamed", "sandbox")
def report_exact(ctx):
    f = _sb(ctx)
    a = ctx.app("Sim report")
    name = f"sim-report-{int(time.time())}.md"
    s = a.research({"message": "Summarise the good datasheet.",
                    "report": {"folder_uid": f["root"], "filename": name}})
    saved = s.one("report_saved") or {}
    ctx.check("report_saved", saved.get("uid"), s.kinds())
    if saved.get("uid"):
        from .app import docx_text, report_body, words
        text = docx_text(a.download(saved["uid"]).content)
        answer = (s.one("result") or {}).get("answer", "")
        ctx.check("the file holds exactly the streamed report's words",
                  words(report_body(answer)) == words(text.replace("Stub report", "", 1))
                  or words(report_body(answer)) == words(text),
                  (words(report_body(answer))[:12], words(text)[:12]))
        rends = ctx.stack.renditions(saved["uid"])
        names = [x.get("name", "") if isinstance(x, dict) else str(x) for x in rends]
        ctx.check("with its provenance sidecar", any(n.endswith("-chatlog.html") for n in names), names)


# ------------------------------------------------------------------ Redis down

@scenario("fail closed: usage counters unavailable -> refused, never uncounted", "sandbox")
def redis_down(ctx):
    """Redis down. By default the session check fails closed first (revocation
    is unknown): the desktop must be told to retry (503 auth_unavailable), not
    that its session is invalid (401), and must keep its credential. With
    revocation told to fail open, the usage meter itself must refuse with 503."""
    from .sandbox import Sandbox
    f = _sb(ctx)
    sb = Sandbox(ctx.env.model.url, log_dir=ctx.env.log_dir, name="no-redis", redis_port=1).start()
    ctx.defer(sb.stop)
    a = ctx.app("Sim no redis", csai=sb.url)
    a._ensure_fresh()
    token = a.token
    r = a.call("GET", "/v1/usage", retry_auth=False)
    ctx.check("default: 503 auth_unavailable, not 401", r.status_code == 503
              and r.json().get("kind") == "auth_unavailable", (r.status_code, r.text[:200]))
    ctx.check("with Retry-After", (r.headers.get("retry-after") or "").isdigit(),
              dict(r.headers))
    r = a.extract_json({"files": [f["uids"]["a-good.md"]], "schema": S})
    ctx.check("default: extract -> 503 auth_unavailable", r.status_code == 503
              and r.json().get("kind") == "auth_unavailable", (r.status_code, r.text[:200]))
    s = a.research({"message": "hello"})
    ctx.check("default: research -> 503", s.status == 503, s.status)
    ctx.check("the desktop kept its session (no 401-driven refresh or sign-in)",
              a.token == token and a.refreshes == 0, a.refreshes)
    model_before = len(ctx.env.model.requests("json"))
    sb2 = Sandbox(ctx.env.model.url, log_dir=ctx.env.log_dir, name="no-redis-open", redis_port=1,
                  overrides={"AUTH_REVOCATION_FAIL_OPEN": "true"}).start()
    ctx.defer(sb2.stop)
    b = ctx.app("Sim no redis open", csai=sb2.url)
    r = b.call("GET", "/v1/usage", retry_auth=False)
    ctx.check("usage -> 503 usage_unavailable", r.status_code == 503
              and r.json().get("kind") == "usage_unavailable", (r.status_code, r.text[:200]))
    r = b.extract_json({"files": [f["uids"]["a-good.md"]], "schema": S})
    ctx.check("extract -> 503", r.status_code == 503, (r.status_code, r.text[:200]))
    s = b.research({"message": "hello"})
    ctx.check("research -> 503", s.status == 503, s.status)
    ctx.check("no model call was made", len(ctx.env.model.requests("json")) == model_before)
