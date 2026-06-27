"""The indexing agent must read all content (complete index); per-user ACLs are
enforced later at retrieval time. So agent_client carries the core's trusted
``system_admin`` bypass role, while client_for (end-user/retrieval) never does."""
import convert_search_ai.core_client as core_client
from convert_search_ai.core_client import SYSTEM_ADMIN_ROLE, agent_client, client_for
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity


class FakeMF:
    def __init__(self, *, server_address, user_name, user_roles, tenant):
        self.user_name = user_name
        self.user_roles = list(user_roles)
        self.tenant = tenant


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
