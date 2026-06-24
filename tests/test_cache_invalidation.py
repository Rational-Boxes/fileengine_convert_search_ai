"""Unit tests for real-time permission-cache invalidation from the event stream."""
from convert_search_ai.cache_invalidation import PermissionCacheInvalidator
from convert_search_ai.config import Config


class RecordingGate:
    def __init__(self):
        self.events = []

    def invalidate_resource(self, tenant, uid):
        self.events.append(("res", tenant, uid))

    def invalidate_member(self, tenant, member):
        self.events.append(("mem", tenant, member))

    def invalidate_tenant(self, tenant):
        self.events.append(("ten", tenant))


def _inv(gate):
    # source is set so _ensure_source() is never called in handle() tests.
    return PermissionCacheInvalidator(Config(), gate, source=object())


def test_acl_changed_invalidates_resource():
    g = RecordingGate()
    _inv(g).handle({"type": "acl.changed", "tenant": "t1", "file_uid": "f1", "principal": "dave"})
    assert g.events == [("res", "t1", "f1")]


def test_role_membership_invalidates_member():
    g = RecordingGate()
    inv = _inv(g)
    inv.handle({"type": "role.assigned", "tenant": "t1", "role": "editors", "member": "carol"})
    inv.handle({"type": "role.member_removed", "tenant": "t1", "role": "editors", "member": "bob"})
    assert g.events == [("mem", "t1", "carol"), ("mem", "t1", "bob")]


def test_role_deleted_invalidates_tenant():
    g = RecordingGate()
    _inv(g).handle({"type": "role.deleted", "tenant": "t1", "role": "editors"})
    assert g.events == [("ten", "t1")]


def test_file_deleted_invalidates_resource():
    g = RecordingGate()
    _inv(g).handle({"type": "file.deleted", "tenant": "t1", "file_uid": "f9"})
    assert g.events == [("res", "t1", "f9")]


def test_non_governance_events_ignored():
    g = RecordingGate()
    inv = _inv(g)
    for t in ("file.created", "file.updated", "file.moved", "dir.created"):
        inv.handle({"type": t, "tenant": "t1", "file_uid": "f"})
    assert g.events == []


def test_run_once_handles_and_acks():
    g = RecordingGate()

    class Src:
        def __init__(self):
            self.acked = []

        def read(self, count=64, block_ms=5000):
            return [("1-0", {"type": "acl.changed", "tenant": "t", "file_uid": "f"})]

        def ack(self, ids):
            self.acked.extend(ids)

    src = Src()
    inv = PermissionCacheInvalidator(Config(), g, source=src)
    assert inv.run_once() == 1
    assert src.acked == ["1-0"]
    assert g.events == [("res", "t", "f")]
