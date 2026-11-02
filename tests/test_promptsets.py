"""Prompt-set definitions, the extraction model list, and the service rules
(EXTRACTION_PROMPT_SETS.md §4)."""
import json
from types import SimpleNamespace as NS

import pytest
from jsonschema import Draft202012Validator

from convert_search_ai.headless import promptsets as ps
from convert_search_ai.headless.models import ModelConfigError, ModelRegistry
from convert_search_ai.headless.promptset_service import PromptSetService, ServiceError
from convert_search_ai.headless.promptset_store import MemoryPromptSetStore
from convert_search_ai.ldap_auth import Identity

DOOR = {
    "name": "Door datasheet", "description": "Fire doors", "slug": "reviera.door-datasheet",
    "instructions": "Use the door named in the file name.", "model": "deepseek-v4-flash",
    "applies_to": {"classifier_set": "Construction", "labels": ["door_datasheet"]},
    "markup": {"colours": {"fire_rating": "#E8590C"}},
    "fields": [
        {"name": "fire_rating", "label": "Fire rating", "type": "enum",
         "enum": ["EI 30", "EI 60", "EI 60"], "required": True,
         "description": "EN 13501-2 class"},
        {"name": "width", "type": "number", "unit": "mm", "minimum": 300},
        {"name": "manufacturer", "type": "string", "required": True},
        {"name": "certificate_expiry", "type": "date"},
        {"name": "fsc", "type": "boolean"},
        {"name": "notes", "type": "text"},
        {"name": "standards", "type": "list"},
        {"name": "leaves", "type": "integer", "maximum": 2},
    ],
}


# ------------------------------------------------------------------ definitions

def test_normalise_is_canonical_and_compiles_to_valid_json_schema():
    body = ps.normalise(DOOR)
    assert body["fields"][0]["enum"] == ["EI 30", "EI 60"]             # de-duplicated
    assert body["markup"]["colours"] == {"fire_rating": "#e8590c"}
    schema = ps.compile_schema(body)
    Draft202012Validator.check_schema(schema)
    props = schema["properties"]
    assert props["fire_rating"] == {"type": "string", "enum": ["EI 30", "EI 60"],
                                    "title": "Fire rating", "description": "EN 13501-2 class"}
    assert props["width"]["type"] == "number" and props["width"]["minimum"] == 300
    assert "(unit: mm)" in props["width"]["description"] and props["width"]["x-unit"] == "mm"
    assert props["certificate_expiry"]["format"] == "date"
    assert props["standards"] == {"type": "array", "items": {"type": "string"}}
    assert props["leaves"]["type"] == "integer"
    assert "required" not in schema          # the model still answers found:false


def test_equal_definitions_hash_equal_and_changes_do_not():
    a = ps.definition_hash(ps.normalise(DOOR))
    reordered = dict(reversed(list(DOOR.items())))
    assert ps.definition_hash(ps.normalise(reordered)) == a
    changed = json.loads(json.dumps(DOOR))
    changed["fields"][1]["unit"] = "cm"
    assert ps.definition_hash(ps.normalise(changed)) != a


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(name=""), "needs a name"),
    (lambda d: d.update(fields=[]), "at least one field"),
    (lambda d: d["fields"].append({"name": "width", "type": "number"}), "appears twice"),
    (lambda d: d["fields"].append({"name": "9bad"}), "not allowed"),
    (lambda d: d["fields"].append({"name": "x", "type": "blob"}), "is not one of"),
    (lambda d: d["fields"].append({"name": "x", "type": "enum"}), "list of values"),
    (lambda d: d["fields"].append({"name": "x", "type": "string", "unit": "mm"}), "unit"),
    (lambda d: d["fields"].append({"name": "x", "type": "number", "minimum": 5, "maximum": 1}),
     "minimum is greater"),
    (lambda d: d.update(slug="Bad Slug"), "slug"),
    (lambda d: d.update(markup={"colours": {"nope": "#000000"}}), "unknown field"),
    (lambda d: d.update(markup={"colours": {"width": "red"}}), "#rrggbb"),
])
def test_invalid_definitions_name_the_set_and_field(mutate, needle):
    d = json.loads(json.dumps(DOOR))
    mutate(d)
    with pytest.raises(ps.DefinitionError) as e:
        ps.normalise(d)
    assert needle in str(e.value)


def test_field_and_size_limits():
    many = {**DOOR, "fields": [{"name": f"f{i}"} for i in range(5)]}
    with pytest.raises(ps.DefinitionError, match="more than 4 fields"):
        ps.normalise(many, max_fields=4)
    with pytest.raises(ps.DefinitionError, match="exceed 300"):
        ps.normalise({**DOOR, "instructions": "x" * 250}, max_chars=300)


def test_yaml_round_trip_for_every_field_type():
    body = ps.normalise(DOOR)
    text = ps.dump_yaml([ps.to_export(body, revision=3)])
    assert "format: fileengine.prompt_set.v1" in text
    (raw, warnings), = ps.parse_import(text)
    assert warnings == [] and ps.normalise(raw) == body
    again = ps.dump_yaml([ps.to_export(ps.normalise(raw), revision=3)])
    strip = lambda t: "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("at:"))
    assert strip(again) == strip(text)


def test_multi_set_file_round_trips():
    a = ps.normalise(DOOR)
    b = ps.normalise({**DOOR, "name": "Window", "slug": "", "fields": [{"name": "u_value",
                                                                      "type": "number"}],
                      "markup": {}})
    text = ps.dump_yaml([ps.to_export(a), ps.to_export(b)])
    got = [ps.normalise(r) for r, _ in ps.parse_import(text)]
    assert got == [a, b]


def test_legacy_extractor_files_import_as_text_fields_with_a_warning():
    legacy = """
name: EPD Type
type: extractor
prompt: Is the product a Type III EPD?
fields:
  - {name: internal, description: yes or no for Internally Reviewed}
  - {name: external, description: yes or no for Externally Reviewed}
"""
    (raw, warnings), = ps.parse_import(legacy)
    body = ps.normalise(raw)
    assert body["instructions"] == "Is the product a Type III EPD?"
    assert [(f["name"], f["type"]) for f in body["fields"]] == [("internal", "string"),
                                                                ("external", "string")]
    assert "set the field types" in warnings[0]
    # the older JSON form, without `type`
    (raw2, w2), = ps.parse_import(json.dumps({"name": "x", "prompt": "q",
                                              "fields": [{"name": "a", "description": "d"}]}))
    assert ps.normalise(raw2)["instructions"] == "q" and w2


@pytest.mark.parametrize("text,needle", [
    ("format: other.v9\nname: x", "unknown format"),
    ("type: classifier\nname: x", "is not a prompt set"),
    ("[: bad", "not valid YAML"),
    ("just a string", "does not hold"),
])
def test_bad_files_are_refused(text, needle):
    with pytest.raises(ps.DefinitionError, match=needle):
        ps.parse_import(text)


def test_file_size_and_set_count_caps():
    with pytest.raises(ps.DefinitionError, match="larger than"):
        ps.parse_import("a: " + "x" * (ps.MAX_FILE_BYTES + 1))
    many = {"type": ps.TYPE_MANY, "sets": [{"name": f"s{i}", "fields": [{"name": "a"}]}
                                           for i in range(ps.MAX_SETS_PER_FILE + 1)]}
    with pytest.raises(ps.DefinitionError, match="more than"):
        ps.parse_import(json.dumps(many))


# ------------------------------------------------------------------ models

def cfg(**kw):
    base = dict(chat_model="deepseek-ai/DeepSeek-V4-Flash", chat_provider="deepinfra",
                chat_base_url="https://api.deepinfra.com/v1/openai", chat_api_key="k",
                chat_max_tokens=4096, chat_json_mode=True, chat_stream_usage=True,
                extract_models="", extract_default_model="",
                headless_max_schema_fields=60, headless_max_input_chars=20000,
                prompt_set_app_max_sets=50, prompt_set_app_revisions_per_day=20)
    base.update(kw)
    return NS(**base)


MODELS = json.dumps([
    {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash",
     "model": "deepseek-ai/DeepSeek-V4-Flash"},
    {"id": "qwen3", "label": "Qwen3", "model": "Qwen/Qwen3-235B"},
    {"id": "claude", "label": "Claude", "provider": "anthropic", "model": "claude-sonnet-5-5",
     "api_key_env": "SOME_KEY_VAR"},
])


def test_unset_list_is_the_chat_model_alone():
    r = ModelRegistry.from_config(cfg())
    assert r.public() == [{"id": "deepseek-ai/DeepSeek-V4-Flash",
                           "label": "deepseek-ai/DeepSeek-V4-Flash", "default": True}]
    sentinel = object()
    assert r.provider(r.by_id[r.default_id], sentinel) is sentinel


def test_listed_models_and_fallback_to_the_default():
    r = ModelRegistry.from_config(cfg(extract_models=MODELS, extract_default_model="qwen3"))
    assert [m["id"] for m in r.public()] == ["deepseek-v4-flash", "qwen3", "claude"]
    assert [m["default"] for m in r.public()] == [False, True, False]
    e, rep = r.resolve("claude")
    assert e["id"] == "claude" and rep == {"requested": "claude", "used": "claude",
                                           "fallback": False}
    e, rep = r.resolve("gpt-9")
    assert e["id"] == "qwen3" and rep == {"requested": "gpt-9", "used": "qwen3", "fallback": True}
    e, rep = r.resolve(None)
    assert rep == {"requested": None, "used": "qwen3", "fallback": False}


def test_public_list_never_shows_provider_details_or_keys():
    r = ModelRegistry.from_config(cfg(extract_models=MODELS))
    text = json.dumps(r.public())
    for secret in ("SOME_KEY_VAR", "anthropic", "base_url", "Qwen/Qwen3-235B"):
        assert secret not in text


def test_the_chat_model_entry_reuses_the_app_provider_and_others_are_built(monkeypatch):
    r = ModelRegistry.from_config(cfg(extract_models=MODELS))
    sentinel = object()
    assert r.provider(r.by_id["deepseek-v4-flash"], sentinel) is sentinel
    built = []
    monkeypatch.setenv("SOME_KEY_VAR", "sk-test")
    monkeypatch.setattr("convert_search_ai.providers.factory.make_chat_provider",
                        lambda c: built.append(c) or ("provider", c.chat_model))
    p = r.provider(r.by_id["claude"], sentinel)
    assert p == ("provider", "claude-sonnet-5-5") and r.provider(r.by_id["claude"], sentinel) is p
    (c,) = built
    assert c.chat_provider == "anthropic" and c.anthropic_api_key_override == "sk-test"


@pytest.mark.parametrize("raw,default,needle", [
    ("not json", "", "not valid JSON"),
    ("[]", "", "non-empty"),
    (json.dumps([{"id": "a", "model": "m"}, {"id": "a", "model": "n"}]), "", "twice"),
    (json.dumps([{"id": "a"}]), "", "model is required"),
    (json.dumps([{"id": "a", "model": "m", "api_key": "sk-123"}]), "", "unknown keys"),
    (json.dumps([{"id": "a", "model": "m"}]), "b", "is not in"),
])
def test_a_broken_list_refuses_to_start(raw, default, needle):
    with pytest.raises(ModelConfigError, match=needle):
        ModelRegistry.from_config(cfg(extract_models=raw, extract_default_model=default))


# ------------------------------------------------------------------ service

EDITOR = Identity(user="ed", tenant="t", authenticated=True, roles=["tenant_admin"])
MEMBER = Identity(user="mo", tenant="t", authenticated=True, roles=["users"])
KEY = Identity(user="mo", tenant="t", authenticated=True, roles=["users"],
               client_key_id="fesk_1", client_label="Reviera laptop")
KEY2 = Identity(user="al", tenant="t", authenticated=True, roles=["users"],
                client_key_id="fesk_2", client_label="Reviera desktop")
EDITOR_KEY = Identity(user="ed", tenant="t", authenticated=True, roles=["tenant_admin"],
                      client_key_id="fesk_9", client_label="Reviera")


def svc(**kw):
    c = cfg(extract_models=MODELS, **kw)
    return PromptSetService(MemoryPromptSetStore(), ModelRegistry.from_config(c), c)


def err(fn, *a, **kw):
    with pytest.raises(ServiceError) as e:
        fn(*a, **kw)
    return e.value


def test_builder_draft_publish_and_what_each_caller_sees():
    s = svc()
    made = s.create(EDITOR, DOOR)
    sid = made["id"]
    assert made["draft_revision"] == 1
    assert s.list(KEY)["prompt_sets"] == []                     # a draft is invisible
    assert s.list(MEMBER)["prompt_sets"] == []
    assert s.list(EDITOR)["prompt_sets"][0]["draft_revision"] == 1
    assert err(s.get, KEY, sid).status == 404
    s.publish(EDITOR, sid)
    (v,) = s.list(KEY)["prompt_sets"]
    assert v["revision"] == 1 and v["fields"][0]["name"] == "fire_rating"
    assert v["model"] == {"requested": "deepseek-v4-flash", "used": "deepseek-v4-flash",
                          "fallback": False, "label": "DeepSeek V4 Flash"}
    assert "locked" not in v and "draft_revision" not in v      # builder detail hidden
    full = s.get(KEY, sid)
    assert full["schema"]["properties"]["width"]["type"] == "number"
    # a new draft does not change what applications see
    s.save_draft(EDITOR, sid, {**DOOR, "instructions": "changed"})
    assert s.get(KEY, sid)["instructions"] == DOOR["instructions"]
    assert s.list(EDITOR)["prompt_sets"][0]["draft_revision"] == 2
    s.publish(EDITOR, sid)
    assert s.get(KEY, sid)["revision"] == 2
    assert s.get(KEY, sid, revision=1)["instructions"] == DOOR["instructions"]
    assert [r["revision"] for r in s.revisions(KEY, sid)] == [1, 2]


def test_only_editors_in_the_web_application_use_the_builder():
    s = svc()
    assert err(s.create, MEMBER, DOOR).status == 403
    assert err(s.create, EDITOR_KEY, DOOR).status == 403      # a key session uses the slug PUT
    assert err(s.import_yaml, KEY, "name: x").status == 403


def test_saving_a_draft_twice_edits_the_same_draft():
    s = svc()
    sid = s.create(EDITOR, DOOR)["id"]
    s.save_draft(EDITOR, sid, {**DOOR, "description": "one"})
    s.save_draft(EDITOR, sid, {**DOOR, "description": "two"})
    revs = s.revisions(EDITOR, sid)
    assert [(r["revision"], r["state"]) for r in revs] == [(1, "draft")]


def test_an_unconfigured_model_is_a_warning_and_falls_back():
    s = svc()
    made = s.create(EDITOR, {**DOOR, "model": "gpt-9"})
    assert made["warnings"][0]["kind"] == "model_fallback"
    s.publish(EDITOR, made["id"])
    assert s.get(KEY, made["id"])["model"]["fallback"] is True


def test_retire_and_resolve_for_runs():
    s = svc()
    a = s.create(EDITOR, DOOR)["id"]
    b = s.create(EDITOR, {**DOOR, "name": "Door v2", "slug": "reviera.door-v2"})["id"]
    s.publish(EDITOR, a)
    s.publish(EDITOR, b)
    info, body = s.resolve_for_run(KEY, a, None)
    assert info["revision"] == 1 and body["name"] == "Door datasheet"
    s.retire(EDITOR, a, replaced_by=b)
    e = err(s.resolve_for_run, KEY, a, None)
    assert e.status == 410 and e.extra["replaced_by"] == b
    assert [v["id"] for v in s.list(KEY)["prompt_sets"]] == [b]
    assert err(s.resolve_for_run, KEY, b, 7).status == 404


def test_export_import_as_drafts_with_conflict_choices():
    s = svc()
    sid = s.create(EDITOR, DOOR)["id"]
    s.publish(EDITOR, sid)
    text = s.export(KEY, [sid])
    assert "reviera.door-datasheet" in text
    out = s.import_yaml(EDITOR, text)["imported"][0]
    assert out["created"] and out["name"] == "Door datasheet (imported)"
    assert out["warnings"][0]["kind"] == "slug_dropped"       # slugs are unique per tenant
    assert s.get(EDITOR, out["id"])["state"] == "draft"        # never published by import
    rev = s.import_yaml(EDITOR, text, on_conflict="revision")["imported"][0]
    assert rev["id"] == sid and not rev["created"] and rev["draft_revision"] == 2


def test_an_invalid_file_imports_nothing():
    s = svc()
    good = ps.to_export(ps.normalise(DOOR))
    bad = {"name": "broken", "fields": [{"name": "x", "type": "blob"}]}
    text = json.dumps({"type": ps.TYPE_MANY, "sets": [good, bad]})
    e = err(s.import_yaml, EDITOR, text)
    assert e.status == 400 and "broken" in e.message
    assert s.list(EDITOR)["prompt_sets"] == []


def test_a_key_exports_published_sets_only():
    s = svc()
    sid = s.create(EDITOR, DOOR)["id"]
    assert err(s.export, KEY, [sid]).status == 404
    assert "status: draft" in s.export(EDITOR, [sid])


# ------------------------------------------------------------------ applications publish

SLUG = "reviera.door-datasheet"


def test_an_application_publishes_with_no_role_and_no_setup():
    s = svc()
    status, out = s.app_publish(KEY, SLUG, DOOR)
    assert status == 201 and out["revision"] == 1 and out["changed"]
    (v,) = s.list(KEY2)["prompt_sets"]
    assert v["managed"] == "application" and v["managed_by"]["label"] == "Reviera laptop"
    # the same definition from another user's copy of the app: a no-op
    status, out = s.app_publish(KEY2, SLUG, DOOR)
    assert (status, out["revision"], out["changed"]) == (200, 1, False)
    assert len(s.revisions(EDITOR, out["id"])) == 1
    # a newer release publishes the next revision
    status, out = s.app_publish(KEY2, SLUG, {**DOOR, "instructions": "v2"})
    assert (status, out["revision"], out["changed"]) == (200, 2, True)


def test_publish_conflicts_with_a_builder_edit_unless_forced():
    s = svc()
    _st, out = s.app_publish(KEY, SLUG, DOOR)
    sid = out["id"]
    s.save_draft(EDITOR, sid, {**DOOR, "description": "edited in builder"})
    s.publish(EDITOR, sid)                                    # revision 2
    e = err(s.app_publish, KEY, SLUG, {**DOOR, "instructions": "app v2"}, base_revision=1)
    assert e.status == 409 and e.kind == "revision_conflict" and e.extra["current_revision"] == 2
    _st, out = s.app_publish(KEY, SLUG, {**DOOR, "instructions": "app v2"}, base_revision=1,
                             force=True)
    assert out["revision"] == 3


def test_editor_control_take_over_and_lock():
    s = svc()
    _st, out = s.app_publish(KEY, SLUG, DOOR)
    sid = out["id"]
    s.set_control(EDITOR, sid, take_over=True)
    e = err(s.app_publish, KEY, SLUG, {**DOOR, "instructions": "x"})
    assert e.status == 409 and e.kind == "managed_in_builder"
    assert s.app_publish(KEY, SLUG, DOOR)[1]["changed"] is False     # identical still fine
    assert s.app_publish(EDITOR_KEY, SLUG, {**DOOR, "instructions": "x"})[1]["revision"] == 2
    s.set_control(EDITOR, sid, take_over=False, locked=True)
    e = err(s.app_publish, KEY, SLUG, {**DOOR, "instructions": "y"})
    assert e.kind == "locked"
    assert s.app_publish(KEY, SLUG, {**DOOR, "instructions": "x"})[1]["changed"] is False


def test_application_draft_and_retire():
    s = svc()
    status, out = s.app_publish(KEY, SLUG, DOOR, publish=False)
    assert status == 201 and out["revision"] is None and out["draft_revision"] == 1
    assert s.list(KEY)["prompt_sets"] == []
    _st, out = s.app_publish(KEY, SLUG, DOOR)
    assert out["revision"] == 2          # the draft stays revision 1; the publish is 2
    s.app_retire(KEY, SLUG)
    assert err(s.resolve_for_run, KEY, out["id"], None).status == 410
    assert err(s.app_publish, KEY, SLUG, DOOR).status == 410


def test_application_caps():
    s = svc(prompt_set_app_max_sets=1, prompt_set_app_revisions_per_day=2)
    s.app_publish(KEY, SLUG, DOOR)
    e = err(s.app_publish, KEY, "reviera.other", {**DOOR, "slug": ""})
    assert e.status == 429 and e.kind == "app_set_limit"
    s.app_publish(KEY, SLUG, {**DOOR, "instructions": "2"})
    e = err(s.app_publish, KEY, SLUG, {**DOOR, "instructions": "3"})
    assert e.status == 429 and e.kind == "revision_limit"


def test_a_plain_browser_session_cannot_use_the_application_endpoint():
    assert err(svc().app_publish, MEMBER, SLUG, DOOR).status == 403


def test_the_path_slug_wins_and_an_unconfigured_model_publishes_with_a_warning():
    s = svc()
    status, out = s.app_publish(KEY, "reviera.window", {**DOOR, "slug": "other.slug",
                                                       "model": "claude-opus-9"})
    assert status == 201 and out["slug"] == "reviera.window"
    assert out["model"]["fallback"] is True and out["warnings"][0]["kind"] == "model_fallback"
