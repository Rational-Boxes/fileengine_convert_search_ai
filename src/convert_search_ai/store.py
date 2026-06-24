"""Document-state store — one row per processed file in the tenant's schema.

Tracks conversion/indexing status and holds the extracted Markdown that M2's
search builds on. Per-tenant isolation is by Postgres schema (see schema.py): the
connection's ``search_path`` is set to ``tenant_<tenant>`` so ``documents`` is
unqualified. ``psycopg`` is imported lazily via ``db``."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Config


@dataclass
class DocStatus:
    source_version: str
    status: str


class DocumentStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, provision: bool = False):
        from .db import connect_for_tenant
        return connect_for_tenant(self.config, tenant, provision=provision)

    def get_status(self, tenant: str, file_uid: str) -> Optional[DocStatus]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source_version, status FROM documents WHERE file_uid = %s",
                (file_uid,),
            )
            row = cur.fetchone()
            return DocStatus(row[0], row[1]) if row else None

    def upsert(self, tenant: str, file_uid: str, *, source_version: str, mime: str = "",
               name: str = "", path: str = "", content_md: Optional[str] = None,
               status: str = "pending", error: Optional[str] = None,
               provision: bool = True) -> None:
        """Insert or update a document row (provisions the tenant schema by default)."""
        with self._conn(tenant, provision=provision) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (file_uid, source_version, mime, name, path, content_md, status, error, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (file_uid) DO UPDATE SET
                    source_version = EXCLUDED.source_version,
                    mime           = EXCLUDED.mime,
                    name           = EXCLUDED.name,
                    path           = EXCLUDED.path,
                    content_md     = COALESCE(EXCLUDED.content_md, documents.content_md),
                    status         = EXCLUDED.status,
                    error          = EXCLUDED.error,
                    updated_at     = now()
                """,
                (file_uid, source_version, mime, name, path, content_md, status, error),
            )
            conn.commit()

    def delete(self, tenant: str, file_uid: str) -> None:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE file_uid = %s", (file_uid,))
            conn.commit()
