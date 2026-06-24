"""Unit tests for SearchService — permission filtering + text retrieval (fakes)."""
import pytest

from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.permissions import PermissionGate
from convert_search_ai.search import SearchService


class FakeRepo:
    def __init__(self, rows, text=None):
        self.rows = rows
        self._text = text

    def query(self, tenant, q, *, fetch, fuzzy):
        return list(self.rows)

    def get_text(self, tenant, uid):
        return self._text


class FakeMF:
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def check_permission(self, uid, perm, tenant=None):
        return uid in self.allowed

    def close(self):
        pass


def _svc(rows=None, text=None, allowed=()):
    return SearchService(
        Config(),
        repo=FakeRepo(rows or [], text=text),
        gate=PermissionGate(300),
        client_factory=lambda ident: FakeMF(allowed),
    )


def _id():
    return Identity(user="alice", roles=[], tenant="default", authenticated=True)


def _row(uid, score=1.0):
    return {"file_uid": uid, "name": uid.upper(), "snippet": "...", "score": score}


def test_search_drops_unreadable_hits():
    svc = _svc([_row("a", 3), _row("b", 2), _row("c", 1)], allowed=["a", "c"])
    assert [h.file_uid for h in svc.search(_id(), "hello", limit=10)] == ["a", "c"]


def test_search_respects_limit_after_filtering():
    rows = [_row(str(i)) for i in range(10)]
    svc = _svc(rows, allowed=[str(i) for i in range(10)])
    assert len(svc.search(_id(), "q", limit=3)) == 3


def test_empty_query_returns_nothing():
    assert _svc([_row("a")], allowed=["a"]).search(_id(), "   ") == []


def test_get_text_ok():
    assert _svc(text="# Doc", allowed=["x"]).get_text(_id(), "x") == "# Doc"


def test_get_text_permission_denied():
    with pytest.raises(PermissionError):
        _svc(text="# Doc", allowed=[]).get_text(_id(), "x")


def test_get_text_not_found():
    with pytest.raises(FileNotFoundError):
        _svc(text=None, allowed=["x"]).get_text(_id(), "x")
