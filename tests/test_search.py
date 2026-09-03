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

    def entity_exists(self, uid, include_deleted=False):

        # The permission gate pairs its ACL check with an existence

        # check: the core grants READ by default when no rule matches,

        # including for a uid that is gone. These stubs model files

        # that exist, so this is True.

        return True


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


def test_empty_query_rejected():
    from convert_search_ai.guards import GuardError
    with pytest.raises(GuardError):
        _svc([_row("a")], allowed=["a"]).search(_id(), "   ")


def test_get_text_ok():
    text, truncated = _svc(text="# Doc", allowed=["x"]).get_text(_id(), "x")
    assert text == "# Doc" and truncated is False


def test_get_text_permission_denied():
    with pytest.raises(PermissionError):
        _svc(text="# Doc", allowed=[]).get_text(_id(), "x")


def test_get_text_not_found():
    with pytest.raises(FileNotFoundError):
        _svc(text=None, allowed=["x"]).get_text(_id(), "x")


# --- the query shape, not just its results ----------------------------------
#
# Search went down completely on the DO deployment as the corpus grew: every
# query, including one matching nothing, exceeded the 5 s statement timeout. Two
# causes, both in the SQL, both invisible to a test that only checks hits.

def test_the_snippet_is_computed_after_the_limit_not_before():
    """ts_headline re-parses each document it is given. In the ranking query's
    target list it ran for every candidate; measured at 976 documents that was
    22 ms of matching and 4.0 s of snippetting. It belongs outside the CTE that
    does ORDER BY ... LIMIT."""
    from convert_search_ai.search import _SEARCH_SQL
    sql = " ".join(_SEARCH_SQL.split())
    ranked = sql[sql.index("ranked AS ("):sql.index("SELECT file_uid,")]
    assert "ts_headline" not in ranked, "snippet is being computed before the LIMIT"
    assert "LIMIT %(fetch)s" in ranked
    assert sql.count("ts_headline") == 1
    assert sql.index("LIMIT %(fetch)s") < sql.index("ts_headline")


def test_no_full_document_trigram_scan():
    """`query <% content_md` and `word_similarity(query, content_md)` are both
    O(size of every document) and neither used the trigram index — a sequential
    scan over 18 MB on every query, paid even when nothing matched. Fuzzy now
    means the filename; the content is covered by full-text search, which stems."""
    from convert_search_ai.search import _SEARCH_SQL
    sql = " ".join(_SEARCH_SQL.split())
    assert "word_similarity" not in sql
    assert "<%" not in sql
    # Filename fuzz stays: names are short, and the index on them is used.
    assert "d.name %% %(q)s" in sql
    assert "similarity(d.name, %(q)s)" in sql
