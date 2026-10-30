"""The extraction cache sidecar (HEADLESS_RESEARCH_API.md §3.7).

`<version>-extract.json`, a hidden child of the source document written by the
rendition convention: it parses as a known rendition (so a superseded version's
cache is pruned), holds one entry per question, adds entries as new versions of
the same child, and a broken store never fails the caller."""
import io
import json

from convert_search_ai.headless.cache import SCHEMA, ExtractionCache, sidecar_name
from convert_search_ai.renditions import parse_rendition_name


class MF:
    def __init__(self):
        self.children = {}     # parent -> {name: uid}
        self.content = {}      # uid -> bytes (latest version)
        self.versions = {}     # uid -> count
        self.closed = 0

    def dir(self, uid, tenant=None, **_):
        class E:
            def __init__(self, uid, name):
                self.uid, self.name = uid, name
        return [E(u, n) for n, u in self.children.get(uid, {}).items()]

    def touch(self, parent, name, tenant=None, **_):
        uid = f"c{len(self.content) + 1}"
        self.children.setdefault(parent, {})[name] = uid
        self.content[uid] = b""
        return uid

    def put_stream(self, uid, chunks, tenant=None, **_):
        self.content[uid] = b"".join(chunks)
        self.versions[uid] = self.versions.get(uid, 0) + 1

    def get(self, uid, tenant=None, **_):
        return io.BytesIO(self.content[uid])

    def close(self):
        self.closed += 1


def test_sidecar_name_is_a_known_rendition_even_for_a_suffixed_version():
    assert sidecar_name("20261029_120000.123") == "20261029_120000.123-extract.json"
    assert parse_rendition_name(sidecar_name("20261029_120000.123-1")) == \
        ("20261029_120000.123-1", "extract", "json")


def test_put_then_get_and_entries_accumulate_as_versions():
    mf = MF()
    c = ExtractionCache(lambda: mf)
    assert c.get("doc", "v1", "t", "k1") is None
    assert c.put("doc", "v1", "t", "k1", {"data": {"a": 1}})
    assert c.put("doc", "v1", "t", "k2", {"data": {"b": 2}})
    assert c.get("doc", "v1", "t", "k1") == {"data": {"a": 1}}
    assert c.get("doc", "v1", "t", "k2") == {"data": {"b": 2}}
    assert list(mf.children["doc"]) == ["v1-extract.json"]          # one sidecar
    uid = mf.children["doc"]["v1-extract.json"]
    assert mf.versions[uid] == 2                                     # history kept
    doc = json.loads(mf.content[uid])
    assert doc["schema"] == SCHEMA and doc["version"] == "v1"
    assert c.get("doc", "v2", "t", "k1") is None                    # a new version: no hit
    assert mf.closed >= 6                                            # every use closes


def test_a_broken_store_misses_rather_than_fails():
    class Broken(MF):
        def dir(self, *a, **k):
            raise RuntimeError("core down")
    c = ExtractionCache(lambda: Broken())
    assert c.get("doc", "v1", "t", "k") is None
    assert c.put("doc", "v1", "t", "k", {}) is False
