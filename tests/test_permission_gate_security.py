"""Security tests for the CSAI PermissionGate — the fail-closed cache that every
search result and RAG citation is filtered through. These pin the invariants the
whole "results are gated by the end-user's READ" guarantee depends on:

  * a core error / unreachable core => DENY (fail-closed), never allow
  * an allow decision is cached (no re-hit within TTL) but a DENY is never
    silently upgraded to allow
  * event-driven invalidation (resource / member / tenant) forces a re-check
    tighter than the TTL, so a revoked grant stops being served promptly
  * TTL <= 0 disables caching (every call re-checks)

Pure-unit: drives PermissionGate with a fake user-bound client; no core/LDAP/DB.
Run: ``PYTHONPATH=src pytest tests/test_permission_gate_security.py``
"""
from types import SimpleNamespace

import pytest

from convert_search_ai.permissions import PermissionGate


class FakeClient:
    """Stand-in for the user-bound gRPC ManagedFiles client. `allowed` is the set
    of file_uids this identity may read; `raises` simulates an unreachable core."""

    def __init__(self, allowed=None, raises=False):
        self.allowed = set(allowed or [])
        self.raises = raises
        self.calls = 0

    def check_permission(self, file_uid, perm, tenant=None):
        self.calls += 1
        assert perm == "r"  # the gate only ever asks for READ
        if self.raises:
            raise RuntimeError("core unreachable")
        return file_uid in self.allowed


IDENT = SimpleNamespace(tenant="acme", user="alice")


def test_fail_closed_when_core_errors():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(raises=True)
    assert gate.can_read(mf, IDENT, "F") is False


def test_allow_is_cached_within_ttl():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(allowed={"F"})
    assert gate.can_read(mf, IDENT, "F") is True
    assert gate.can_read(mf, IDENT, "F") is True
    assert mf.calls == 1, "second read within TTL must be served from cache"


def test_deny_is_not_upgraded_to_allow():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(allowed=set())  # nothing readable
    assert gate.can_read(mf, IDENT, "secret") is False
    assert gate.can_read(mf, IDENT, "secret") is False


def test_invalidate_resource_forces_recheck():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(allowed={"F"})
    assert gate.can_read(mf, IDENT, "F") is True
    # Access to F is revoked at the core; an acl.changed event invalidates it.
    mf.allowed.discard("F")
    gate.invalidate_resource(IDENT.tenant, "F")
    assert gate.can_read(mf, IDENT, "F") is False
    assert mf.calls == 2, "invalidation must force a fresh core check"


def test_invalidate_member_forces_recheck():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(allowed={"F"})
    assert gate.can_read(mf, IDENT, "F") is True
    mf.allowed.discard("F")
    gate.invalidate_member(IDENT.tenant, IDENT.user)
    assert gate.can_read(mf, IDENT, "F") is False


def test_invalidate_tenant_forces_recheck():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(allowed={"F"})
    assert gate.can_read(mf, IDENT, "F") is True
    mf.allowed.discard("F")
    gate.invalidate_tenant(IDENT.tenant)
    assert gate.can_read(mf, IDENT, "F") is False


def test_zero_ttl_disables_caching():
    gate = PermissionGate(ttl_seconds=0)
    mf = FakeClient(allowed={"F"})
    assert gate.can_read(mf, IDENT, "F") is True
    assert gate.can_read(mf, IDENT, "F") is True
    assert mf.calls == 2, "TTL<=0 must re-check every time (no stale allow)"


def test_filter_readable_returns_only_permitted():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(allowed={"A", "C"})
    got = gate.filter_readable(mf, IDENT, ["A", "B", "C", "D"])
    assert got == ["A", "C"], "only files the identity can READ survive the filter"


def test_filter_readable_fails_closed_on_error():
    gate = PermissionGate(ttl_seconds=300)
    mf = FakeClient(raises=True)
    assert gate.filter_readable(mf, IDENT, ["A", "B"]) == []
