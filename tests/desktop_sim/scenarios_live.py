"""Live tier: the dev stack's csai and its real chat model.

These measure what only the real model can show (correct values, quotes that
are really in the document, nothing invented) and the contract around them
(sessions, cache, versions, search, reports, review, revocation)."""
from __future__ import annotations

import re
import time

from . import fixtures as fx
from .harness import scenario


def _live(ctx) -> dict:
    """The live fixture folder, uploaded and indexed once per run.

        <root>/door-D90-datasheet.pdf, FSC-certificate.pdf, meeting-notes.txt
        <root>/more/two-model-datasheet.pdf, window-W12-datasheet.pdf"""
    def build():
        st = ctx.stack
        root = st.mkdir(f"desktop-sim-live-{int(time.time())}")
        ctx.env.at_exit(lambda: st.rmdir(root))
        uids = {
            "door-D90-datasheet.pdf": st.put_file(root, "door-D90-datasheet.pdf", fx.door_datasheet()),
            "FSC-certificate.pdf": st.put_file(root, "FSC-certificate.pdf", fx.fsc_certificate()),
            "meeting-notes.txt": st.put_file(root, "meeting-notes.txt", fx.NOTE),
        }
        sub = st.mkdir("more", parent=root)
        uids["two-model-datasheet.pdf"] = st.put_file(sub, "two-model-datasheet.pdf",
                                                      fx.two_model_datasheet())
        uids["window-W12-datasheet.pdf"] = st.put_file(sub, "window-W12-datasheet.pdf",
                                                       fx.window_datasheet())
        texts = st.wait_indexed(uids.values())
        return {"folder": root, "sub": sub, "uids": uids, "texts": texts}
    return ctx.env.fixture("live", build)


def _val(item, field):
    return ((item or {}).get("data") or {}).get(field, {}).get("value")


def _found_values(item):
    return {k: v for k, v in ((item or {}).get("data") or {}).items() if v.get("found")}


def _quote_at_offset(text: str, ev: dict) -> bool:
    """The quote's words appear starting near the reported offset."""
    if not ev or ev.get("offset") is None:
        return False
    words = [w for w in re.split(r"[\s|*_`>#\-:]+", ev.get("quote") or "") if w]
    seg = text[ev["offset"]:ev["offset"] + max(200, len(ev.get("quote") or "") * 3)]
    return bool(words) and all(w.lower() in seg.lower() for w in words[:4])


# ------------------------------------------------------------------ session

@scenario("session: key sign-in, claims, refresh, wrong secret, wrong scope", "live")
def session(ctx):
    """The desktop signs in with its key at the REST door and nowhere else."""
    a = ctx.app("Desktop sim session")
    c = a.claims()
    ctx.check("session bearer carries the key id", c.get("skid") == a.key_id, c)
    ctx.check("and the key's label (skname)", c.get("skname") == "Desktop sim session", c)
    old = a.token
    r = a.refresh()
    ctx.check("refresh issues a new bearer", r.status_code == 200 and a.token != old, r.status_code)
    ctx.check("csai accepts the refreshed bearer", a.usage().get("subject") == "key")

    bad = ctx.app("Desktop sim bad secret", sign_in=False)
    bad.secret = bad.secret[:-2] + ("xx" if not bad.secret.endswith("xx") else "yy")
    ctx.check("wrong secret -> 401 at the REST door", bad.sign_in().status_code == 401)

    dav = ctx.app("Desktop sim webdav-only", scopes=("webdav",), sign_in=False)
    r = dav.sign_in()
    ctx.check("a key without the api scope gets no session", r.status_code in (401, 403),
              r.status_code)

    r = a.http.post(f"{ctx.csai}/v1/extract", headers={"Authorization": a.basic},
                    json={"files": ["x"], "schema": fx.LIVE_SCHEMA})
    ctx.check("key:secret sent straight to csai -> 401 pointing at the REST door",
              r.status_code == 401 and "/api/v1/auth/token" in r.text, (r.status_code, r.text[:200]))


# ------------------------------------------------------------------ extraction

@scenario("extract: folder over SSE, values, evidence, negative control", "live")
def extract_folder(ctx):
    """The main use: facts from datasheets and certificates in one folder."""
    f = _live(ctx)
    a = ctx.app("Desktop sim extract")
    s = a.extract({"folder_uid": f["folder"], "schema": fx.LIVE_SCHEMA, "cache": "refresh"})
    ctx.check("200 event stream with usage headers",
              s.status == 200 and s.headers.get("x-usage-state") in ("ok", "warning"), s.status)
    k = s.kinds()
    ctx.check("events: usage, plan, items..., usage, done",
              k[:2] == ["usage", "plan"] and k[-2:] == ["usage", "done"], k)
    ctx.check("first event arrives before any model work (< 3 s)",
              (s.first_event_s or 99) < 3, s.first_event_s)
    plan = s.one("plan") or {}
    ctx.check("plan lists exactly the folder's three documents (not the subfolder)",
              sorted(p["name"] for p in plan.get("files", []))
              == sorted(["door-D90-datasheet.pdf", "FSC-certificate.pdf", "meeting-notes.txt"]),
              plan)
    items = s.items()
    door, cert, note = (items.get("door-D90-datasheet.pdf"), items.get("FSC-certificate.pdf"),
                        items.get("meeting-notes.txt"))
    ctx.check("door: fire rating EI 60", _val(door, "fire_rating") == "EI 60", _val(door, "fire_rating"))
    ctx.check("door: width 926 (number)", _val(door, "width_mm") in (926, 926.0), _val(door, "width_mm"))
    ctx.check("door: EPD number", _val(door, "epd_number") == "S-P-04512", _val(door, "epd_number"))
    ctx.check("door: manufacturer", _val(door, "manufacturer") == "Nordwood Doors AB")
    ctx.check("door: no certificate code", _val(door, "certificate_code") is None)
    ctx.check("certificate: code", _val(cert, "certificate_code") == "SA-COC-012345")
    ctx.check("certificate: valid until", _val(cert, "valid_until") == "2028-03-31")
    ctx.check("certificate: no fire rating", _val(cert, "fire_rating") is None)
    ctx.check("note: nothing found, nothing guessed",
              note is not None and (note.get("skipped") or not _found_values(note)), note)

    bad = []
    for name, it in items.items():
        text = f["texts"][f["uids"][name]]
        for field, v in _found_values(it).items():
            if not _quote_at_offset(text, v.get("evidence")):
                bad.append((name, field, v.get("evidence")))
    ctx.check("every found value's quote is in the stored text at its offset", not bad, bad)
    dropped = [(n, k) for n, it in items.items()
               for k, v in ((it.get("data") or {}).items()) if v.get("dropped")]
    ctx.note(f"values dropped for missing evidence: {len(dropped)} {dropped}")
    done = s.one("done") or {}
    ctx.check("done carries the run id and counts", done.get("run_id") and done.get("files") == 3, done)
    ctx.note(f"{s.elapsed_s:.1f}s, {done.get('tokens')} tokens for three documents")
    ctx.env._fixtures["live_run"] = {"run_id": done.get("run_id"), "items": items,
                                     "key": a.key_id}


@scenario("extract: JSON mode, cache hits, cache refresh, sidecar", "live")
def extract_cache(ctx):
    """A repeat question is free; asking for a refresh pays again."""
    f = _live(ctx)
    a = ctx.app("Desktop sim cache")
    body = {"files": [f["uids"]["door-D90-datasheet.pdf"], f["uids"]["FSC-certificate.pdf"]],
            "schema": fx.LIVE_SCHEMA}
    first = a.extract_json(body).json()
    second = a.extract_json(body).json()
    ctx.check("JSON mode returns run id, plan, items, summary, usage",
              all(k in second for k in ("run_id", "plan", "items", "summary", "usage")), list(second))
    hits = [i for i in second["items"] if i.get("cached")]
    ctx.check("second ask: both documents from the cache", len(hits) == 2,
              [(i.get("name"), i.get("cached")) for i in second["items"]])
    ctx.check("cache hits cost no tokens", sum(int(i.get("tokens") or 0) for i in hits) == 0)
    ctx.check("same values from the cache as from the model",
              {i["name"]: _found_values(i) for i in first["items"]}
              == {i["name"]: _found_values(i) for i in second["items"]})
    ctx.check("usage counts the cache hits", second["usage"]["cache"]["hits"] >= 2, second["usage"]["cache"])
    third = a.extract_json({**body, "cache": "refresh"}).json()
    ctx.check("cache: refresh asks the model again",
              not any(i.get("cached") for i in third["items"])
              and sum(int(i.get("tokens") or 0) for i in third["items"]) > 0)
    rends = ctx.stack.renditions(f["uids"]["door-D90-datasheet.pdf"])
    names = [r.get("name", "") if isinstance(r, dict) else str(r) for r in rends]
    ctx.check("the answer is kept in the file's sidecar (<version>-extract.json)",
              any(n.endswith("-extract.json") for n in names), names)


@scenario("extract: a new version is re-read, not served stale", "live")
def extract_new_version(ctx):
    """The cache is per version: uploading a new version changes the answer."""
    st = ctx.stack
    f = _live(ctx)
    uid = st.put_file(f["folder"], "door-versioned.pdf", fx.door_datasheet("EI 30"))
    st.wait_indexed([uid])
    a = ctx.app("Desktop sim versions")
    body = {"files": [uid], "schema": fx.LIVE_SCHEMA}
    one = a.extract_json(body).json()["items"][0]
    ctx.check("first version: EI 30", _val(one, "fire_rating") == "EI 30", _val(one, "fire_rating"))
    st.put_version(uid, fx.door_datasheet("EI 90"))
    two = a.extract_json(body).json()["items"][0]
    ctx.check("new version: EI 90, read fresh (not cached)",
              _val(two, "fire_rating") == "EI 90" and not two.get("cached"),
              (_val(two, "fire_rating"), two.get("cached")))
    ctx.check("the item names the version it read", two.get("version") and two["version"] != one.get("version"),
              (one.get("version"), two.get("version")))


@scenario("extract: id lists, unknown ids, filters, recursion, instructions", "live")
def extract_targets(ctx):
    """Every way a desktop names what to read."""
    f = _live(ctx)
    u = f["uids"]
    a = ctx.app("Desktop sim targets")
    ghost = "00000000-dead-beef-0000-000000000000"
    r = a.extract_json({"files": [u["FSC-certificate.pdf"], ghost], "schema": fx.LIVE_SCHEMA}).json()
    ctx.check("unknown id: one skip reason, the rest extracted",
              r["plan"]["skipped"] == [{"file_uid": ghost, "reason": "not found or not readable"}]
              and len(r["plan"]["files"]) == 1, r["plan"])
    r = a.extract_json({"files": [f["sub"]], "schema": fx.LIVE_SCHEMA}).json()
    ctx.check("a folder id in a file list: skipped as not a document",
              (r["plan"]["skipped"] or [{}])[0].get("reason") == "not a document", r["plan"])

    def plan_names(body):
        resp = a.extract_json({**body, "schema": fx.LIVE_SCHEMA})
        return sorted(p["name"] for p in (resp.json().get("plan") or {}).get("files", []))

    pdfs = plan_names({"folder_uid": f["folder"], "match": {"extensions": [".pdf"]}})
    ctx.check("match extensions [.pdf] leaves out the note",
              "meeting-notes.txt" not in pdfs
              and {"FSC-certificate.pdf", "door-D90-datasheet.pdf"} <= set(pdfs), pdfs)
    ctx.check("match name *certificate*",
              plan_names({"folder_uid": f["folder"], "match": {"name": "*certificate*"}})
              == ["FSC-certificate.pdf"])
    rec = plan_names({"folder_uid": f["folder"], "recursive": True, "match": {"name": "*datasheet*"}})
    ctx.check("recursive reaches the subfolder",
              "two-model-datasheet.pdf" in rec and "window-W12-datasheet.pdf" in rec, rec)
    r = a.extract_json({"files": [u["two-model-datasheet.pdf"]], "schema": fx.LIVE_SCHEMA,
                        "instructions": "The product is door model D-120."}).json()
    ctx.check("instructions pick the named product (D-120: EI 120)",
              _val(r["items"][0], "fire_rating") == "EI 120", _val(r["items"][0], "fire_rating"))
    r = a.extract_json({"files": [u["two-model-datasheet.pdf"]], "schema": fx.LIVE_SCHEMA}).json()
    ctx.note("two products, none named: fire_rating = "
             f"{_val(r['items'][0], 'fire_rating')!r} (the prompt asks for found:false)")


@scenario("extract: malformed requests are refused before any work", "live")
def extract_bad_requests(ctx):
    """400 with a reason, no run recorded, no tokens."""
    f = _live(ctx)
    a = ctx.app("Desktop sim bad requests")
    before = a.usage()["limits"]["tokens"]["used"]
    many = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(61)}}
    cases = {
        "both folder and files": {"folder_uid": f["folder"], "files": ["x"], "schema": fx.LIVE_SCHEMA},
        "neither folder nor files": {"schema": fx.LIVE_SCHEMA},
        "empty files list": {"files": [], "schema": fx.LIVE_SCHEMA},
        "schema not an object schema": {"files": ["x"], "schema": {"type": "string"}},
        "schema without properties": {"files": ["x"], "schema": {"type": "object"}},
        "61 fields": {"files": ["x"], "schema": many},
        "bad field name": {"files": ["x"], "schema": {"type": "object", "properties": {"a b": {}}}},
        "invalid JSON Schema": {"files": ["x"], "schema": {"type": "object",
                                                         "properties": {"a": {"type": 7}}}},
        "cache value": {"files": ["x"], "schema": fx.LIVE_SCHEMA, "cache": "never"},
        "201 files": {"files": [f"id{i}" for i in range(201)], "schema": fx.LIVE_SCHEMA},
    }
    for name, body in cases.items():
        r = a.extract_json(body)
        ctx.check(f"{name} -> 400/422", r.status_code in (400, 422), (r.status_code, r.text[:200]))
    r = a.call("POST", "/v1/research", json={"message": "   "})
    ctx.check("research with an empty message -> 400", r.status_code == 400, r.status_code)
    r = a.call("POST", "/v1/research", json={"message": "x" * 20001})
    ctx.check("research over the input cap -> 400", r.status_code == 400, r.status_code)
    after = a.usage()["limits"]["tokens"]["used"]
    ctx.check("no tokens spent on refused requests", after == before, (before, after))


# ------------------------------------------------------------------ questions & reports

@scenario("research: a question streamed with citations", "live")
def research_question(ctx):
    f = _live(ctx)
    a = ctx.app("Desktop sim research")
    s = a.research({"message": "What is the fire resistance class of the D-90 door?",
                    "scope_folders": [{"uid": f["folder"]}]})
    k = s.kinds()
    ctx.check("200 stream: usage first, then usage and done last",
              s.status == 200 and k[:1] == ["usage"] and k[-2:] == ["usage", "done"], k)
    tokens = s.of("token")
    ctx.check("the answer streams in many token events", len(tokens) > 3, len(tokens))
    result = s.one("result") or {}
    ctx.check("result carries the whole answer", result.get("answer")
              and result["answer"] == "".join(t.get("text", "") for t in tokens))
    cites = (s.one("citations") or {}).get("citations", [])
    cited = {c.get("file_uid") for c in cites}
    ctx.check("cites the door datasheet", f["uids"]["door-D90-datasheet.pdf"] in cited, cites)
    st = ctx.stack

    def inside(uid):
        node = st.http.get(f"{st.rest}/v1/nodes/{uid}", headers=st.user).json()
        return node.get("parent_uid") in (f["folder"], f["sub"])
    outside = {u for u in cited - {None} if not inside(u)}
    ctx.check("scope_folders keeps citations inside the folder", not outside, outside)
    ctx.note(f"answer mentions EI 60: {'EI 60' in (result.get('answer') or '')}; "
             f"{s.elapsed_s:.1f}s, {result.get('tokens')} tokens")
    ctx.check("no MCP tools offered by default (no consent requests)",
              not s.of("tool_consent_request"))


@scenario("report: saved in FileEngine with its sidecar, downloaded by the desktop", "live")
def report(ctx):
    """Reports use the existing technique; the stream is progress, the file is
    the deliverable."""
    f = _live(ctx)
    a = ctx.app("Desktop sim report")
    name = f"door-summary-{int(time.time())}.md"
    s = a.research({"message": "Write a short summary of the D-90 door datasheet.",
                    "scope_folders": [{"uid": f["folder"]}],
                    "report": {"folder_uid": f["folder"], "filename": name}})
    saved = s.one("report_saved") or {}
    ctx.check("report_saved names the file", saved.get("uid"), s.kinds())
    if not saved.get("uid"):
        return
    r = a.download(saved["uid"])
    ctx.check("the desktop downloads it through the REST door", r.status_code == 200, r.status_code)
    from .app import docx_text, report_body, words
    answer = (s.one("result") or {}).get("answer", "")
    ctx.check("saved as an editable Word document", saved.get("name", "").endswith(".docx"),
              saved.get("name"))
    text = docx_text(r.content) if r.status_code == 200 else ""
    want = words(report_body(answer))
    have = set(words(text))
    share = sum(1 for w in want if w in have) / max(1, len(want))
    ctx.check("the file holds what was streamed (>= 90% of its words)", share >= 0.9,
              (round(share, 2), answer[:120], text[:120]))
    rends = ctx.stack.renditions(saved["uid"])
    names = [x.get("name", "") if isinstance(x, dict) else str(x) for x in rends]
    ctx.check("the report has its provenance sidecar", any(n.endswith("-chatlog.html") for n in names), names)


# ------------------------------------------------------------------ search & the Reviera flow

@scenario("search: folder scope, paging, usage headers, then extract the hits", "live")
def search_then_extract(ctx):
    """The desktop flow of §3.10, ending with values written back as metadata
    through the REST door (what Reviera does with them)."""
    f = _live(ctx)
    a = ctx.app("Desktop sim search")
    r = a.search("Nordwood", folder_uids=[f["folder"]], limit=10)
    hits = r.json().get("hits", []) if r.status_code == 200 else []
    names = [h["name"] for h in hits]
    ctx.check("folder-scoped search finds the datasheets", "door-D90-datasheet.pdf" in names, names)
    ctx.check("nothing from outside the folder (recursive by design)",
              all(h["file_uid"] in f["uids"].values() or h["name"].startswith("door") for h in hits),
              names)
    ctx.check("search carries the usage headers", r.headers.get("x-usage-state") in ("ok", "warning")
              and "ratelimit" in {k.lower() for k in r.headers})
    p0 = a.search("Nordwood", folder_uids=[f["folder"]], limit=1, offset=0).json().get("hits", [])
    p1 = a.search("Nordwood", folder_uids=[f["folder"]], limit=1, offset=1).json().get("hits", [])
    ctx.check("offset pages through the hits",
              p0 and p1 and p0[0]["file_uid"] != p1[0]["file_uid"], (p0, p1))
    t = a.text(f["uids"]["door-D90-datasheet.pdf"])
    ctx.check("document text: the stored Markdown, with the table",
              t.status_code == 200 and "EI 60" in t.json().get("text", ""), t.status_code)
    ids = [h["file_uid"] for h in hits if h["name"].endswith(".pdf")]
    r = a.extract_json({"files": ids, "schema": fx.LIVE_SCHEMA}).json()
    door = next((i for i in r["items"] if i.get("name") == "door-D90-datasheet.pdf"), None)
    ctx.check("extract from the hits by id", _val(door, "fire_rating") == "EI 60")
    if door:
        uid = door["file_uid"]
        put = a.http.put(f"{ctx.stack.rest}/v1/nodes/{uid}/metadata/fire_rating",
                         headers={**a.auth, "Content-Type": "application/json"},
                         json={"value": _val(door, "fire_rating")})
        back = a.http.get(f"{ctx.stack.rest}/v1/nodes/{uid}/metadata/fire_rating", headers=a.auth)
        ctx.check("the value is written back as file metadata with the key session",
                  put.status_code in (200, 201, 204) and "EI 60" in back.text,
                  (put.status_code, put.text[:120], back.status_code, back.text[:120]))


# ------------------------------------------------------------------ review in the web app

@scenario("review: runs per application, read-only, out of the chat list", "live")
def review(ctx):
    f = _live(ctx)
    a = ctx.app("Desktop sim review")
    b = ctx.app("Desktop sim review")          # same name, second key
    for app in (a, b):
        app.extract_json({"files": [f["uids"]["FSC-certificate.pdf"]], "schema": fx.LIVE_SCHEMA})
    a.research({"message": "Who holds the FSC certificate?", "scope_folders": [{"uid": f["folder"]}]})
    st = ctx.stack
    apps = st.http.get(f"{ctx.csai}/v1/runs", headers=st.user).json()["applications"]
    mine = {x["client_key_id"]: x for x in apps if x["client_key_id"] in (a.key_id, b.key_id)}
    ctx.check("one fold per key, even with the same name", len(mine) == 2, list(mine))
    fa = mine.get(a.key_id, {})
    ctx.check("labelled as the person named the key", fa.get("label") == "Desktop sim review")
    ctx.check("run count per key", fa.get("run_count") == 2, fa.get("run_count"))
    ctx.check("the fold carries the key's usage", (fa.get("usage") or {}).get("state") == "ok")
    convos = st.http.get(f"{ctx.csai}/conversations", headers=st.user).json()["conversations"]
    run_ids = {r["id"] for r in fa.get("runs", [])}
    ctx.check("runs are not in the research chat list", not (run_ids & {c["id"] for c in convos}))
    ex = next((r for r in fa.get("runs", []) if r["title"].startswith("Extract")), None)
    if ex:
        run = st.http.get(f"{ctx.csai}/v1/runs/{ex['id']}", headers=st.user).json()
        res = (run.get("results") or [{}])[0]
        ctx.check("an extraction run keeps its values and evidence",
                  _val(res, "certificate_code") == "SA-COC-012345"
                  and res["data"]["certificate_code"].get("evidence"), res)
        ctx.check("a run cannot be deleted",
                  st.http.delete(f"{ctx.csai}/conversations/{ex['id']}", headers=st.user).status_code == 409)
    if ctx.env.user2:
        u2 = ctx.env.user2
        r = u2.http.get(f"{ctx.csai}/v1/runs/{ex['id']}", headers=u2.user) if ex else None
        ctx.check("another user cannot read the run", r is None or r.status_code == 404,
                  r.status_code if r is not None else None)
    else:
        ctx.note("second user not configured (FE_USER2): cross-user isolation not checked")


# ------------------------------------------------------------------ end of a key's life

@scenario("revocation: revoked key cannot refresh; signed-out session refused", "live")
def revocation(ctx):
    a = ctx.app("Desktop sim revoke")
    ctx.check("works before revocation", a.usage().get("subject") == "key")
    ctx.stack.revoke_key(a.key_id)
    ctx.check("refresh after revoke -> 401", a.refresh().status_code == 401)
    ctx.check("sign-in after revoke -> 401", a.sign_in().status_code == 401)
    ctx.check("sign-out -> 204", a.sign_out() == 204)
    time.sleep(6)    # csai's revocation cache (AUTH_REVOCATION_CACHE_TTL_SECONDS, 5 s)
    r = a.http.get(f"{ctx.csai}/v1/usage", headers=a.auth)
    ctx.check("csai refuses the signed-out bearer", r.status_code == 401, r.status_code)
    r = a.call("GET", "/v1/usage")
    ctx.check("the app cannot recover the session (refresh refused too)", r.status_code == 401,
              r.status_code)


@scenario("erasure: an erased source leaves a tombstone in the run", "live")
def erasure(ctx):
    st = ctx.stack
    f = _live(ctx)
    uid = st.put_file(f["folder"], "to-erase-datasheet.pdf", fx.door_datasheet("EI 45"))
    st.wait_indexed([uid])
    a = ctx.app("Desktop sim erasure")
    run = a.extract_json({"files": [uid], "schema": fx.LIVE_SCHEMA}).json()
    ctx.check("extracted before erasure", _val(run["items"][0], "fire_rating") == "EI 45")
    r = st.http.post(f"{st.rest}/v1/files/{uid}/erase", headers=st.user, json={"reason": "desktop sim"})
    if r.status_code in (401, 403):
        ctx.note(f"erase refused ({r.status_code}): the fixture user is not in erasure_admins; "
                 "tombstone not checked")
        return
    ctx.check("erase accepted", r.status_code in (200, 202), (r.status_code, r.text[:200]))

    def tomb():
        got = st.http.get(f"{ctx.csai}/v1/runs/{run['run_id']}", headers=st.user).json()
        res = (got.get("results") or [{}])[0]
        return res if res.get("erased") else None
    res = ctx.wait("tombstone", tomb, timeout=60, every=2)
    ctx.check("the run's result becomes a tombstone (values gone)",
              res is not None and not res.get("data"), res)
