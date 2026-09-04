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

"""In-memory fakes for unit tests (no gRPC core, Redis, or Postgres)."""
from __future__ import annotations

import io

from convert_search_ai._client import NotFoundError
from convert_search_ai.store import DocStatus


class FakeEntry:
    """Mirrors DirectoryEntry: is_container is a property (not is_dir()), and
    the listing carries the size — which is what lets the tree walk refuse an
    oversized file without a stat."""
    def __init__(self, uid, name, is_dir=False, size=0):
        self.uid, self.name, self._dir = uid, name, is_dir
        self.size = size

    @property
    def is_container(self):
        return self._dir


class FakeInfo:
    """Mirrors FileInfo: is_dir is a property (not a method), and carries size."""
    def __init__(self, uid, name, version="v1", is_dir=False, size=0):
        self.uid, self.name, self.version, self._dir = uid, name, version, is_dir
        self.size = size

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
        self.gets = []         # uids whose CONTENT was read — the expensive part
        self._n = 1000

    def add_file(self, uid, name, content=b"", version="v1", is_dir=False, size=None):
        # size defaults to the content's own length, so a test only names it when
        # it wants a file that CLAIMS to be huge without carrying the bytes.
        self.files[uid] = {"name": name, "content": content, "version": version,
                           "dir": is_dir,
                           "size": len(content) if size is None else size}
        return uid

    def stat(self, uid, tenant=None, **kw):
        f = self.files.get(uid)
        if f is None:
            raise NotFoundError("file does not exist", operation="stat", uid=uid)
        return FakeInfo(uid, f["name"], f["version"], f["dir"], f.get("size", 0))

    def get(self, uid, tenant=None, **kw):
        self.gets.append(uid)
        f = self.files.get(uid)
        if f is None:
            raise NotFoundError("file does not exist", operation="get", uid=uid)
        return io.BytesIO(f["content"])

    def touch(self, parent_uid, name, tenant=None, **kw):
        self._n += 1
        uid = f"rend-{self._n}"
        self.renditions.setdefault(parent_uid, {})[name] = uid
        return uid

    def put_stream(self, uid, chunks, tenant=None, **kw):
        # The real client re-splits chunks so no gRPC message is
        # oversized; a stub only has to reassemble them.
        joined = b"".join(
            c.encode() if isinstance(c, str) else bytes(c) for c in chunks)
        return self.put(uid, joined, **kw)

    def put(self, uid, payload, tenant=None, **kw):
        self.puts.append((uid, payload))
        return 123.0

    def dir(self, uid, tenant=None, **kw):
        # A targeted listing of a file's UID returns its rendition children;
        # explicit tree children (for reconcile) are merged in.
        out = list(self.children.get(uid, []))
        out += [FakeEntry(u, n) for n, u in self.renditions.get(uid, {}).items()]
        return out

    def remove(self, uid, tenant=None, **kw):
        # Soft-delete: drop the rendition child with this uid from its parent.
        for names in self.renditions.values():
            for name, rend_uid in list(names.items()):
                if rend_uid == uid:
                    del names[name]
                    return True
        self.files.pop(uid, None)
        return True


class FakeStore:
    def __init__(self):
        self.docs = {}        # (tenant, uid) -> DocStatus
        self.deleted = []
        self.upserts = []
        # Erasure tombstones. The pipeline refuses to convert an erased uid, so
        # a fake without this reports every file as live.
        self.erased = set()

    def get_status(self, tenant, uid):
        return self.docs.get((tenant, uid))

    def upsert(self, tenant, uid, *, source_version, status="pending", **kw):
        self.upserts.append((uid, status, source_version))
        self.docs[(tenant, uid)] = DocStatus(source_version, status)

    def delete(self, tenant, uid):
        self.deleted.append((tenant, uid))
        self.docs.pop((tenant, uid), None)

    def erase(self, tenant, uid, erasure_id=""):
        self.erased.add((tenant, uid))
        self.docs.pop((tenant, uid), None)
        return {"chunks": 0, "extracted_text": False}

    def is_erased(self, tenant, uid):
        return (tenant, uid) in self.erased
