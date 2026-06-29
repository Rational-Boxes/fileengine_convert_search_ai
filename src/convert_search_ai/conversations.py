"""Persisted chat conversations — per (tenant, user), in the tenant's schema.

Lets the chat UI list/resume/delete past chats. Ownership is enforced by always
scoping queries on ``user_id`` so a user only ever sees/touches their own chats.
Per-tenant isolation is by Postgres schema (see schema.py); ``psycopg`` is lazy
via ``db.connect_for_tenant``."""
from __future__ import annotations

import json
import uuid
from typing import List, Optional

from .config import Config

_TITLE_MAX = 120


class ConversationStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, provision: bool = False, readonly: bool = False):
        from .db import connect_for_tenant
        return connect_for_tenant(self.config, tenant, provision=provision, readonly=readonly)

    def list(self, tenant: str, user: str, *, limit: int = 200) -> List[dict]:
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, updated_at FROM conversations "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
                (user, int(limit)))
            return [{"id": r[0], "title": r[1], "updated_at": r[2].isoformat()}
                    for r in cur.fetchall()]

    def create(self, tenant: str, user: str, *, title: str = "") -> str:
        cid = uuid.uuid4().hex
        with self._conn(tenant, provision=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, %s)",
                (cid, user, (title or "")[:_TITLE_MAX]))
            conn.commit()
        return cid

    def get(self, tenant: str, user: str, conversation_id: str) -> Optional[dict]:
        """The conversation + its messages, or None if it isn't the user's."""
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, title FROM conversations WHERE id = %s AND user_id = %s",
                        (conversation_id, user))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "SELECT role, content, citations FROM conversation_messages "
                "WHERE conversation_id = %s ORDER BY id", (conversation_id,))
            messages = [{"role": m[0], "content": m[1], "citations": m[2] or []}
                        for m in cur.fetchall()]
            return {"id": row[0], "title": row[1], "messages": messages}

    def owns(self, tenant: str, user: str, conversation_id: str) -> bool:
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM conversations WHERE id = %s AND user_id = %s",
                        (conversation_id, user))
            return cur.fetchone() is not None

    def append(self, tenant: str, user: str, conversation_id: str, role: str,
               content: str, citations: Optional[list] = None) -> bool:
        """Append a message (owner-scoped) and bump updated_at. False if not owned."""
        with self._conn(tenant, provision=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM conversations WHERE id = %s AND user_id = %s",
                        (conversation_id, user))
            if not cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content, citations) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (conversation_id, role, content,
                 json.dumps(citations) if citations is not None else None))
            cur.execute("UPDATE conversations SET updated_at = now() WHERE id = %s",
                        (conversation_id,))
            conn.commit()
            return True

    def set_title_if_empty(self, tenant: str, user: str, conversation_id: str,
                           title: str) -> None:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET title = %s "
                "WHERE id = %s AND user_id = %s AND coalesce(title, '') = ''",
                ((title or "")[:_TITLE_MAX], conversation_id, user))
            conn.commit()

    def delete(self, tenant: str, user: str, conversation_id: str) -> bool:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE id = %s AND user_id = %s",
                        (conversation_id, user))
            conn.commit()
            return cur.rowcount > 0
