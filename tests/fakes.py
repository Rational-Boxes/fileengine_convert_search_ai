"""In-memory fakes for unit tests (no gRPC core, Redis, or Postgres)."""
from __future__ import annotations

import io

from convert_search_ai.store import DocStatus


class FakeEntry:
    """Mirrors DirectoryEntry: is_container is a property (not is_dir())."""
    def __init__(self, uid, name, is_dir=False):
        self.uid, self.name, self._dir = uid, name, is_dir

    @property
    def is_container(self):
        return self._dir


class FakeInfo:
    """Mirrors FileInfo: is_dir is a property (not a method)."""
    def __init__(self, uid, name, version="v1", is_dir=False):
        self.uid, self.name, self.version, self._dir = uid, name, version, is_dir

    @property
    def is_dir(self):
        return self._dir


class FakeMF:
    """Mimics the bits of ManagedFiles the pipeline/renditions/reconcile use."""

    def __init__(self):
        self.files = {}        # uid -> {name, content, version, dir}
        self.renditions = {}   # parent_uid -> {name: rend_uid}
        self.children = {}     # parent_uid -> [FakeEntry] (for dir()/reconcile)
        self.puts = []         # (uid, bytes)
        self._n = 1000

    def add_file(self, uid, name, content=b"", version="v1", is_dir=False):
        self.files[uid] = {"name": name, "content": content, "version": version, "dir": is_dir}
        return uid

    def stat(self, uid, tenant=None, **kw):
        f = self.files.get(uid)
        return FakeInfo(uid, f["name"], f["version"], f["dir"]) if f else None

    def get(self, uid, tenant=None, **kw):
        f = self.files.get(uid)
        return io.BytesIO(f["content"]) if f else False

    def touch(self, parent_uid, name, tenant=None, **kw):
        self._n += 1
        uid = f"rend-{self._n}"
        self.renditions.setdefault(parent_uid, {})[name] = uid
        return uid

    def put(self, uid, payload, tenant=None, **kw):
        self.puts.append((uid, payload))
        return 123.0

    def dir(self, uid, tenant=None, **kw):
        # A targeted listing of a file's UID returns its rendition children;
        # explicit tree children (for reconcile) are merged in.
        out = list(self.children.get(uid, []))
        out += [FakeEntry(u, n) for n, u in self.renditions.get(uid, {}).items()]
        return out


class FakeStore:
    def __init__(self):
        self.docs = {}        # (tenant, uid) -> DocStatus
        self.deleted = []
        self.upserts = []

    def get_status(self, tenant, uid):
        return self.docs.get((tenant, uid))

    def upsert(self, tenant, uid, *, source_version, status="pending", **kw):
        self.upserts.append((uid, status, source_version))
        self.docs[(tenant, uid)] = DocStatus(source_version, status)

    def delete(self, tenant, uid):
        self.deleted.append((tenant, uid))
        self.docs.pop((tenant, uid), None)
