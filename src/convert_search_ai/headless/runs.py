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

"""Headless run records (HEADLESS_RESEARCH_API.md §3.5, §3.6).

A run is a conversation with ``origin = 'headless'``, attributed to the key it
came from (``client_key_id``, ``client_label``). The research sidebar lists
interactive conversations only; the web application shows headless runs in a
folded section per key, read-only.

- The request is the first message (``user``), the summary the last
  (``assistant``); a question or report run stores its turns as a chat does.
- Each extraction ``item`` is a ``headless_results`` row keyed by ``file_uid``,
  so erasure of a source file can destroy that file's values and quotes from
  every run (``DocumentStore.erase``) and leave the row as a tombstone.
- Runs are kept: there is no expiry, and the web application cannot delete
  them — they are the record of what an automated client did as the user.
- Owner-scoped like conversations: every query carries ``user_id``.

Persistence is best-effort for the caller: a store outage must not fail a run
the client is waiting on, so the route logs and carries on (``run_id`` None)."""
from __future__ import annotations

import json
import uuid
from typing import List, Optional

_TITLE_MAX = 120


class RunStore:
    def __init__(self, config):
        self.config = config

    def _conn(self, tenant: str, provision: bool = False, readonly: bool = False):
        from ..db import connect_for_tenant
        return connect_for_tenant(self.config, tenant, provision=provision, readonly=readonly)

    # ---------------------------------------------------------------- writes
    def create(self, identity, *, title: str, request: dict) -> str:
        rid = uuid.uuid4().hex
        with self._conn(identity.tenant, provision=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, user_id, title, origin, client_key_id, client_label) "
                "VALUES (%s, %s, %s, 'headless', %s, %s)",
                (rid, identity.user, (title or "")[:_TITLE_MAX],
                 identity.client_key_id or "", identity.client_label or ""))
            cur.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content) "
                "VALUES (%s, 'user', %s)", (rid, json.dumps(request, ensure_ascii=False)))
            conn.commit()
        return rid

    def add_result(self, identity, run_id: str, item: dict) -> None:
        with self._conn(identity.tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO headless_results (conversation_id, file_uid, version, name, result) "
                "SELECT %s, %s, %s, %s, %s::jsonb WHERE EXISTS ("
                "  SELECT 1 FROM conversations WHERE id = %s AND user_id = %s)",
                (run_id, item.get("file_uid", ""), item.get("version", ""),
                 item.get("name", ""), json.dumps(item, ensure_ascii=False),
                 run_id, identity.user))
            cur.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (run_id,))
            conn.commit()

    def append(self, identity, run_id: str, role: str, content: str,
               citations: Optional[list] = None) -> None:
        with self._conn(identity.tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content, citations) "
                "SELECT %s, %s, %s, %s::jsonb WHERE EXISTS ("
                "  SELECT 1 FROM conversations WHERE id = %s AND user_id = %s)",
                (run_id, role, content,
                 json.dumps(citations) if citations is not None else None,
                 run_id, identity.user))
            cur.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (run_id,))
            conn.commit()

    def finish(self, identity, run_id: str, summary: dict) -> None:
        self.append(identity, run_id, "assistant", json.dumps(summary, ensure_ascii=False))

    # ----------------------------------------------------------------- reads
    def applications(self, tenant: str, user: str, *, limit_per_app: int = 50) -> List[dict]:
        """The user's headless runs, one group per key, newest first."""
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT client_key_id, id, title, updated_at, client_label FROM ("
                "  SELECT c.client_key_id, c.id, c.title, c.updated_at, c.client_label, "
                "         row_number() OVER (PARTITION BY c.client_key_id "
                "                            ORDER BY c.updated_at DESC) AS rn "
                "  FROM conversations c WHERE c.user_id = %s AND c.origin = 'headless') r "
                "WHERE rn <= %s ORDER BY client_key_id, updated_at DESC",
                (user, int(limit_per_app)))
            rows = cur.fetchall()
            cur.execute(
                "SELECT client_key_id, count(*), max(updated_at) FROM conversations "
                "WHERE user_id = %s AND origin = 'headless' GROUP BY client_key_id", (user,))
            totals = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        apps: dict = {}
        for key, rid, title, updated, label in rows:
            a = apps.setdefault(key, {"client_key_id": key, "label": label or "", "runs": []})
            # The newest run carries the latest label (a renamed key).
            if not a["label"] and label:
                a["label"] = label
            a["runs"].append({"id": rid, "title": title, "updated_at": updated.isoformat()})
        out = []
        for key, a in apps.items():
            n, last = totals.get(key, (len(a["runs"]), None))
            a["run_count"] = n
            a["last_run"] = last.isoformat() if last else ""
            out.append(a)
        out.sort(key=lambda a: a["last_run"], reverse=True)
        return out

    def get(self, tenant: str, user: str, run_id: str) -> Optional[dict]:
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, client_key_id, client_label, created_at FROM conversations "
                "WHERE id = %s AND user_id = %s AND origin = 'headless'", (run_id, user))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "SELECT role, content, citations FROM conversation_messages "
                "WHERE conversation_id = %s ORDER BY id", (run_id,))
            messages = [{"role": m[0], "content": m[1], "citations": m[2] or []}
                        for m in cur.fetchall()]
            cur.execute(
                "SELECT file_uid, version, name, result, erased FROM headless_results "
                "WHERE conversation_id = %s ORDER BY id", (run_id,))
            results = []
            for f, v, n, res, erased in cur.fetchall():
                results.append({"file_uid": f, "version": v, "name": n, "erased": True}
                               if erased else (res or {"file_uid": f, "version": v, "name": n}))
        return {"id": row[0], "title": row[1], "origin": "headless",
                "client_key_id": row[2], "client_label": row[3],
                "created_at": row[4].isoformat(), "messages": messages, "results": results}

    def is_headless(self, tenant: str, user: str, run_id: str) -> bool:
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM conversations c WHERE id = %s AND user_id = %s "
                "AND coalesce(to_jsonb(c) ->> 'origin', 'interactive') = 'headless'",
                (run_id, user))
            return cur.fetchone() is not None
