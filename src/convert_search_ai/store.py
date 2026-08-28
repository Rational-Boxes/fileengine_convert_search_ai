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

"""Document-state store — one row per processed file in the tenant's schema.

Tracks conversion/indexing status and holds the extracted Markdown that M2's
search builds on. Per-tenant isolation is by Postgres schema (see schema.py): the
connection's ``search_path`` is set to ``tenant_<tenant>`` so ``documents`` is
unqualified. ``psycopg`` is imported lazily via ``db``."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from .config import Config


@dataclass
class DocStatus:
    source_version: str
    status: str


@dataclass
class DocRow:
    """One document as the reconcile sweep sees it — enough to decide whether it
    needs re-converting without fetching its content."""
    file_uid: str
    status: str
    mime: str
    name: str
    source_version: str
    chunks: int


class DocumentStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, provision: bool = False, readonly: bool = False):
        from .db import connect_for_tenant
        return connect_for_tenant(self.config, tenant, provision=provision, readonly=readonly)

    def get_status(self, tenant: str, file_uid: str) -> Optional[DocStatus]:
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source_version, status FROM documents WHERE file_uid = %s",
                (file_uid,),
            )
            row = cur.fetchone()
            return DocStatus(row[0], row[1]) if row else None

    def list_documents(self, tenant: str, *, statuses: Optional[Sequence[str]] = None,
                       limit: Optional[int] = None) -> List[DocRow]:
        """Documents in this tenant, with their chunk counts.

        The chunk count is the point: status alone cannot distinguish a document
        that holds no text from one whose text never reached the index, and the
        sweep has to tell those apart. Counted with a LEFT JOIN aggregate rather
        than a per-row query so a sweep over a large corpus is one round trip.

        ``statuses`` filters server-side; ``None`` returns every row, which is what
        the sweep wants — it re-judges 'unsupported' and 'converted' against the
        current plugin registry, so it cannot pre-filter on status without
        deciding the answer first."""
        sql = ["SELECT d.file_uid, d.status, d.mime, d.name, d.source_version,",
               "       count(c.id) AS chunks",
               "  FROM documents d LEFT JOIN chunks c ON c.file_uid = d.file_uid"]
        params: list = []
        if statuses:
            sql.append(" WHERE d.status = ANY(%s)")
            params.append(list(statuses))
        sql.append(" GROUP BY d.file_uid, d.status, d.mime, d.name, d.source_version")
        sql.append(" ORDER BY d.updated_at")
        if limit:
            sql.append(" LIMIT %s")
            params.append(int(limit))
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute("\n".join(sql), params)
            return [DocRow(file_uid=r[0], status=r[1], mime=r[2] or "", name=r[3] or "",
                           source_version=r[4] or "", chunks=int(r[5]))
                    for r in cur.fetchall()]

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
