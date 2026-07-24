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

    def ann_search(self, tenant, qv, k):
        return list(self.rows)[:k]


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
