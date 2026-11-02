"""The marked-up PDF through the routes (EXTRACTION_PROMPT_SETS.md §5).

- a prompt-set run writes ``<version>-extractpdf_<set>.pdf`` with the evidence
  highlighted; a raw-schema run does so only when asked;
- the item reports where each field was found, or why there is no PDF;
- a cache hit reuses the existing PDF instead of writing another version;
- a builder test returns a short-lived download for its own editor only, and
  writes nothing;
- the name round-trips through the rendition parser, so it is pruned with its
  version, and it is never the user-markup fmt."""
import pymupdf
import pytest

from convert_search_ai.headless.promptset_store import MemoryPromptSetStore
from convert_search_ai.renditions import parse_rendition_name

from test_headless_extract import SCHEMA
from test_headless_routes import env, sse_events  # noqa: F401 (fixture)
from test_promptset_routes import SET, _editor_headers


def datasheet_pdf():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 100), "Fire resistance", fontsize=11)
    page.insert_text((300, 100), "EI 60", fontsize=11)
    page.insert_text((50, 120), "Width", fontsize=11)
    page.insert_text((300, 120), "926 mm", fontsize=11)
    out = doc.tobytes()
    doc.close()
    return out


class FakeMarkupStore:
    def __init__(self, pdf=None, why="no_pdf"):
        self.pdf, self.why = pdf, why
        self.writes, self.sources = [], []
        self.files = {}

    def source(self, file_uid, version, name, tenant):
        self.sources.append(file_uid)
        return (self.pdf, "") if self.pdf else (None, self.why)

    def existing(self, file_uid, version, fmt, tenant):
        name = f"{version}-{fmt}.pdf"
        return (self.files[name], name) if name in self.files else None

    def write(self, file_uid, version, fmt, data, tenant):
        name = f"{version}-{fmt}.pdf"
        self.files[name] = f"uid-{len(self.files) + 1}"
        self.writes.append((file_uid, name, data))
        return self.files[name], name


@pytest.fixture
def ctx(env):
    client, app, auth, model = env
    app.state.promptset_store = MemoryPromptSetStore()
    app.state.promptsets = None
    store = FakeMarkupStore(datasheet_pdf())
    app.state.headless_markup = store
    app.state.headless = None
    return client, app, auth, model, store


def _publish(client, auth):
    r = client.put("/v1/prompt-sets/slug/reviera.door", headers=auth,
                   json={**SET, "markup": {"colours": {"fire_rating": "#123456"}}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _item(client, auth, body):
    r = client.post("/v1/extract", headers={**auth, "Accept": "application/json"}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def test_a_prompt_set_run_writes_the_marked_up_pdf(ctx):
    client, app, auth, model, store = ctx
    sid = _publish(client, auth)
    item = _item(client, auth, {"files": ["d1"], "prompt_set": sid})
    m = item["markup"]
    assert m["status"] == "written"
    assert m["name"] == f"v1-extractpdf_{sid[3:]}.pdf"
    assert m["fields"]["fire_rating"]["located"] and m["fields"]["fire_rating"]["page"] == 1
    assert m["fields"]["epd_number"] == {"located": False, "reason": "not_found"}
    (_uid, name, data), = store.writes
    doc = pymupdf.open(stream=data, filetype="pdf")
    page = doc[0]
    notes = {a.info["content"] for a in page.annots()}
    assert "Fire rating: EI 60" in notes
    assert "Door datasheet" in doc[-1].get_text()                     # the legend page
    assert parse_rendition_name(name) == ("v1", f"extractpdf_{sid[3:]}", "pdf")


def test_a_raw_schema_run_marks_up_only_when_asked(ctx):
    client, app, auth, model, store = ctx
    item = _item(client, auth, {"files": ["d1"], "schema": SCHEMA})
    assert "markup" not in item and store.writes == []
    item = _item(client, auth, {"files": ["d1"], "schema": SCHEMA, "markup": True})
    assert item["markup"]["status"] == "written"
    assert item["markup"]["name"].startswith("v1-extractpdf_s")


def test_a_document_without_a_pdf_says_so(ctx):
    client, app, auth, model, store = ctx
    store.pdf, store.why = None, "no_pdf"
    sid = _publish(client, auth)
    item = _item(client, auth, {"files": ["d1"], "prompt_set": sid})
    assert item["markup"] == {"status": "no_pdf"}
    assert item["data"]["fire_rating"]["value"] == "EI 60"              # the values stand


def test_a_cache_hit_reuses_the_existing_pdf(ctx):
    client, app, auth, model, store = ctx
    entries = {}

    class Cache:
        def get(self, file_uid, version, tenant, key):
            return entries.get(key)

        def put(self, file_uid, version, tenant, key, entry):
            entries[key] = entry
            return True
    app.state.headless_cache = Cache()
    app.state.headless = None
    sid = _publish(client, auth)
    first = _item(client, auth, {"files": ["d1"], "prompt_set": sid})
    second = _item(client, auth, {"files": ["d1"], "prompt_set": sid})
    assert second["cached"] is True
    assert second["markup"] == {"status": "written", "uid": first["markup"]["uid"],
                                "name": first["markup"]["name"]}
    assert len(store.writes) == 1 and len(store.sources) == 1


def test_a_builder_test_returns_a_download_for_its_editor_only(ctx):
    client, app, auth, model, store = ctx
    ed = _editor_headers()
    r = client.post("/v1/prompt-sets/test", headers={**ed, "Accept": "application/json"},
                    json={"set": SET, "file_uid": "d1"})
    assert r.status_code == 200, r.text
    m = r.json()["items"][0]["markup"]
    assert m["status"] == "ready" and store.writes == []                # nothing stored
    url = m["download"]
    pdf = client.get(url, headers=ed)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert client.get(url, headers=auth).status_code == 404              # another user


def test_pasted_text_has_no_pdf(ctx):
    client, app, auth, model, store = ctx
    r = client.post("/v1/prompt-sets/test", headers={**_editor_headers(),
                                                    "Accept": "application/json"},
                    json={"set": SET, "text": "| Fire resistance | EI 60 |"})
    assert "markup" not in r.json()["items"][0] and store.sources == []


def test_the_family_is_never_the_user_markup_fmt():
    assert parse_rendition_name("v1-extractpdf_abc123.pdf") == ("v1", "extractpdf_abc123", "pdf")
    assert parse_rendition_name("alice_20261102-markup.pdf") is None    # user copies: never pruned
    assert parse_rendition_name("v1-extractpdf_.pdf") is None
    assert parse_rendition_name("v1-extractpdf_Bad-Name.pdf") is None
