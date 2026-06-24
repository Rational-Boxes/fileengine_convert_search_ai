"""Unit tests for the permission gate: caching, fail-closed, invalidation."""
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.permissions import PermissionGate


class FakeMF:
    def __init__(self, allowed):
        self.allowed = set(allowed)
        self.calls = 0

    def check_permission(self, uid, perm, tenant=None):
        self.calls += 1
        return uid in self.allowed


def _id(user="alice", tenant="default"):
    return Identity(user=user, roles=[], tenant=tenant, authenticated=True)


def test_allows_denies_and_caches():
    g = PermissionGate(300)
    mf = FakeMF(["a"])
    ident = _id()
    assert g.can_read(mf, ident, "a") is True
    assert g.can_read(mf, ident, "b") is False
    calls = mf.calls
    assert g.can_read(mf, ident, "a") is True  # cached -> no new check
    assert mf.calls == calls


def test_fail_closed_on_error():
    class Boom:
        def check_permission(self, *a, **k):
            raise RuntimeError("core down")

    assert PermissionGate(300).can_read(Boom(), _id(), "a") is False


def test_filter_readable_preserves_order():
    g = PermissionGate(300)
    assert g.filter_readable(FakeMF(["a", "c"]), _id(), ["a", "b", "c"]) == ["a", "c"]


def test_invalidate_resource_only_evicts_that_resource():
    g = PermissionGate(300)
    mf = FakeMF(["a", "b"])
    ident = _id()
    g.can_read(mf, ident, "a")
    g.can_read(mf, ident, "b")
    g.invalidate_resource("default", "a")
    calls = mf.calls
    g.can_read(mf, ident, "a")  # miss -> +1
    g.can_read(mf, ident, "b")  # still cached -> +0
    assert mf.calls == calls + 1


def test_invalidate_member_and_tenant():
    g = PermissionGate(300)
    mf = FakeMF(["a", "b"])
    ident = _id()
    g.can_read(mf, ident, "a")
    g.can_read(mf, ident, "b")
    g.invalidate_member("default", "alice")
    calls = mf.calls
    g.can_read(mf, ident, "a")  # +1
    g.can_read(mf, ident, "b")  # +1
    assert mf.calls == calls + 2

    g.invalidate_tenant("default")
    calls = mf.calls
    g.can_read(mf, ident, "a")  # +1
    assert mf.calls == calls + 1
