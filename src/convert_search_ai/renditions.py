"""Write conversion renditions back into FileEngine as hidden children.

Per ``file_engine_core/design_documents/file_renditions.md``: a rendition is a
child file whose ``parent_uid`` is the source file's UID, created via the normal
gRPC calls (no special RPC). We name them ``<version>-<fmt>.<ext>`` so processing
is **idempotent** — a rendition for a given source version + format is written at
most once, and a new version produces new (superseding) names."""
from __future__ import annotations

import re
from typing import List

from .plugins.base import Rendition

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def rendition_name(version: str, fmt: str, ext: str) -> str:
    """``<version>-<fmt>.<ext>`` with the version sanitized for a filename."""
    v = _UNSAFE.sub("_", (version or "0").strip()) or "0"
    return f"{v}-{fmt}.{ext}"


class RenditionWriter:
    """Writes renditions as hidden children using an agent-bound ManagedFiles."""

    def __init__(self, mf):
        self.mf = mf

    def existing_names(self, file_uid: str, tenant: str) -> set:
        # A targeted listing of a file's UID returns its hidden children
        # (renditions) — the core's documented behavior; dir(file_uid) is the
        # direct call (equivalent to ManagedFiles.list_renditions()).
        entries = self.mf.dir(file_uid, tenant=tenant)
        if not entries:
            return set()
        return {e.name for e in entries}

    def write(self, file_uid: str, version: str, renditions: List[Rendition], tenant: str) -> List[str]:
        """Idempotently write ``renditions`` for ``(file_uid, version)``.

        Returns the names actually created this call (skips ones already present)."""
        existing = self.existing_names(file_uid, tenant)
        written: List[str] = []
        for r in renditions:
            name = rendition_name(version, r.fmt, r.ext)
            if name in existing:
                continue
            rend_uid = self.mf.touch(file_uid, name, tenant=tenant)
            if not rend_uid:
                continue
            if self.mf.put(rend_uid, r.data, tenant=tenant) is not False:
                written.append(name)
                existing.add(name)
        return written
