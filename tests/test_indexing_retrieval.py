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

"""Unit tests for the indexer and permission-scoped retriever (fakes)."""
from convert_search_ai.config import Config
from convert_search_ai.indexing import Indexer
from convert_search_ai.ldap_auth import Identity
from convert_search_ai.permissions import PermissionGate
from convert_search_ai.retrieval import Retriever
from convert_search_ai.vectorstore import RetrievedChunk


class FakeEmbedder:
    def embed(self, texts):
        return [[float(len(t))] * 4 for t in texts]

    def embed_query(self, text):
        return [0.0] * 4


class FakeChunkStore:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.replaced = {}
        self.deleted = []

    def replace(self, tenant, uid, items):
        self.replaced[(tenant, uid)] = items

    def delete(self, tenant, uid):
        self.deleted.append((tenant, uid))

    def ann_search(self, tenant, qv, k, *, file_uids=None):
        rows = self.rows if file_uids is None else [r for r in self.rows if r.file_uid in set(file_uids)]
        return list(rows)[:k]


class FakeMF:
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def check_permission(self, uid, perm, tenant=None):
        return uid in self.allowed

    def close(self):
        pass


def _id():
    return Identity(user="u", tenant="default", authenticated=True)


# --- indexing ---
def test_indexer_chunks_embeds_and_stores():
    cs = FakeChunkStore()
    n = Indexer(Config(), embedder=FakeEmbedder(), chunk_store=cs).index(
        "default", "f1", "# A\n\npara one\n\npara two")
    assert n >= 1
    items = cs.replaced[("default", "f1")]
    assert items[0][0] == 0 and len(items[0]) == 3  # (ordinal, text, vector)


def test_indexer_empty_content_deletes():
    cs = FakeChunkStore()
    assert Indexer(Config(), embedder=FakeEmbedder(), chunk_store=cs).index("default", "f1", "  ") == 0
    assert ("default", "f1") in cs.deleted


# --- retrieval ---
def _chunk(uid, o=0):
    return RetrievedChunk(uid, o, f"text {uid}", 0.1)


def _retriever(rows, allowed):
    return Retriever(Config(), embedder=FakeEmbedder(), chunk_store=FakeChunkStore(rows),
                     gate=PermissionGate(300), client_factory=lambda i: FakeMF(allowed))


def test_retrieve_is_permission_scoped():
    out = _retriever([_chunk("a"), _chunk("b"), _chunk("c")], allowed=["a", "c"]).retrieve(_id(), "q", k=10)
    assert [c.file_uid for c in out] == ["a", "c"]


def test_retrieve_respects_k():
    rows = [_chunk(str(i)) for i in range(10)]
    out = _retriever(rows, allowed=[str(i) for i in range(10)]).retrieve(_id(), "q", k=3)
    assert len(out) == 3


def test_retrieve_empty_query():
    assert _retriever([_chunk("a")], allowed=["a"]).retrieve(_id(), "   ") == []


# --- folder-scoped retrieval (Feature 2) ---
class _ScopeEntry:
    def __init__(self, uid, is_container=False):
        self.uid, self.is_container, self.name = uid, is_container, uid


class _ScopeMF:
    """Fake client with a folder tree (for scope resolution) + a permission set."""
    def __init__(self, tree, allowed):
        self.tree = tree          # {folder_uid: [(child_uid, is_container), ...]}
        self.allowed = set(allowed)

    def dir(self, uid, tenant=None):
        return [_ScopeEntry(u, c) for (u, c) in self.tree.get(uid, [])]

    def check_permission(self, uid, perm, tenant=None):
        return uid in self.allowed

    def close(self):
        pass


def _scope_retriever(rows, tree, allowed):
    return Retriever(Config(), embedder=FakeEmbedder(), chunk_store=FakeChunkStore(rows),
                     gate=PermissionGate(300), client_factory=lambda i: _ScopeMF(tree, allowed))


# /F1 has doc "a" + subfolder "S"; /S has doc "b"; /F2 has doc "c".
_TREE = {"F1": [("a", False), ("S", True)], "S": [("b", False)], "F2": [("c", False)]}


def test_scope_includes_selected_folder_and_its_subfolders():
    out = _scope_retriever([_chunk("a"), _chunk("b"), _chunk("c")], _TREE, allowed=["a", "b", "c"]) \
        .retrieve(_id(), "q", k=10, scope_folder_uids=["F1"])
    assert sorted(c.file_uid for c in out) == ["a", "b"]  # direct + subfolder, NOT c


def test_no_scope_searches_all_documents():
    out = _scope_retriever([_chunk("a"), _chunk("b"), _chunk("c")], _TREE, allowed=["a", "b", "c"]) \
        .retrieve(_id(), "q", k=10)
    assert sorted(c.file_uid for c in out) == ["a", "b", "c"]


def test_scope_with_no_documents_yields_no_context():
    out = _scope_retriever([_chunk("a")], _TREE, allowed=["a"]) \
        .retrieve(_id(), "q", k=10, scope_folder_uids=["EMPTY"])
    assert out == []


def test_scope_still_permission_filters():
    # "b" is in scope (F1→S) but not readable → excluded even though scoped in.
    out = _scope_retriever([_chunk("a"), _chunk("b")], _TREE, allowed=["a"]) \
        .retrieve(_id(), "q", k=10, scope_folder_uids=["F1"])
    assert [c.file_uid for c in out] == ["a"]
