"""Headless run records against a real Postgres (HEADLESS_RESEARCH_API.md §3.5, §3.6).

In a throwaway tenant schema, dropped afterwards. Pinned:

- a run is a conversation with origin 'headless' under its key and label, kept
  OUT of the research sidebar listing, and grouped per application;
- a chat turn naming a headless run does not append to it;
- erasure of a source file destroys that file's values and quotes in every run
  and leaves a tombstone row (the record that the run read it);
- erasure in a tenant provisioned before headless runs existed still works."""
import os
import uuid

import pytest
from conftest import live_db

from convert_search_ai.config import Config
from convert_search_ai.conversations import ConversationStore
from convert_search_ai.headless.runs import RunStore
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.store import DocumentStore

pytestmark = live_db


@pytest.fixture
def tenant():
    t = f"hl{uuid.uuid4().hex[:10]}"
    yield t
    from convert_search_ai.db import connect
    from convert_search_ai.schema import schema_name
    with connect(Config()) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name(t)}" CASCADE')
        conn.commit()


def ident(tenant, key="fesk_a", label="Reviera laptop", user="alice"):
    return Identity(user=user, tenant=tenant, authenticated=True,
                    client_key_id=key, client_label=label)


ITEM = {"file_uid": "doc1", "version": "v1", "name": "door.pdf",
        "data": {"fire_rating": {"found": True, "value": "EI 60",
                                 "evidence": {"quote": "Fire resistance | EI 60", "offset": 40}}}}


def test_runs_are_recorded_listed_per_application_and_kept_out_of_the_sidebar(tenant):
    cfg = Config()
    runs, convos = RunStore(cfg), ConversationStore(cfg)
    chat_id = convos.create(tenant, "alice", title="my own research")
    a = ident(tenant)
    r1 = runs.create(a, title="Extract fire_rating from folder", request={"kind": "extract"})
    runs.add_result(a, r1, ITEM)
    runs.finish(a, r1, {"extracted": 1})
    r2 = runs.create(ident(tenant, key="fesk_b", label="Reviera desktop"), title="second",
                     request={"kind": "research"})

    assert [c["id"] for c in convos.list(tenant, "alice")] == [chat_id]
    apps = {a["client_key_id"]: a for a in runs.applications(tenant, "alice")}
    assert set(apps) == {"fesk_a", "fesk_b"}
    assert apps["fesk_a"]["label"] == "Reviera laptop" and apps["fesk_a"]["run_count"] == 1
    assert runs.applications(tenant, "bob") == []

    got = runs.get(tenant, "alice", r1)
    assert got["client_label"] == "Reviera laptop"
    assert got["results"][0]["data"]["fire_rating"]["value"] == "EI 60"
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert runs.get(tenant, "bob", r1) is None
    assert runs.get(tenant, "alice", chat_id) is None          # a chat is not a run
    assert runs.is_headless(tenant, "alice", r2) and not runs.is_headless(tenant, "alice", chat_id)


def test_a_chat_turn_cannot_append_to_a_headless_run(tenant):
    cfg = Config()
    runs, convos = RunStore(cfg), ConversationStore(cfg)
    r = runs.create(ident(tenant), title="run", request={})
    assert convos.owns(tenant, "alice", r) is False


def test_erasure_tombstones_headless_results(tenant):
    cfg = Config()
    runs, store = RunStore(cfg), DocumentStore(cfg)
    a = ident(tenant)
    r = runs.create(a, title="run", request={})
    runs.add_result(a, r, ITEM)
    runs.add_result(a, r, {**ITEM, "file_uid": "doc2", "name": "window.pdf"})
    out = store.erase(tenant, "doc1", erasure_id="er-1")
    assert out["headless_results"] == 1
    results = runs.get(tenant, "alice", r)["results"]
    assert results[0] == {"file_uid": "doc1", "version": "v1", "name": "door.pdf", "erased": True}
    assert results[1]["data"]["fire_rating"]["value"] == "EI 60"          # other file intact
    from convert_search_ai.db import connect_for_tenant
    with connect_for_tenant(cfg, tenant) as conn, conn.cursor() as cur:
        cur.execute("SELECT result, erased, erasure_id FROM headless_results "
                    "WHERE file_uid = 'doc1'")
        assert cur.fetchone() == (None, True, "er-1")
    assert store.erase(tenant, "doc1", erasure_id="er-1")["headless_results"] == 0


def test_erasure_in_a_tenant_without_the_headless_table_still_works(tenant):
    cfg = Config()
    from convert_search_ai.db import connect_for_tenant
    with connect_for_tenant(cfg, tenant, provision=True) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE headless_results")
        conn.commit()
    assert DocumentStore(cfg).erase(tenant, "doc1", erasure_id="er-2")["headless_results"] == 0
