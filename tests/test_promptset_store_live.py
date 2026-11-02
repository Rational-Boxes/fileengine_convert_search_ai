"""Prompt-set storage: the Postgres store and the in-memory store keep one
contract (EXTRACTION_PROMPT_SETS.md §4.1). The service tests run on the memory
store, so this is what makes them say something about production.

Postgres cases run in a throwaway tenant schema, dropped afterwards."""
import datetime as dt
import uuid

import pytest
from conftest import _db_up

from convert_search_ai.config import Config
from convert_search_ai.headless import promptsets as ps
from convert_search_ai.headless.models import ModelRegistry
from convert_search_ai.headless.promptset_service import PromptSetService
from convert_search_ai.headless.promptset_store import (MemoryPromptSetStore, PromptSetStore,
                                                        SlugTaken)
from convert_search_ai.ldap_auth import Identity

from test_promptsets import DOOR, EDITOR, KEY, KEY2, SLUG


def _drop(tenant):
    from convert_search_ai.db import connect
    from convert_search_ai.schema import schema_name
    with connect(Config()) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name(tenant)}" CASCADE')
        conn.commit()


@pytest.fixture(params=["memory", "postgres"])
def store_and_tenant(request):
    tenant = f"ps{uuid.uuid4().hex[:10]}"
    if request.param == "memory":
        yield MemoryPromptSetStore(), tenant
        return
    if not _db_up():
        pytest.skip("Postgres (CSAI_PG_*) not reachable")
    yield PromptSetStore(Config()), tenant
    _drop(tenant)


BODY = ps.normalise(DOOR)
H = ps.definition_hash(BODY)


def test_create_revise_publish(store_and_tenant):
    st, t = store_and_tenant
    st.create_set(t, set_id="ps_a", slug="a.b", name="A", description="d", managed="builder",
                  created_by="ed")
    assert st.add_revision(t, "ps_a", body=BODY, hash=H, state="draft", user="ed") == 1
    assert st.get_set(t, "ps_a")["current_revision"] is None
    assert st.update_draft(t, "ps_a", 1, body={**BODY, "description": "x"}, hash="h2", user="ed")
    assert st.publish_revision(t, "ps_a", 1)
    s = st.get_set(t, "ps_a")
    assert s["current_revision"] == 1 and s["name"] == "Door datasheet" and s["description"] == "x"
    assert not st.publish_revision(t, "ps_a", 1)              # already published
    assert not st.update_draft(t, "ps_a", 1, body=BODY, hash=H, user="ed")   # immutable now
    n = st.add_revision(t, "ps_a", body=BODY, hash=H, state="published", user="k",
                        managed_by={"client_key_id": "fesk_1", "label": "L", "user": "k"})
    assert n == 2 and st.get_set(t, "ps_a")["current_revision"] == 2
    revs = st.list_revisions(t, "ps_a")
    assert [(r["revision"], r["state"]) for r in revs] == [(1, "published"), (2, "published")]
    assert revs[1]["body"] == BODY and revs[1]["managed_by"]["label"] == "L"
    assert revs[0]["published_at"] is not None


def test_slugs_are_unique_and_flags_update(store_and_tenant):
    st, t = store_and_tenant
    st.create_set(t, set_id="ps_a", slug="same.slug", name="A", description="", managed="application",
                  created_by="k")
    with pytest.raises(SlugTaken):
        st.create_set(t, set_id="ps_b", slug="same.slug", name="B", description="",
                      managed="application", created_by="k")
    st.create_set(t, set_id="ps_c", slug=None, name="C", description="", managed="builder",
                  created_by="ed")
    st.create_set(t, set_id="ps_d", slug=None, name="D", description="", managed="builder",
                  created_by="ed")                           # many sets without a slug
    assert st.by_slug(t, "same.slug")["id"] == "ps_a"
    assert st.count_sets(t, "application") == 1 and st.count_sets(t, "builder") == 2
    assert st.update_set(t, "ps_a", locked=True, managed="builder", status="retired",
                         replaced_by="ps_c")
    s = st.get_set(t, "ps_a")
    assert (s["locked"], s["managed"], s["status"], s["replaced_by"]) == \
        (True, "builder", "retired", "ps_c")
    assert [s["id"] for s in st.list_sets(t)] == ["ps_a", "ps_c", "ps_d"]


def test_revisions_since(store_and_tenant):
    st, t = store_and_tenant
    st.create_set(t, set_id="ps_a", slug=None, name="A", description="", managed="builder",
                  created_by="ed")
    for _ in range(3):
        st.add_revision(t, "ps_a", body=BODY, hash=H, state="draft", user="ed")
    now = dt.datetime.now(dt.timezone.utc)
    assert st.revisions_since(t, "ps_a", now - dt.timedelta(days=1)) == 3
    assert st.revisions_since(t, "ps_a", now + dt.timedelta(minutes=5)) == 0


def test_the_application_publish_flow_on_each_store(store_and_tenant):
    st, t = store_and_tenant
    c = Config()
    svc = PromptSetService(st, ModelRegistry.from_config(c), c)
    k1 = Identity(**{**KEY.__dict__, "tenant": t})
    k2 = Identity(**{**KEY2.__dict__, "tenant": t})
    ed = Identity(**{**EDITOR.__dict__, "tenant": t})
    status, out = svc.app_publish(k1, SLUG, DOOR)
    assert status == 201
    status2, same = svc.app_publish(k2, SLUG, DOOR)
    assert (status2, same["revision"], same["changed"]) == (200, 1, False)
    _st, out2 = svc.app_publish(k2, SLUG, {**DOOR, "instructions": "v2"})
    assert out2["revision"] == 2
    (v,) = svc.list(k1)["prompt_sets"]
    assert v["revision"] == 2 and v["managed_by"]["label"] == "Reviera desktop"
    svc.set_control(ed, out["id"], locked=True)
    assert svc.app_publish(k1, SLUG, {**DOOR, "instructions": "v2"})[1]["changed"] is False
