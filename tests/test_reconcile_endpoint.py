# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""POST /ingest/reconcile — authorization, and the status-vocabulary contract
between the pipeline and the schema."""
import re

import convert_search_ai.api as api
import convert_search_ai.reconcile as reconcile_mod
from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.schema import tenant_ddl
from fastapi.testclient import TestClient


def _client(monkeypatch):
    # The route refuses early if the core is unreachable; that check is not what
    # these tests are about.
    monkeypatch.setattr(api, "_check_core", lambda config: True)
    monkeypatch.setattr(reconcile_mod, "sweep", lambda *a, **k: {"examined": 0, "retried": 0})
    monkeypatch.setattr(reconcile_mod, "reconcile", lambda *a, **k: {"files": 0})
    app = build_app(Config())
    return app, TestClient(app)


def _token(app, user="alice", roles=("administrators",)):
    return app.state.token_store.issue(
        Identity(user=user, roles=list(roles), tenant="default", authenticated=True))


def test_reconcile_requires_authentication(monkeypatch):
    app, c = _client(monkeypatch)
    r = c.post("/ingest/reconcile")
    assert r.status_code == 401


def test_reconcile_requires_a_tenant_administrator(monkeypatch):
    app, c = _client(monkeypatch)
    tok = _token(app, roles=["users"])
    r = c.post("/ingest/reconcile", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_admin_may_sweep_their_own_tenant(monkeypatch):
    app, c = _client(monkeypatch)
    tok = _token(app)
    r = c.post("/ingest/reconcile", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["mode"] == "sweep" and r.json()["tenant"] == "default"


def test_admin_cannot_reconcile_another_tenant(monkeypatch):
    """`tenant` used to be a free query parameter on an unauthenticated route."""
    app, c = _client(monkeypatch)
    tok = _token(app)
    r = c.post("/ingest/reconcile?tenant=someco", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_mode_full_is_accepted_and_anything_else_is_not(monkeypatch):
    app, c = _client(monkeypatch)
    tok = _token(app)
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/ingest/reconcile?mode=full", headers=h).status_code == 200
    assert c.post("/ingest/reconcile?mode=wander", headers=h).status_code == 422


# --- the contract the production bug broke ----------------------------------
#: Every status ConversionPipeline can write to the documents table.
PIPELINE_STATUSES = {"pending", "converting", "converted", "indexed",
                     "index_failed", "unsupported", "error"}


def _ddl_statuses(ddl: str):
    """Every status vocabulary declared in the DDL (CREATE TABLE + the ALTER)."""
    return [set(re.findall(r"'([a-z_]+)'", m))
            for m in re.findall(r"status IN \(([^)]*)\)", ddl)]


def test_schema_permits_every_status_the_pipeline_writes():
    """The bug this file exists for. The pipeline gained 'index_failed' and the
    CHECK constraint did not, so the write meant to record an embedding failure
    raised CheckViolation and the row kept its previous 'converting' value —
    invisibly, forever.

    It survived because the pipeline tests use a fake store that accepts any
    string. This asserts the vocabularies agree without needing a database."""
    vocabularies = _ddl_statuses(tenant_ddl("default"))
    assert vocabularies, "no status CHECK found in the tenant DDL"
    for vocab in vocabularies:
        missing = PIPELINE_STATUSES - vocab
        assert not missing, f"schema CHECK rejects pipeline status(es): {sorted(missing)}"


def test_the_alter_self_heals_a_tenant_created_before_index_failed():
    """CREATE TABLE IF NOT EXISTS cannot widen an existing tenant's constraint,
    so the DDL must also drop and re-add it."""
    ddl = tenant_ddl("default")
    assert "DROP CONSTRAINT IF EXISTS documents_status_check" in ddl
    assert re.search(r"ADD CONSTRAINT documents_status_check", ddl)
