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

"""The indexing agent must read all content (complete index); per-user ACLs are
enforced later at retrieval time. So agent_client carries the core's trusted
``system_admin`` bypass role, while client_for (end-user/retrieval) never does."""
import convert_search_ai.core_client as core_client
from convert_search_ai.core_client import SYSTEM_ADMIN_ROLE, agent_client, client_for
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity


class FakeMF:
    def __init__(self, *, server_address, user_name, user_roles, tenant, source_addr="", **_kw):
        self.user_name = user_name
        self.user_roles = list(user_roles)
        self.tenant = tenant
        self.source_addr = source_addr


def _patch(monkeypatch, agent=Identity(user="csai", roles=["users"], tenant="t", authenticated=True)):
    monkeypatch.setattr(core_client, "ManagedFiles", FakeMF)
    monkeypatch.setattr(core_client, "agent_identity", lambda config: agent)


def test_agent_client_attaches_system_admin_for_full_index(monkeypatch):
    _patch(monkeypatch)
    mf = agent_client(Config())
    assert SYSTEM_ADMIN_ROLE in mf.user_roles
    assert "users" in mf.user_roles  # original roles preserved


def test_agent_client_respects_disabled_bypass(monkeypatch):
    _patch(monkeypatch)
    cfg = Config()
    cfg.index_bypass_acl = False
    mf = agent_client(cfg)
    assert SYSTEM_ADMIN_ROLE not in mf.user_roles


def test_agent_client_does_not_duplicate_role(monkeypatch):
    _patch(monkeypatch, agent=Identity(user="csai", roles=["system_admin"], tenant="t", authenticated=True))
    mf = agent_client(Config())
    assert mf.user_roles.count(SYSTEM_ADMIN_ROLE) == 1


def test_client_for_end_user_never_gets_bypass(monkeypatch):
    monkeypatch.setattr(core_client, "ManagedFiles", FakeMF)
    mf = client_for(Identity(user="alice", roles=["users"], tenant="t", authenticated=True), Config())
    assert SYSTEM_ADMIN_ROLE not in mf.user_roles
    assert mf.user_name == "alice"


def test_client_for_aliases_administrators_to_tenant_admin(monkeypatch):
    # Mirror the bridges (security review H2): a tenant "administrators" member acts
    # with the core's tenant_admin role, so CSAI resolves the same effective perms
    # the REST/WebDAV doors do (else admins are denied WRITE / ONLYOFFICE editing).
    monkeypatch.setattr(core_client, "ManagedFiles", FakeMF)
    mf = client_for(Identity(user="james", roles=["administrators", "users"], tenant="t",
                             authenticated=True), Config())
    assert "tenant_admin" in mf.user_roles
    assert "administrators" in mf.user_roles  # originals preserved
    # NOT the global system_admin bypass (only real system_admin members carry it).
    assert SYSTEM_ADMIN_ROLE not in mf.user_roles


def test_client_for_non_admin_gets_no_extra_roles(monkeypatch):
    monkeypatch.setattr(core_client, "ManagedFiles", FakeMF)
    mf = client_for(Identity(user="bob", roles=["users"], tenant="t", authenticated=True), Config())
    assert "tenant_admin" not in mf.user_roles and mf.user_roles == ["users"]


def test_client_for_does_not_duplicate_tenant_admin(monkeypatch):
    monkeypatch.setattr(core_client, "ManagedFiles", FakeMF)
    mf = client_for(Identity(user="j", roles=["administrators", "tenant_admin"], tenant="t",
                             authenticated=True), Config())
    assert mf.user_roles.count("tenant_admin") == 1
