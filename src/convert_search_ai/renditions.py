"""Write conversion renditions back into FileEngine as hidden children.

Per ``file_engine_core/design_documents/file_renditions.md``: a rendition is a
child file whose ``parent_uid`` is the source file's UID, created via the normal
gRPC calls (no special RPC). We name them ``<version>-<fmt>.<ext>`` so processing
is **idempotent** — a rendition for a given source version + format is written at
most once, and a new version produces new (superseding) names."""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from .plugins.base import Rendition

log = logging.getLogger("convert_search_ai.renditions")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# The rendition fmt vocabulary (matches plugins' Rendition.fmt and the frontend's
# KNOWN allowlist). Used to recognize our own hidden children when pruning, so a
# non-rendition child can never be mistaken for one and removed.
_KNOWN_FMTS = frozenset({"thumbnail", "preview", "pdf", "poster", "model"})


def _safe_version(version: str) -> str:
    return _UNSAFE.sub("_", (version or "0").strip()) or "0"


def rendition_name(version: str, fmt: str, ext: str) -> str:
    """``<version>-<fmt>.<ext>`` with the version sanitized for a filename."""
    return f"{_safe_version(version)}-{fmt}.{ext}"


def parse_rendition_name(name: str) -> Optional[Tuple[str, str, str]]:
    """``(version, fmt, ext)`` for a ``<version>-<fmt>.<ext>`` rendition child, or
    ``None`` if ``name`` isn't a recognized rendition. Parsed right-to-left so a
    version containing ``-`` is handled (the fmt token never contains one)."""
    dot = name.rfind(".")
    if dot <= 0:
        return None
    stem, ext = name[:dot], name[dot + 1:]
    dash = stem.rfind("-")
    if dash <= 0:
        return None
    version, fmt = stem[:dash], stem[dash + 1:]
    if not version or not ext or fmt not in _KNOWN_FMTS:
        return None
    return version, fmt, ext


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

    def prune_old_versions(self, file_uid: str, keep_version: str, tenant: str) -> List[str]:
        """Remove rendition children belonging to *other* (superseded) source
        versions, across all formats, keeping only ``keep_version``'s.

        Call after the current version's renditions are written, so a viewable set
        always exists. Best-effort: a failed delete is logged, not raised, so
        cleanup never fails the conversion. Returns the names removed."""
        keep = _safe_version(keep_version)
        entries = self.mf.dir(file_uid, tenant=tenant) or []
        removed: List[str] = []
        for e in entries:
            parsed = parse_rendition_name(e.name)
            if not parsed:
                continue                       # not one of our renditions — leave it
            version, _fmt, _ext = parsed
            if version == keep:
                continue                       # current version's rendition — keep
            try:
                self.mf.remove(e.uid, tenant=tenant)
                removed.append(e.name)
            except Exception:
                log.warning("could not prune stale rendition %s (%s) of %s",
                            e.name, e.uid, file_uid, exc_info=True)
        return removed
