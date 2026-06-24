"""Permission-gated full-text + fuzzy search over extracted Markdown (M2).

Postgres FTS (``websearch_to_tsquery`` over a stored ``tsvector``) with a
``pg_trgm`` fuzzy match on the name and word-similarity on the body, ranked
together. Every candidate is then gated by the **requesting user's** READ
permission (``PermissionGate``) before it is returned, so results never leak a
file the user cannot read. Over-fetch then filter, so the permission step does
not silently shrink a page below ``limit``."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import audit, guards
from .config import Config
from .permissions import PermissionGate

_HEADLINE_OPTS = "MaxFragments=2, MinWords=5, MaxWords=18, ShortWord=2, StartSel=**, StopSel=**"

_SEARCH_SQL = """
WITH q AS (SELECT websearch_to_tsquery('english', %(q)s) AS tsq)
SELECT d.file_uid,
       d.name,
       ts_headline('english', coalesce(d.content_md, ''), q.tsq, %(hl)s) AS snippet,
       ts_rank(d.fts, q.tsq)
         + GREATEST(similarity(d.name, %(q)s), word_similarity(%(q)s, coalesce(d.content_md, ''))) AS score
FROM documents d, q
WHERE d.status IN ('converted', 'indexed')
  AND (
        d.fts @@ q.tsq
        OR (%(fuzzy)s AND (d.name %% %(q)s OR %(q)s <%% coalesce(d.content_md, '')))
      )
ORDER BY score DESC
LIMIT %(fetch)s
"""


@dataclass
class Hit:
    file_uid: str
    name: str
    snippet: str
    score: float


class DocumentSearchRepo:
    """Postgres FTS/trigram queries against a tenant's ``documents`` (psycopg, lazy)."""

    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str):
        from .db import connect_for_tenant
        return connect_for_tenant(self.config, tenant)

    def query(self, tenant: str, q: str, *, fetch: int, fuzzy: bool) -> List[dict]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(_SEARCH_SQL, {"q": q, "hl": _HEADLINE_OPTS, "fuzzy": fuzzy, "fetch": fetch})
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_text(self, tenant: str, file_uid: str) -> Optional[str]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT content_md FROM documents WHERE file_uid = %s", (file_uid,))
            row = cur.fetchone()
            return row[0] if row else None


class SearchService:
    def __init__(self, config: Config, *, repo=None, gate: Optional[PermissionGate] = None,
                 client_factory=None):
        self.config = config
        self.repo = repo or DocumentSearchRepo(config)
        self.gate = gate or PermissionGate(config.permission_cache_ttl)
        self._client_factory = client_factory

    def _client(self, identity):
        if self._client_factory:
            return self._client_factory(identity)
        from .core_client import client_for
        return client_for(identity, self.config)

    def search(self, identity, query: str, *, limit: int = 20, fuzzy: bool = True) -> List[Hit]:
        """Run a permission-gated search. Raises ``guards.GuardError`` for an empty
        or over-long query."""
        q = guards.check_query(query, self.config.max_query_chars)
        limit = guards.cap_limit(limit, self.config.max_results)
        # Over-fetch so the permission filter can't shrink the page below `limit`.
        rows = self.repo.query(identity.tenant, q, fetch=max(limit * 5, limit), fuzzy=fuzzy)
        mf = self._client(identity)
        try:
            hits: List[Hit] = []
            for r in rows:
                if self.gate.can_read(mf, identity, r["file_uid"]):
                    hits.append(Hit(r["file_uid"], r.get("name") or "",
                                    r.get("snippet") or "", float(r.get("score") or 0.0)))
                    if len(hits) >= limit:
                        break
        finally:
            _safe_close(mf)
        audit.record(action="search", user=identity.user, tenant=identity.tenant,
                     result="ok", candidates=len(rows), hits=len(hits))
        return hits

    def get_text(self, identity, file_uid: str):
        """Extracted Markdown for a file the identity may read, as
        ``(text, truncated)``.

        Raises ``PermissionError`` if not readable, ``FileNotFoundError`` if there
        is no extracted text for it."""
        mf = self._client(identity)
        try:
            if not self.gate.can_read(mf, identity, file_uid):
                audit.record(action="document_text", user=identity.user, tenant=identity.tenant,
                             result="denied", file_uid=file_uid)
                raise PermissionError(file_uid)
            text = self.repo.get_text(identity.tenant, file_uid)
            if text is None:
                audit.record(action="document_text", user=identity.user, tenant=identity.tenant,
                             result="missing", file_uid=file_uid)
                raise FileNotFoundError(file_uid)
        finally:
            _safe_close(mf)
        capped, truncated = guards.cap_text_bytes(text, self.config.max_text_bytes)
        audit.record(action="document_text", user=identity.user, tenant=identity.tenant,
                     result="ok", file_uid=file_uid, truncated=truncated)
        return capped, truncated


def _safe_close(mf) -> None:
    try:
        mf.close()
    except Exception:
        pass
