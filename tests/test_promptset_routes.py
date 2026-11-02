"""Prompt-set routes through the real app (EXTRACTION_PROMPT_SETS.md §4).

Signed in as the desktop application is (a bridge session minted from an API
key) and as an editor in the web application (a session with no key). Pinned:

- an application publishes its definition with no role and no setup, and an
  unchanged definition is a no-op;
- a key session lists only published sets, with ETag revalidation;
- ``/v1/extract {prompt_set}`` runs the set's compiled schema and reports the
  set, revision and model choice; a pinned revision, a retired set, a draft
  and conflicting parameters are handled as specified;
- a preferred model the deployment does not offer falls back to the default;
- builder test runs on pasted text and on a stored file store nothing;
- YAML export and import over HTTP."""
import json

import pytest
from fastapi.testclient import TestClient

from convert_search_ai.headless.promptset_store import MemoryPromptSetStore

from test_headless_extract import SCHEMA, good_reply
from test_headless_routes import env, sse_events  # noqa: F401 (fixture)

SET = {"name": "Door datasheet", "instructions": "Use the door in the title.",
       "fields": [{"name": "fire_rating", "type": "string", "label": "Fire rating"},
                  {"name": "width_mm", "type": "number"},
                  {"name": "epd_number", "type": "string"}]}
YAML = """format: fileengine.prompt_set.v1
type: extraction_prompt_set
name: Door datasheet
instructions: Use the door in the title.
model: not-offered-here
fields:
  - {name: fire_rating, type: string}
  - {name: width_mm, type: number, unit: mm}
  - {name: epd_number, type: string}
"""


@pytest.fixture
def ctx(env):
    client, app, auth, model = env
    app.state.promptset_store = MemoryPromptSetStore()
    app.state.promptsets = None
    return client, app, auth, model, _editor_headers()


def _editor_headers():
    """A browser session (no key) of a tenant administrator."""
    import hashlib
    import hmac
    from test_headless_routes import SECRET, _b64
    h = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = _b64(json.dumps({"sub": "ed@x.com", "tenant": "default", "exp": 4102444800,
                         "roles": {"default": ["users", "tenant_admin"]}}).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return {"Authorization": f"Bearer {h}.{p}.{sig}", "X-Tenant": "default"}


def _publish(client, auth, body=YAML, slug="reviera.door", **params):
    return client.put(f"/v1/prompt-sets/slug/{slug}", headers={**auth,
                      "Content-Type": "application/yaml"}, content=body, params=params)


def test_an_application_publishes_and_republishing_is_a_no_op(ctx):
    client, app, auth, model, _ = ctx
    r = _publish(client, auth)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["revision"] == 1 and out["changed"]
    assert out["model"]["fallback"] is True and out["warnings"][0]["kind"] == "model_fallback"
    r = _publish(client, auth)
    assert r.status_code == 200 and r.json()["changed"] is False and r.json()["revision"] == 1
    r = client.put("/v1/prompt-sets/slug/reviera.door", headers=auth,
                   json={**SET, "instructions": "v2"})
    assert r.status_code == 200 and r.json()["revision"] == 2
    r = _publish(client, auth, base_revision=1)          # stale base, changed definition
    assert r.status_code == 409 and r.json()["kind"] == "revision_conflict"


def test_a_key_session_lists_published_sets_with_etag(ctx):
    client, app, auth, model, _ = ctx
    _publish(client, auth)
    r = client.get("/v1/prompt-sets", headers=auth)
    assert r.status_code == 200
    (s,) = r.json()["prompt_sets"]
    assert s["slug"] == "reviera.door" and [f["name"] for f in s["fields"]] == \
        ["fire_rating", "width_mm", "epd_number"]
    assert s["fields"][1]["unit"] == "mm"
    etag = r.headers["etag"]
    assert client.get("/v1/prompt-sets", headers={**auth, "If-None-Match": etag}).status_code == 304
    one = client.get(f"/v1/prompt-sets/{s['id']}", headers=auth).json()
    assert one["schema"]["properties"]["width_mm"]["x-unit"] == "mm"


def test_extract_by_prompt_set_reports_set_revision_and_model(ctx):
    client, app, auth, model, _ = ctx
    sid = _publish(client, auth).json()["id"]
    r = client.post("/v1/extract", headers=auth, json={"folder_uid": "proj", "prompt_set": sid})
    assert r.status_code == 200, r.text
    events = dict((e, d) for e, d in sse_events(r.text) if e in ("plan", "item", "done"))
    assert events["plan"]["prompt_set"] == {"id": sid, "name": "Door datasheet",
                                            "slug": "reviera.door", "revision": 1,
                                            "current_revision": 1}
    assert events["plan"]["model"] == {"requested": "not-offered-here",
                                       "used": app.state.extract_models.default_id,
                                       "fallback": True}
    assert events["item"]["data"]["fire_rating"]["value"] == "EI 60"
    assert events["done"]["prompt_set"]["revision"] == 1 and events["done"]["model"]["fallback"]
    sent = model.calls[0][0]["content"]
    assert "Use the door in the title." in sent and "(unit: mm)" in sent
    run = app.state.headless_runs.runs["run1"]
    assert run["title"].startswith("Door datasheet r1") and run["request"]["prompt_set"]["id"] == sid


def test_extract_pins_a_revision_and_reports_the_newer_one(ctx):
    client, app, auth, model, _ = ctx
    sid = _publish(client, auth).json()["id"]
    client.put("/v1/prompt-sets/slug/reviera.door", headers=auth, json={**SET, "description": "2"})
    r = client.post("/v1/extract", headers={**auth, "Accept": "application/json"},
                    json={"files": ["d1"], "prompt_set": sid, "revision": 1})
    plan = r.json()["plan"]
    assert plan["prompt_set"]["revision"] == 1 and plan["prompt_set"]["current_revision"] == 2


@pytest.mark.parametrize("extra,status,needle", [
    ({"schema": SCHEMA}, 400, "not both"),
    ({"model": "x"}, 400, "may not be overridden"),
])
def test_extract_refuses_conflicting_parameters(ctx, extra, status, needle):
    client, app, auth, model, _ = ctx
    sid = _publish(client, auth).json()["id"]
    r = client.post("/v1/extract", headers=auth,
                    json={"files": ["d1"], "prompt_set": sid, **extra})
    assert r.status_code == status and needle in r.text


def test_extract_unknown_draft_and_retired_sets(ctx):
    client, app, auth, model, _ = ctx
    assert client.post("/v1/extract", headers=auth,
                       json={"files": ["d1"], "prompt_set": "ps_nope"}).status_code == 404
    draft = _publish(client, auth, slug="reviera.draft", publish="false").json()["id"]
    assert client.post("/v1/extract", headers=auth,
                       json={"files": ["d1"], "prompt_set": draft}).status_code == 404
    sid = _publish(client, auth).json()["id"]
    assert client.post("/v1/prompt-sets/slug/reviera.door/retire", headers=auth).status_code == 200
    r = client.post("/v1/extract", headers=auth, json={"files": ["d1"], "prompt_set": sid})
    assert r.status_code == 410 and r.json()["kind"] == "prompt_set_retired"


def test_a_raw_schema_request_may_prefer_a_model(ctx):
    client, app, auth, model, _ = ctx
    r = client.post("/v1/extract", headers={**auth, "Accept": "application/json"},
                    json={"files": ["d1"], "schema": SCHEMA, "model": "gpt-9"})
    assert r.json()["plan"]["model"]["fallback"] is True
    assert client.get("/v1/extract/models", headers=auth).json()["models"][0]["default"] is True


def test_builder_test_on_pasted_text_stores_nothing(ctx):
    client, app, auth, model, _ = ctx
    ed = _editor_headers()
    r = client.post("/v1/prompt-sets/test", headers={**ed, "Accept": "application/json"},
                    json={"set": SET, "text": "| Fire resistance | EI 60 |\n| Width | 926 mm |"})
    assert r.status_code == 200, r.text
    out = r.json()
    (item,) = out["items"]
    assert item["data"]["fire_rating"]["evidence"]["offset"] == 2
    assert item["data"]["width_mm"]["value"] == 926
    assert out["summary"]["test"] is True and out["plan"]["prompt_set"]["id"] is None
    assert app.state.headless_runs.runs == {}                 # no run record


def test_builder_test_on_a_stored_file_skips_the_cache(ctx):
    client, app, auth, model, _ = ctx
    puts = []

    class Cache:
        def get(self, *a):
            raise AssertionError("a test must not read the cache")

        def put(self, *a):
            puts.append(a)
    app.state.headless_cache = Cache()
    app.state.headless = None
    ed = _editor_headers()
    r = client.post("/v1/prompt-sets/test", headers={**ed, "Accept": "application/json"},
                    json={"set": SET, "file_uid": "d1"})
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["data"]["fire_rating"]["value"] == "EI 60" and puts == []


def test_tests_and_builder_writes_are_for_editors_in_the_web_app(ctx):
    client, app, auth, model, _ = ctx
    assert client.post("/v1/prompt-sets/test", headers=auth,
                       json={"set": SET, "text": "x"}).status_code == 403
    assert client.post("/v1/prompt-sets", headers=auth, json=SET).status_code == 403
    assert client.post("/v1/prompt-sets/import", headers=auth, content=YAML).status_code == 403


def test_yaml_export_and_import_over_http(ctx):
    client, app, auth, model, _ = ctx
    ed = _editor_headers()
    sid = _publish(client, auth).json()["id"]
    r = client.get(f"/v1/prompt-sets/{sid}/export", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/yaml")
    assert 'filename="Door datasheet.promptset.yaml"' in r.headers["content-disposition"]
    r = client.post("/v1/prompt-sets/import", headers=ed, content=r.text)
    assert r.status_code == 200, r.text
    (imp,) = r.json()["imported"]
    assert imp["created"] and imp["name"] == "Door datasheet (imported)"
    # upload form
    r = client.post("/v1/prompt-sets/import", headers=ed,
                    files={"file": ("door.yaml", YAML.encode(), "application/yaml")})
    assert r.status_code == 200 and len(r.json()["imported"]) == 1
    # a draft-only set is not visible to the application
    names = [s["name"] for s in client.get("/v1/prompt-sets", headers=auth).json()["prompt_sets"]]
    assert names == ["Door datasheet"]
