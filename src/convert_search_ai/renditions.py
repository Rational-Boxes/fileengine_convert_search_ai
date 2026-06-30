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


def _safe_version(version: str) -> str:
    return _UNSAFE.sub("_", (version or "0").strip()) or "0"


def rendition_name(version: str, fmt: str, ext: str) -> str:
    """``<version>-<fmt>.<ext>`` with the version sanitized for a filename."""
    return f"{_safe_version(version)}-{fmt}.{ext}"


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

    def names_for_version(self, file_uid: str, version: str, tenant: str) -> List[str]:
        """All rendition child names already present for ``(file_uid, version)`` —
        used to report the full current set on an on-demand (re)generate, even
        when this call wrote nothing new (renditions already existed)."""
        prefix = f"{_safe_version(version)}-"
        return sorted(n for n in self.existing_names(file_uid, tenant) if n.startswith(prefix))

    def write(self, file_uid: str, version: str, renditions: List[Rendition], tenant: str) -> List[str]:
        """Idempotently write ``renditions`` for ``(file_uid, version)``.

        Returns the names actually created this call (skips ones already present)."""
        existing = self.existing_names(file_uid, tenant)
        written: List[str] = []
        for r in renditions:
            name = rendition_name(version, r.fmt, r.ext)
            if name in existing:
                continue
            # touch + put raise on failure (e.g. WriteUnavailableError while the
            # core is read-only during a failover) — propagated so the caller can
            # retry/reconcile rather than silently dropping the rendition.
            rend_uid = self.mf.touch(file_uid, name, tenant=tenant)
            self.mf.put(rend_uid, r.data, tenant=tenant)
            written.append(name)
            existing.add(name)
        return written
