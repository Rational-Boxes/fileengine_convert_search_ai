"""Structured extraction (HEADLESS_RESEARCH_API.md §3.1, §3.7).

The engine against fakes: a core client with a folder tree, a permission gate,
a document store holding stored Markdown with its source version, the sidecar
cache and a scripted model. What is pinned is what the caller relies on:

- both target forms; a named id the caller cannot use is reported with ONE
  reason whether it is missing or unreadable;
- the wrapper is enforced: an absent value is found:false, an invented value
  (quote not in the document) is dropped and counted, a malformed reply gets one
  repair round trip, offsets point into the stored Markdown;
- stale text is re-indexed before it is read; no text is skipped, not "nothing
  found";
- a cache hit makes no model call; tokens are counted from provider usage; a
  spent budget skips the rest; cancellation stops between files."""
import json

import pytest

from convert_search_ai.headless import extract as ex
from convert_search_ai.headless import usage as u
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.providers.base import report_usage
from convert_search_ai.store import TextRow

ID = Identity(user="alice", tenant="t", authenticated=True, client_key_id="fesk_a",
              client_label="Reviera")

SCHEMA = {"type": "object", "properties": {
    "fire_rating": {"type": "string"},
    "width_mm": {"type": "number"},
    "epd_number": {"type": "string"}}}

DATASHEET = ("# Door D-90\n\n| Property | Value |\n| --- | --- |\n"
             "| Fire resistance | EI 60 |\n| Width | 926 mm |\n")


class Cfg:
    headless_files_per_run = 50
    headless_max_input_chars = 20000
    headless_max_schema_fields = 60
    headless_extract_window_chars = 60000
    chat_provider = "fake"


# ------------------------------------------------------------------ fakes

class Entry:
    def __init__(self, uid, name, container=False):
        self.uid, self.name, self.is_container = uid, name, container


class Info:
    def __init__(self, uid, name, version, is_dir=False):
        self.uid, self.name, self.version, self.is_dir = uid, name, version, is_dir


class Core:
    def __init__(self):
        self.tree = {}      # folder uid -> [Entry]
        self.info = {}      # uid -> Info

    def add(self, parent, uid, name, version="v1", folder=False):
        self.tree.setdefault(parent, []).append(Entry(uid, name, folder))
        self.info[uid] = Info(uid, name, version, folder)

    def dir(self, uid, tenant=None, **_):
        if uid not in self.tree and uid != "":
            raise FileNotFoundError(uid)
        return list(self.tree.get(uid, []))

    def stat(self, uid, tenant=None, **_):
        return self.info[uid]

    def close(self):
        pass


class Gate:
    def __init__(self, readable):
        self.readable = set(readable)

    def can_read(self, mf, identity, uid):
        return uid in self.readable


class Store:
    def __init__(self):
        self.rows = {}

    def get_text_row(self, tenant, uid):
        return self.rows.get(uid)


class Model:
    """Scripted complete_json: replies in order, reporting usage like a provider."""
    model_id = "fake-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, messages, *, system=None):
        self.calls.append(messages)
        report_usage(100, 20)
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


class Cache:
    def __init__(self):
        self.data = {}

    def get(self, uid, version, tenant, key):
        return self.data.get((uid, version, key))

    def put(self, uid, version, tenant, key, entry):
        self.data[(uid, version, key)] = entry
        return True


def good_reply():
    return {"fire_rating": {"found": True, "value": "EI 60", "unit": None,
                            "evidence": {"quote": "Fire resistance | EI 60"}},
            "width_mm": {"found": True, "value": 926, "unit": "mm",
                         "evidence": {"quote": "Width | 926 mm"}},
            "epd_number": {"found": False, "value": None, "unit": None, "evidence": None}}


def build(core=None, readable=None, model=None, cache=None, convert=None, meter=None):
    core = core or Core()
    store = Store()
    e = ex.Extractor(Cfg(), client_for=lambda ident: core, gate=Gate(readable or []),
                     store=store, chat=model or Model(), cache=cache, convert=convert,
                     meter=meter)
    return e, core, store


def req(**kw):
    body = {"schema": SCHEMA, **kw}
    return ex.parse_request(body, Cfg())


def run(e, r, cancelled=lambda: False):
    events = []
    summary = e.run(ID, r, lambda ev, d: events.append((ev, d)), cancelled)
    return events, summary


# ------------------------------------------------------------------ request

@pytest.mark.parametrize("body,msg", [
    ({"schema": SCHEMA}, "exactly one target"),
    ({"schema": SCHEMA, "folder_uid": "f", "files": ["a"]}, "exactly one target"),
    ({"schema": SCHEMA, "files": []}, "files is empty"),
    ({"schema": SCHEMA, "files": [""]}, "list of document ids"),
    ({"schema": {"type": "string"}, "files": ["a"]}, "object schema"),
    ({"schema": {"type": "object", "properties": {"bad name!": {}}}, "files": ["a"]}, "not allowed"),
    ({"schema": {"type": "object", "properties": {"x": {"type": 5}}}, "files": ["a"]}, "not valid JSON Schema"),
    ({"schema": SCHEMA, "files": ["a"], "cache": "never"}, "cache must be"),
])
def test_bad_requests_are_refused_with_a_reason(body, msg):
    with pytest.raises(ex.ExtractRequestError) as e:
        ex.parse_request(body, Cfg())
    assert msg in str(e.value)


def test_files_are_deduplicated_in_order_and_capped():
    r = req(files=["b", "a", "b"])
    assert r.files == ["b", "a"] and r.folder_uid is None
    with pytest.raises(ex.ExtractRequestError):
        req(files=[f"f{i}" for i in range(51)])


def test_root_folder_is_a_valid_target():
    assert req(folder_uid="").folder_uid == ""


def test_cache_key_changes_with_the_question_and_the_model():
    a = ex.cache_key(req(files=["a"]), "m1")
    assert a == ex.cache_key(req(files=["zzz"]), "m1")          # target is not the question
    assert a != ex.cache_key(req(files=["a"], instructions="door D-90"), "m1")
    assert a != ex.cache_key(req(files=["a"]), "m2")


# ------------------------------------------------------------------ checking

def test_locate_tolerates_markdown_table_quoting_and_case():
    assert ex.locate("Fire resistance EI 60", DATASHEET) == DATASHEET.index("Fire resistance")
    assert ex.locate("fire resistance | ei 60", DATASHEET) is not None
    assert ex.locate("EI 90", DATASHEET) is None
    assert ex.locate("", DATASHEET) is None


def test_check_enforces_the_wrapper():
    r = req(files=["a"])
    raw = good_reply()
    raw["epd_number"] = {"found": True, "value": "EPD-123", "unit": None,
                         "evidence": {"quote": "EPD number EPD-123"}}   # invented
    data, dropped, problems = ex.check(raw, r, DATASHEET)
    assert data["fire_rating"]["found"] and data["fire_rating"]["evidence"]["offset"] == \
        DATASHEET.index("Fire resistance")
    assert data["width_mm"] == {"found": True, "value": 926, "unit": "mm",
                                "evidence": {"quote": "Width | 926 mm",
                                             "offset": DATASHEET.index("Width")}}
    assert dropped == ["epd_number"] and data["epd_number"]["dropped"] == "no_evidence"
    assert data["epd_number"]["value"] is None
    assert problems == []


def test_check_reports_schema_mismatch_and_missing_fields_for_repair():
    raw = good_reply()
    raw["width_mm"]["value"] = "926 mm"            # string for a number
    del raw["epd_number"]
    _data, _d, problems = ex.check(raw, req(files=["a"]), DATASHEET)
    assert any("width_mm" in p for p in problems)
    assert any("missing field 'epd_number'" in p for p in problems)


def test_parse_json_tolerates_fences_and_preamble():
    assert ex.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ex.parse_json('Here you go: {"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        ex.parse_json("no json here")


def test_windows_cover_the_text_with_overlap_and_cut_at_lines():
    text = "\n".join(f"line {i:05d}" for i in range(5000))
    parts = ex.windows(text, 10000)
    assert len(parts) > 1 and all(len(p) <= 10000 for p in parts)
    assert parts[0].endswith(tuple("0123456789")) and "line 00000" in parts[0]
    assert "line 04999" in parts[-1]


# ------------------------------------------------------------------ runs

def _datasheet_folder():
    core = Core()
    core.add("", "proj", "Project", folder=True)
    core.add("proj", "d1", "door-datasheet.pdf", "v1")
    core.add("proj", "d2", "notes.txt", "v1")
    core.add("proj", "sub", "Sub", folder=True)
    core.add("sub", "d3", "window-datasheet.pdf", "v7")
    core.add("proj", "secret", "secret-datasheet.pdf", "v1")
    return core


def test_folder_target_recursive_with_match_and_unreadable_left_out_silently():
    model = Model(good_reply(), good_reply())
    e, core, store = build(core=_datasheet_folder(), readable={"d1", "d2", "d3"}, model=model)
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed"),
                  "d3": TextRow(DATASHEET, "v7", "indexed")}
    events, summary = run(e, req(folder_uid="proj", recursive=True,
                                 match={"extensions": ["pdf"], "name": "*datasheet*"}))
    plan = events[0][1]
    assert [f["file_uid"] for f in plan["files"]] == ["d1", "d3"]   # notes.txt unmatched, secret unreadable
    assert plan["skipped"] == []
    items = [d for ev, d in events if ev == "item"]
    assert [i["file_uid"] for i in items] == ["d1", "d3"]
    assert items[1]["version"] == "v7" and items[0]["counts"] == {
        "found": 2, "not_found": 1, "dropped_no_evidence": 0}
    assert summary["extracted"] == 2 and summary["found"] == 4 and summary["tokens"] == 240


def test_named_ids_report_one_reason_for_missing_and_unreadable():
    core = _datasheet_folder()
    e, _, store = build(core=core, readable={"d1", "proj"}, model=Model(good_reply()))
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed")}
    events, summary = run(e, req(files=["nope", "secret", "proj", "d1"]))
    plan = events[0][1]
    assert plan["skipped"] == [
        {"file_uid": "nope", "reason": "not found or not readable"},
        {"file_uid": "secret", "reason": "not found or not readable"},
        {"file_uid": "proj", "reason": "not a document"}]
    assert [d["file_uid"] for ev, d in events if ev == "item" and "data" in d] == ["d1"]
    assert summary["skipped"] == 3 and summary["extracted"] == 1


def test_stale_text_is_reindexed_before_it_is_read():
    core = _datasheet_folder()
    converted = []
    e, _, store = build(core=core, readable={"d1"}, model=Model(good_reply()),
                        convert=lambda uid, tenant: (converted.append(uid),
                                                     store.rows.__setitem__(
                                                         uid, TextRow(DATASHEET, "v1", "indexed"))))
    store.rows = {"d1": TextRow("old text", "v0", "indexed")}      # older version's text
    events, _ = run(e, req(files=["d1"]))
    assert converted == ["d1"]
    assert [d for ev, d in events if ev == "item"][0]["counts"]["found"] == 2


def test_no_text_is_skipped_with_the_reason_never_nothing_found():
    core = _datasheet_folder()
    model = Model()
    e, _, store = build(core=core, readable={"d1", "d3"}, model=model)
    store.rows = {"d1": TextRow(None, "v1", "unsupported"),       # a scan, no text layer
                  "d3": TextRow("x", "v6", "indexed")}             # never caught up
    events, summary = run(e, req(files=["d1", "d3"]))
    items = [d for ev, d in events if ev == "item"]
    assert items[0]["skipped"] == "no text"
    assert items[1]["skipped"] == "text not yet indexed for this version"
    assert model.calls == [] and summary["skipped"] == 2


def test_malformed_reply_gets_one_repair():
    core = _datasheet_folder()
    model = Model("sorry, here is prose", good_reply())
    e, _, store = build(core=core, readable={"d1"}, model=model)
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed")}
    events, _ = run(e, req(files=["d1"]))
    item = [d for ev, d in events if ev == "item"][0]
    assert len(model.calls) == 2 and item["valid"] and item["counts"]["found"] == 2
    assert "cannot be used" in model.calls[1][-1]["content"]


def test_still_invalid_after_repair_is_returned_invalid_and_not_cached():
    core = _datasheet_folder()
    cache = Cache()
    model = Model("nope", "still nope")
    e, _, store = build(core=core, readable={"d1"}, model=model, cache=cache)
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed")}
    events, summary = run(e, req(files=["d1"]))
    item = [d for ev, d in events if ev == "item"][0]
    assert item["valid"] is False and item["problems"] and cache.data == {}
    assert all(not v["found"] for v in item["data"].values())


def test_cache_hit_makes_no_model_call_and_counts_saved_tokens():
    core = _datasheet_folder()
    cache = Cache()
    m, _ = (u.UsageMeter(u.Limits(60, 10_000, 2, 50), u.MemoryBackend()), None)
    first = Model(good_reply())
    e, _, store = build(core=core, readable={"d1"}, model=first, cache=cache, meter=m)
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed")}
    run(e, req(files=["d1"]))
    assert len(cache.data) == 1
    entry = next(iter(cache.data.values()))
    assert entry["created_by"] == "alice" and entry["client_key_id"] == "fesk_a"
    assert entry["prompt_version"] == ex.PROMPT_VERSION and entry["tokens"] == 120

    second = Model()
    e2 = ex.Extractor(Cfg(), client_for=lambda i: core, gate=Gate({"d1"}), store=store,
                      chat=second, cache=cache, meter=m)
    events, summary = run(e2, req(files=["d1"]))
    item = [d for ev, d in events if ev == "item"][0]
    assert item["cached"] is True and second.calls == []
    assert summary["cached"] == 1
    st = m.status(ID)
    assert st["cache"] == {"hits": 1, "tokens_saved_estimate": 120}
    assert st["limits"]["tokens"]["used"] == 120          # only the first run spent

    third = Model(good_reply())
    e3 = ex.Extractor(Cfg(), client_for=lambda i: core, gate=Gate({"d1"}), store=store,
                      chat=third, cache=cache, meter=m)
    run(e3, req(files=["d1"], cache="refresh"))
    assert len(third.calls) == 1                           # refresh re-extracts


def test_spent_budget_skips_the_remaining_files():
    core = _datasheet_folder()
    # 100 tokens a day; the first file spends 120, so the second must not run.
    m = u.UsageMeter(u.Limits(60, 100, 2, 50), u.MemoryBackend())
    e, _, store = build(core=core, readable={"d1", "d3"}, model=Model(good_reply()), meter=m)
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed"),
                  "d3": TextRow(DATASHEET, "v7", "indexed")}
    events, summary = run(e, req(files=["d1", "d3"]))
    items = [d for ev, d in events if ev == "item"]
    assert "data" in items[0] and items[1]["skipped"] == "budget"


def test_cancellation_stops_between_files():
    core = _datasheet_folder()
    model = Model(good_reply(), good_reply())
    e, _, store = build(core=core, readable={"d1", "d3"}, model=model)
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed"),
                  "d3": TextRow(DATASHEET, "v7", "indexed")}
    seen = []
    events, summary = run(e, req(files=["d1", "d3"]), cancelled=lambda: len(seen) >= 1
                          or seen.append(1) is not None and False)
    assert summary["cancelled"] is True and len(model.calls) == 1


def test_long_documents_are_read_in_windows_and_merged():
    core = _datasheet_folder()
    long = DATASHEET + "\n" + ("filler line\n" * 2000) + "| EPD | EPD-778 |\n"
    first = {"fire_rating": good_reply()["fire_rating"], "width_mm": good_reply()["width_mm"],
             "epd_number": {"found": False, "value": None, "unit": None, "evidence": None}}
    second = {"fire_rating": {"found": False, "value": None, "unit": None, "evidence": None},
              "width_mm": {"found": False, "value": None, "unit": None, "evidence": None},
              "epd_number": {"found": True, "value": "EPD-778", "unit": None,
                             "evidence": {"quote": "EPD | EPD-778"}}}

    class SmallWindow(Cfg):
        headless_extract_window_chars = 12000
    model = Model(first, second, second, second)
    e = ex.Extractor(SmallWindow(), client_for=lambda i: core, gate=Gate({"d1"}),
                     store=Store(), chat=model)
    e.store.rows = {"d1": TextRow(long, "v1", "indexed")}
    events, _ = run(e, req(files=["d1"]))
    item = [d for ev, d in events if ev == "item"][0]
    assert len(model.calls) >= 2
    assert item["counts"]["found"] == 3
    assert item["data"]["epd_number"]["evidence"]["offset"] == long.index("EPD | EPD-778")


def test_fields_still_omitted_after_repair_are_not_found_and_cached():
    # Measured on DeepSeek V4 Flash: a document with none of the fields often
    # answers {} even when asked again. That is "nothing found", not invalid.
    core = _datasheet_folder()
    cache = Cache()
    model = Model({}, {})
    e, _, store = build(core=core, readable={"d1"}, model=model, cache=cache)
    store.rows = {"d1": TextRow("Meeting notes about lunch.", "v1", "indexed")}
    events, _ = run(e, req(files=["d1"]))
    item = [d for ev, d in events if ev == "item"][0]
    assert len(model.calls) == 2                    # the repair was still tried
    assert item["valid"] is True and item["counts"]["not_found"] == 3
    assert len(cache.data) == 1


def test_a_wrong_type_after_repair_stays_invalid():
    core = _datasheet_folder()
    bad = good_reply()
    bad["width_mm"]["value"] = "wide"
    model = Model(bad, bad)
    e, _, store = build(core=core, readable={"d1"}, model=model)
    store.rows = {"d1": TextRow(DATASHEET, "v1", "indexed")}
    events, _ = run(e, req(files=["d1"]))
    item = [d for ev, d in events if ev == "item"][0]
    assert item["valid"] is False and any("width_mm" in p for p in item["problems"])
