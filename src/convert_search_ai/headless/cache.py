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

"""The extraction cache: a version-keyed sidecar of the source file (§3.7).

``<version>-extract.json`` is a hidden child of the document, one per source
version, written by the rendition convention. Inside, one entry per question —
the hash of (schema, instructions, model, prompt version) — so the same question
about the same version is answered once and never paid for again.

It follows the file: a revised document is a new version with no sidecar yet (so
stale values are never served), the sidecar is readable by whoever can READ the
document, it is pruned with the other renditions of a superseded version, and it
goes when the document is erased.

Written as csai's own service identity, as renditions are, so a caller with only
READ still has their result cached; READ is checked as the caller before any
lookup. Two runs adding entries at once may race and one entry be lost — it is
recomputed next time, never wrong. Each write is a new version of the sidecar,
so earlier entries stay in its history."""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from ..renditions import rendition_name

log = logging.getLogger("convert_search_ai.headless.cache")

FMT = "extract"
SCHEMA = "fileengine.extract_cache.v1"


def sidecar_name(version: str) -> str:
    return rendition_name(version, FMT, "json")


class ExtractionCache:
    def __init__(self, agent_client_factory: Callable[[], object]):
        self._agent = agent_client_factory

    def _open(self):
        return self._agent()

    @staticmethod
    def _close(mf) -> None:
        close = getattr(mf, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _find(mf, file_uid: str, version: str, tenant: str) -> Optional[str]:
        name = sidecar_name(version)
        for e in mf.dir(file_uid, tenant=tenant) or []:
            if e.name == name:
                return e.uid
        return None

    @staticmethod
    def _read(mf, uid: str, tenant: str) -> dict:
        raw = mf.get(uid, tenant=tenant).read()
        doc = json.loads(raw.decode("utf-8")) if raw else {}
        return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else {}

    def get(self, file_uid: str, version: str, tenant: str, key: str) -> Optional[dict]:
        """The cached entry for ``key``, or None (no sidecar, no entry, unreadable)."""
        mf = self._open()
        try:
            uid = self._find(mf, file_uid, version, tenant)
            if uid is None:
                return None
            entry = (self._read(mf, uid, tenant).get("entries") or {}).get(key)
            return entry if isinstance(entry, dict) else None
        except Exception:
            log.warning("extract cache read failed for %s@%s", file_uid, version, exc_info=True)
            return None
        finally:
            self._close(mf)

    def put(self, file_uid: str, version: str, tenant: str, key: str, entry: dict) -> bool:
        """Add or replace one entry. Best-effort: a failed write costs a repeat
        extraction later, never a wrong answer, so it is logged, not raised."""
        mf = self._open()
        try:
            uid = self._find(mf, file_uid, version, tenant)
            doc = self._read(mf, uid, tenant) if uid else {}
            entries = dict(doc.get("entries") or {})
            entries[key] = entry
            body = json.dumps({"schema": SCHEMA, "file_uid": file_uid, "version": version,
                               "entries": entries}, ensure_ascii=False).encode("utf-8")
            if uid is None:
                uid = mf.touch(file_uid, sidecar_name(version), tenant=tenant)
            mf.put_stream(uid, [body], tenant=tenant)
            return True
        except Exception:
            log.warning("extract cache write failed for %s@%s", file_uid, version, exc_info=True)
            return False
        finally:
            self._close(mf)
