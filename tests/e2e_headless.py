"""End-to-end: a desktop application drives headless csai with a generated key.

Run through tests/test_e2e_headless.sh (it signs the fixture user in, 2FA and
all, and passes USER_TOKEN). Against the live dev stack: http_bridge, ldap_manager,
csai (app + ingest worker), the core and the configured chat model — so this is
also the measurement of whether that model returns valid, evidence-backed JSON.

1. Create an api-scoped key; exchange it at the REST door for a session bearer.
2. Upload a fixture folder through the REST door: a door datasheet and an FSC
   certificate as PDFs with real tables, and a note that states none of the
   values (the negative control).
3. POST /v1/extract over the folder (SSE): every value right, with a quote that
   is really at its offset in the stored Markdown; the note finds nothing.
4. The same request again: served from the sidecar cache, no tokens.
5. By id list, with an id that does not exist: one skip reason.
6. Usage status, the run listed under the key's label, search scoped to the
   folder, and the key refused when sent straight to csai.
7. Revoke the key: refresh fails at the bridge; sign the session out: csai
   refuses it too.
"""
import base64
import io
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BASE", "http://localhost:8090")
IDM = os.environ.get("IDM", "http://localhost:8093")
CSAI = os.environ.get("CSAI", "http://localhost:8092")
TENANT = os.environ.get("FE_TENANT", "default")
USER_TOKEN = os.environ["USER_TOKEN"]

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


def pdf(title, rows, extra=""):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    st = getSampleStyleSheet()
    t = Table(rows)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]))
    doc.build([Paragraph(title, st["Title"]), Paragraph(extra, st["Normal"]), Spacer(1, 12), t])
    return buf.getvalue()


def sse(resp):
    events, ev = [], None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: ") and ev:
            events.append((ev, json.loads(line[6:])))
            ev = None
    return events


def main():
    user = {"Authorization": f"Bearer {USER_TOKEN}", "X-Tenant": TENANT}
    c = httpx.Client(timeout=300)

    print("1. Key -> REST door session")
    r = c.post(f"{IDM}/v1/me/service-credentials", headers=user,
               json={"label": "Reviera e2e", "scopes": ["api"]})
    cred = r.json()
    key, secret = cred["key_id"], cred["secret"]
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    tok = c.post(f"{BASE}/v1/auth/token", headers={"Authorization": f"Basic {basic}",
                                                    "X-Tenant": TENANT}).json()["token"]
    H = {"Authorization": f"Bearer {tok}", "X-Tenant": TENANT}
    check("key session issued", bool(tok))

    print("2. Fixture folder through the REST door")
    folder = c.post(f"{BASE}/v1/dirs/root", headers=H,
                    json={"name": f"headless-e2e-{int(time.time())}"}).json()["uid"]
    files = {
        "door-D90-datasheet.pdf": pdf("Door D-90 Technical Datasheet", [
            ["Property", "Value"], ["Manufacturer", "Nordwood Doors AB"],
            ["Model", "D-90 Acoustic"], ["Fire resistance", "EI 60"],
            ["Width", "926 mm"], ["EPD number", "S-P-04512"]],
            "Internal timber door leaf for offices and schools."),
        "FSC-certificate.pdf": pdf("FSC Chain of Custody Certificate", [
            ["Certificate holder", "Nordwood Doors AB"],
            ["Certificate code", "SA-COC-012345"], ["Valid until", "2028-03-31"]],
            "This certificate confirms the chain of custody of the holder's products."),
        "meeting-notes.txt": b"Meeting notes. We discussed the site schedule and lunch options.\n",
    }
    uids = {}
    for name, data in files.items():
        uid = c.post(f"{BASE}/v1/dirs/{folder}/files", headers=H, json={"name": name}).json()["uid"]
        c.put(f"{BASE}/v1/files/{uid}/content", headers={**H, "Content-Type": "application/octet-stream"},
              content=data)
        uids[name] = uid
    check("three files uploaded", len(uids) == 3)

    schema = {"type": "object", "properties": {
        "manufacturer": {"type": "string"},
        "fire_rating": {"type": "string", "description": "fire resistance class, e.g. EI 30"},
        "width_mm": {"type": "number"},
        "epd_number": {"type": "string"},
        "certificate_code": {"type": "string"},
        "valid_until": {"type": "string", "description": "ISO date"}}}

    print("3. Extract over the folder (SSE)")
    t0 = time.time()
    with c.stream("POST", f"{CSAI}/v1/extract", headers=H,
                  json={"folder_uid": folder, "schema": schema}) as resp:
        check("SSE 200 with usage headers", resp.status_code == 200
              and resp.headers.get("x-usage-state") == "ok", resp.status_code)
        events = sse(resp)
    elapsed = time.time() - t0
    kinds = [e for e, _ in events]
    check("event order usage, plan, …, usage, done",
          kinds[:2] == ["usage", "plan"] and kinds[-2:] == ["usage", "done"], kinds)
    items = {d["name"]: d for e, d in events if e == "item"}
    door, cert, note = (items.get("door-D90-datasheet.pdf"), items.get("FSC-certificate.pdf"),
                        items.get("meeting-notes.txt"))
    print(f"   ({elapsed:.1f}s; done: {events[-1][1]})")
    for name, it in items.items():
        print(f"   {name}: valid={it.get('valid')} counts={it.get('counts')} "
              f"skipped={it.get('skipped')} tokens={it.get('tokens')}"
              + (f" problems={it.get('problems')}" if it.get("problems") else ""))

    def val(it, f):
        return ((it or {}).get("data") or {}).get(f, {}).get("value")

    check("door: fire rating EI 60", val(door, "fire_rating") == "EI 60", val(door, "fire_rating"))
    check("door: width 926", val(door, "width_mm") in (926, 926.0), val(door, "width_mm"))
    check("door: EPD number", val(door, "epd_number") == "S-P-04512", val(door, "epd_number"))
    check("door: manufacturer", val(door, "manufacturer") == "Nordwood Doors AB")
    check("door: no certificate code (not in that document)",
          val(door, "certificate_code") is None)
    check("certificate: code", val(cert, "certificate_code") == "SA-COC-012345",
          val(cert, "certificate_code"))
    check("certificate: valid until", val(cert, "valid_until") == "2028-03-31",
          val(cert, "valid_until"))
    check("certificate: no fire rating", val(cert, "fire_rating") is None)
    check("note: nothing found, never guessed",
          note is not None and (note.get("skipped") or note["counts"]["found"] == 0), note)

    print("   evidence offsets point at the quote in the stored Markdown")
    text = c.get(f"{CSAI}/documents/{uids['door-D90-datasheet.pdf']}/text", headers=H).json()["text"]
    ev = (door or {}).get("data", {}).get("fire_rating", {}).get("evidence") or {}
    seg = text[ev.get("offset", 0):ev.get("offset", 0) + 200]
    check("fire rating quote found at its offset", "EI 60" in seg, (ev, seg[:80]))

    print("4. Same request again: the sidecar cache")
    r = c.post(f"{CSAI}/v1/extract", headers={**H, "Accept": "application/json"},
               json={"folder_uid": folder, "schema": schema})
    body = r.json()
    cached = [i for i in body["items"] if i.get("cached")]
    for i in body["items"]:
        print(f"   {i.get('name')}: cached={i.get('cached')} valid={i.get('valid')}"
              + (f" problems={i.get('problems')}" if i.get("problems") else ""))
    # Only a VALID result is cached (an invalid one is re-asked next time), so
    # the invariant is: every document valid on the first run is a cache hit now.
    first_valid = {n for n, it in items.items() if it.get("valid")}
    hit_names = {i["name"] for i in cached}
    check("every document valid on the first run is served from cache",
          first_valid <= hit_names, (first_valid, hit_names))
    check("cache hits cost no tokens",
          sum(int(i.get("tokens") or 0) for i in cached) == 0)
    check("cache hits counted in usage", body["usage"]["cache"]["hits"] >= len(cached))

    print("5. By id list, with an id that does not exist")
    r = c.post(f"{CSAI}/v1/extract", headers={**H, "Accept": "application/json"},
               json={"files": [uids["FSC-certificate.pdf"], "00000000-dead-beef-0000-000000000000"],
                     "schema": schema}).json()
    check("unknown id: one skip reason",
          r["plan"]["skipped"] == [{"file_uid": "00000000-dead-beef-0000-000000000000",
                                    "reason": "not found or not readable"}], r["plan"])

    print("6. Usage, runs, search, key refused at csai")
    st = c.get(f"{CSAI}/v1/usage", headers=H).json()
    check("usage counts tokens for this key", st["subject"] == "key"
          and st["limits"]["tokens"]["used"] > 0, st["limits"]["tokens"])
    apps = c.get(f"{CSAI}/v1/runs", headers=user).json()["applications"]
    mine = [a for a in apps if a["client_key_id"] == key]
    check("runs listed under the key's label", mine and mine[0]["label"] == "Reviera e2e"
          and mine[0]["run_count"] == 3, mine[:1])
    convos = c.get(f"{CSAI}/conversations", headers=user).json()["conversations"]
    run_ids = {r["id"] for r in mine[0]["runs"]} if mine else set()
    check("runs are not in the research sidebar", not (run_ids & {x["id"] for x in convos}))
    if run_ids:
        check("a run cannot be deleted",
              c.delete(f"{CSAI}/conversations/{next(iter(run_ids))}", headers=user).status_code == 409)
    s = c.post(f"{CSAI}/search", headers=H,
               json={"query": "Nordwood", "folder_uids": [folder]})
    names = {h["name"] for h in s.json().get("hits", [])}
    check("search scoped to the folder finds the datasheets",
          s.status_code == 200 and "door-D90-datasheet.pdf" in names, names)
    check("search carries usage headers for the key", s.headers.get("x-usage-state") == "ok")
    r = c.post(f"{CSAI}/v1/extract", headers={"Authorization": f"Basic {basic}"},
               json={"files": [uids["FSC-certificate.pdf"]], "schema": schema})
    check("key sent straight to csai -> 401 with a pointer",
          r.status_code == 401 and "/api/v1/auth/token" in r.text)

    print("7. Revocation and sign-out")
    c.delete(f"{IDM}/v1/me/service-credentials/{key}", headers=user)
    check("refresh after revoke -> 401",
          c.post(f"{BASE}/v1/auth/refresh", headers=H).status_code == 401)
    check("sign-out at the bridge", c.delete(f"{BASE}/v1/auth/token", headers=H).status_code == 204)
    # csai caches a "not revoked" answer for AUTH_REVOCATION_CACHE_TTL_SECONDS
    # (default 5): the documented revocation latency. Wait it out.
    time.sleep(6)
    check("signed-out session refused by csai (after the revocation cache window)",
          c.get(f"{CSAI}/v1/usage", headers=H).status_code == 401)

    c.delete(f"{BASE}/v1/dirs/{folder}", headers=user)
    print(f"headless e2e: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
