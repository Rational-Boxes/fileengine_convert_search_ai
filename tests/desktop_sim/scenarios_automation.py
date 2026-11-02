"""Extraction prompt sets end to end (EXTRACTION_PROMPT_SETS.md §8).

Live tier, on the dev stack's real model, csai, folder_actions and core:

1. a desktop application publishes the prompt set it relies on (no setup);
2. a folder gets three actions: *extract* (on conversion.complete), *act on
   extracted values* (metadata + file into a sub-folder named from a value),
   and a *webhook* on extraction.complete to a listener here;
3. a datasheet is uploaded; then, with no further request: the values land in
   metadata, the file is filed under its new name, a marked-up PDF sits beside
   it, every action's run is logged, and the webhook received the values
   (without quotes) under ``extracted``;
4. a rule that cannot apply to the set is refused when it is saved.

Sandbox tier, on the sandbox csai with the scripted model (prompt sets over real
HTTP and the real Postgres store): list with ETag, run by id, pinned revision
and current_revision, model fallback reported, a marked-up PDF written, a draft
invisible, a retired set refused."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import fixtures as fx
from .harness import scenario

SLUG = "desktopsim.door-datasheet"
DOOR_SET = {
    "name": "Desktop sim door datasheet",
    "description": "Fire doors: manufacturer, fire rating, leaf width",
    "instructions": "Use the product the datasheet is about.",
    "fields": [
        {"name": "manufacturer", "label": "Manufacturer", "type": "string", "required": True},
        {"name": "fire_rating", "label": "Fire rating", "type": "string", "required": True,
         "description": "fire resistance class, e.g. EI 30"},
        {"name": "width_mm", "label": "Leaf width", "type": "number", "unit": "mm"},
    ],
}


class Listener:
    """A webhook receiver on localhost."""

    def __init__(self):
        got = self.got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                try:
                    got.append(json.loads(self.rfile.read(n) or b"{}"))
                except ValueError:
                    got.append({"_unparsed": True})
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


def _fa(ctx, method, path, **kw):
    st = ctx.stack
    return st.http.request(method, f"{st.fa}{path}", headers=st.user, **kw)


def _binding(ctx, folder, body):
    r = _fa(ctx, "POST", f"/folders/{folder}/actions", json=body)
    if r.status_code in (200, 201):
        bid = r.json()["id"]
        ctx.defer(lambda: _fa(ctx, "DELETE", f"/actions/{bid}"))
        return r.json(), r
    return None, r


def _entries(ctx, folder):
    r = ctx.stack.http.get(f"{ctx.stack.rest}/v1/dirs/{folder}", headers=ctx.stack.user)
    return r.json().get("entries", []) if r.status_code == 200 else []


def _meta(ctx, uid, key):
    r = ctx.stack.http.get(f"{ctx.stack.rest}/v1/nodes/{uid}/metadata/{key}",
                           headers=ctx.stack.user)
    if r.status_code != 200:
        return None
    try:
        body = r.json()
        return body.get("value") if isinstance(body, dict) else body
    except ValueError:
        return r.text


@scenario("automation: publish a set, extract on arrival, file by value, webhook", "live")
def automation_end_to_end(ctx):
    st = ctx.stack
    a = ctx.app("Desktop sim publisher")
    # 1. the application publishes what it relies on: no role, no setup
    r = a.call("PUT", f"/v1/prompt-sets/slug/{SLUG}", json=DOOR_SET)
    ctx.check("the application publishes its prompt set (created or unchanged)",
              r.status_code in (200, 201), (r.status_code, r.text[:200]))
    if r.status_code not in (200, 201):
        return
    pub = r.json()
    sid, rev = pub["id"], pub["revision"]
    again = a.call("PUT", f"/v1/prompt-sets/slug/{SLUG}", json=DOOR_SET).json()
    ctx.check("publishing it again is a no-op", again.get("changed") is False
              and again.get("revision") == rev, again)
    listed = a.call("GET", "/v1/prompt-sets").json().get("prompt_sets", [])
    mine = next((s for s in listed if s["id"] == sid), {})
    ctx.check("the desktop lists it as application-managed with its fields",
              mine.get("managed") == "application"
              and [f["name"] for f in mine.get("fields", [])] == ["manufacturer", "fire_rating",
                                                                   "width_mm"], mine)

    # 2. the folder and its three actions
    ts = int(time.time())
    inbox = st.mkdir(f"desktop-sim-auto-{ts}")
    filed = st.mkdir(f"desktop-sim-filed-{ts}")
    ctx.defer(lambda: st.rmdir(inbox))
    ctx.defer(lambda: st.rmdir(filed))
    hook = Listener()
    ctx.defer(hook.stop)

    extract, r = _binding(ctx, inbox, {"action_type": "extract", "on_events": ["conversion.complete"],
                                       "config": {"prompt_sets": [sid], "markup": True}})
    ctx.check("extract action saved", extract is not None, (r.status_code, r.text[:300]))
    if extract is None:
        return
    ctx.check("the application's set is pinned at its current revision when saved",
              (extract.get("config") or {}).get("pinned") == {sid: rev}, extract.get("config"))

    _bad, r = _binding(ctx, inbox, {
        "action_type": "extraction_rules", "on_events": ["extraction.complete"],
        "config": {"prompt_set": sid, "rules": [
            {"name": "wide", "conditions": [{"field": "width_mm", "op": "gt", "value": "1 m"}],
             "action": "flag"}]}})
    ctx.check("a rule that cannot apply to the set is refused when saved (422)",
              r.status_code == 422 and "unit" in r.text, (r.status_code, r.text[:300]))
    _bad, r = _binding(ctx, inbox, {"action_type": "extract", "on_events": ["review.approved"],
                                    "config": {"prompt_sets": [sid]}})
    ctx.check("an event the action does not handle is refused (422)", r.status_code == 422,
              (r.status_code, r.text[:200]))

    rules, r = _binding(ctx, inbox, {
        "action_type": "extraction_rules", "on_events": ["extraction.complete"],
        "config": {"prompt_set": sid, "match": "all", "rules": [
            {"name": "metadata", "action": "write_metadata", "metadata_prefix": "sim."},
            {"name": "file it", "conditions": [{"field": "fire_rating", "op": "found"}],
             "action": "move", "destination": filed, "subfolder": "{fire_rating}",
             "name_template": "{manufacturer} {fire_rating}"}]}})
    ctx.check("rules action saved", rules is not None, (r.status_code, r.text[:300]))
    webhook, r = _binding(ctx, inbox, {
        "action_type": "webhook", "on_events": ["extraction.complete"],
        "config": {"url": hook.url, "extraction_prompt_sets": [sid], "timeout_s": 10}})
    ctx.check("webhook saved on extraction.complete", webhook is not None,
              (r.status_code, r.text[:300]))
    if rules is None or webhook is None:
        return

    # 3. a datasheet arrives; nothing else is asked of anyone
    uid = st.put_file(inbox, "door-D90-datasheet.pdf", fx.door_datasheet())
    t0 = time.time()
    filed_entry, sub = None, None
    while time.time() - t0 < 300 and not (filed_entry and hook.got):
        time.sleep(3)
        if filed_entry is None:
            for d in _entries(ctx, filed):
                if d.get("type") == "directory":
                    hit = [e for e in _entries(ctx, d["uid"]) if e.get("uid") == uid]
                    if hit:
                        filed_entry, sub = hit[0], d
    ctx.note(f"arrival to filed + webhook: {time.time() - t0:.0f}s")

    ctx.check("the document was filed into a sub-folder named from its fire rating",
              sub is not None and sub.get("name") == "EI 60", sub and sub.get("name"))
    name = (filed_entry or {}).get("name", "")
    ctx.check("under a name made from the values, extension kept",
              name.endswith(" EI 60.pdf") and "Nordwood" in name, name)
    ctx.check("the inbox no longer holds it", all(e.get("uid") != uid for e in _entries(ctx, inbox)))
    ctx.check("values written as metadata", _meta(ctx, uid, "sim.fire_rating") == "EI 60",
              _meta(ctx, uid, "sim.fire_rating"))
    ctx.check("width with its unit", _meta(ctx, uid, "sim.width_mm") == "926 mm",
              _meta(ctx, uid, "sim.width_mm"))
    src = _meta(ctx, uid, "sim._source") or ""
    ctx.check("and the version + revision they came from", src.endswith(f" r{rev}"), src)
    ctx.check("filing marker for the extract action", (_meta(ctx, uid, "fa.filed_by") or "")
              .startswith(f"{rules['id']}@"), _meta(ctx, uid, "fa.filed_by"))

    names = [x.get("name", "") if isinstance(x, dict) else str(x) for x in st.renditions(uid)]
    ctx.check("the values sidecar beside it", any(n.endswith("-extract.json") for n in names), names)
    ctx.check("and the marked-up PDF", any(f"-extractpdf_{sid[3:]}.pdf" in n for n in names), names)

    body = hook.got[0] if hook.got else {}
    ext = body.get("extracted") or {}
    vals = ext.get("values") or {}
    ctx.check("webhook: fired on extraction.complete with the values",
              body.get("event") == "extraction.complete"
              and (vals.get("fire_rating") or {}).get("value") == "EI 60", body)
    ctx.check("webhook: field definitions and the set travel with them",
              (ext.get("fields") or {}).get("width_mm", {}).get("unit") == "mm"
              and (ext.get("prompt_set") or {}).get("id") == sid, ext.get("fields"))
    ctx.check("webhook: no quotes unless asked",
              all("quote" not in (v.get("evidence") or {}) for v in vals.values()), vals)

    runs = {}
    for label, b in (("extract", extract), ("rules", rules), ("webhook", webhook)):
        rr = _fa(ctx, "GET", f"/actions/{b['id']}/runs")
        runs[label] = rr.json() if rr.status_code == 200 else []
        if isinstance(runs[label], dict):
            runs[label] = runs[label].get("runs") or runs[label].get("items") or []
    done = {k: [x for x in v if x.get("status") == "done"] for k, v in runs.items()}
    ctx.check("extract run logged with its job", len(done["extract"]) == 1
              and (done["extract"][0].get("detail") or {}).get("jobs"), runs["extract"])
    ran = [x.get("action") for x in ((done["rules"][0].get("detail") or {}).get("ran") or [])] \
        if done["rules"] else []
    ctx.check("rules run logged: metadata, then the move", ran == ["write_metadata", "move"],
              runs["rules"])
    wd = (done["webhook"][0].get("detail") or {}).get("extracted") if done["webhook"] else {}
    ctx.check("webhook run logged without values", wd and wd.get("fields", 0) >= 2
              and "EI 60" not in json.dumps(runs["webhook"]), runs["webhook"])
    time.sleep(10)
    again = _fa(ctx, "GET", f"/actions/{extract['id']}/runs").json()
    again = again if isinstance(again, list) else (again.get("runs") or again.get("items") or [])
    ctx.check("filing it did not start another extraction",
              len([x for x in again if x.get("status") == "done"]) == 1, again)


# ------------------------------------------------------------------ sandbox

SB_SET = {"name": "Sandbox door", "fields": [
    {"name": "fire_rating", "type": "string"}, {"name": "width_mm", "type": "number", "unit": "mm"},
    {"name": "epd_number", "type": "string"}], "model": "not-offered-here"}


def _sb_file(ctx):
    def build():
        st = ctx.stack
        root = st.mkdir(f"desktop-sim-ps-{int(time.time())}")
        ctx.env.at_exit(lambda: st.rmdir(root))
        pdf = fx.door_datasheet()
        uid = st.put_file(root, "door.pdf", pdf)
        st.wait_indexed([uid], csai=ctx.csai)
        return {"folder": root, "uid": uid}
    return ctx.env.fixture("sandbox-promptsets", build)


def _purge_prompt_sets(tenant: str, slug_prefix: str) -> None:
    """Test cleanup: prompt sets have no delete (retired sets are kept as the
    record of what ran), so the harness removes its own throwaway sets from the
    dev database, or every run would count against the 50 application sets a
    tenant may have. Reads only CSAI_PG_* from csai's .env."""
    import os
    import psycopg
    from convert_search_ai.schema import schema_name
    env = {}
    try:
        for line in open(".env"):
            if line.startswith("CSAI_PG_") and "=" in line:
                k, _, v = line.strip().partition("=")
                env[k] = v.strip().strip('"').strip("'")
    except OSError:
        return
    get = lambda k, d: os.environ.get(k) or env.get(k) or d
    with psycopg.connect(host=get("CSAI_PG_HOST", "localhost"), port=int(get("CSAI_PG_PORT", "5432")),
                         dbname=get("CSAI_PG_DATABASE", "fileengine"),
                         user=get("CSAI_PG_USER", "postgres"),
                         password=get("CSAI_PG_PASSWORD", "")) as conn, conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema_name(tenant)}".prompt_sets WHERE slug LIKE %s',
                    (slug_prefix.replace("_", "\\_") + "%",))
        conn.commit()


@scenario("prompt sets: list with ETag, run by id, pin, fallback, markup, draft, retire",
          "sandbox")
def prompt_sets_sandbox(ctx):
    f = _sb_file(ctx)
    a = ctx.app("Sim prompt sets")
    slug = f"desktopsim.sandbox-{int(time.time())}"
    ctx.defer(lambda: _purge_prompt_sets(ctx.stack.tenant, "desktopsim.sandbox-"))
    r = a.call("PUT", f"/v1/prompt-sets/slug/{slug}", json=SB_SET)
    ctx.check("publish (201) with a model fallback warning", r.status_code == 201
              and r.json()["warnings"][0]["kind"] == "model_fallback", (r.status_code, r.text[:200]))
    if r.status_code != 201:
        return
    sid = r.json()["id"]
    lst = a.call("GET", "/v1/prompt-sets")
    etag = lst.headers.get("etag")
    same = a.call("GET", "/v1/prompt-sets", headers={"If-None-Match": etag or ""})
    ctx.check("list revalidates with ETag (304)", same.status_code == 304, same.status_code)
    run = a.extract_json({"files": [f["uid"]], "prompt_set": sid}).json()
    plan = run.get("plan") or {}
    item = (run.get("items") or [{}])[0]
    ctx.check("run by id: plan names the set and revision",
              (plan.get("prompt_set") or {}).get("revision") == 1, plan)
    ctx.check("the fallback is reported, not hidden", (plan.get("model") or {}).get("fallback") is True,
              plan.get("model"))
    ctx.check("values from the set's compiled schema", "fire_rating" in (item.get("data") or {}),
              item.get("data"))
    m = item.get("markup") or {}
    ctx.check("marked-up PDF written as a sidecar", m.get("status") == "written"
              and m.get("name", "").endswith(f"-extractpdf_{sid[3:]}.pdf"), m)
    a.call("PUT", f"/v1/prompt-sets/slug/{slug}", json={**SB_SET, "description": "r2"})
    pinned = a.extract_json({"files": [f["uid"]], "prompt_set": sid, "revision": 1}).json()
    pp = (pinned.get("plan") or {}).get("prompt_set") or {}
    ctx.check("a pinned revision runs, and the newer one is reported",
              pp.get("revision") == 1 and pp.get("current_revision") == 2, pp)
    draft = a.call("PUT", f"/v1/prompt-sets/slug/{slug}-draft", json=SB_SET,
                   params={"publish": "false"}).json()
    ids = [s["id"] for s in a.call("GET", "/v1/prompt-sets").json()["prompt_sets"]]
    ctx.check("a draft is invisible to applications", draft.get("id") not in ids, draft)
    r = a.call("POST", f"/v1/prompt-sets/slug/{slug}/retire", json={})
    gone = a.extract_json({"files": [f["uid"]], "prompt_set": sid})
    ctx.check("a retired set is refused (410)", r.status_code == 200 and gone.status_code == 410,
              (r.status_code, gone.status_code))
    a.call("POST", f"/v1/prompt-sets/slug/{slug}-draft/retire", json={})
