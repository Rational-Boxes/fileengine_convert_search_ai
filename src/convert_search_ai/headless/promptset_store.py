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

"""Prompt-set storage (EXTRACTION_PROMPT_SETS.md §4.1): sets and their numbered
revisions, per tenant. Storage only; the rules (who may publish what, drafts vs
published, application-managed sets) are in ``promptset_service``.

Two implementations with one contract: :class:`PromptSetStore` (Postgres, the
tenant schema) and :class:`MemoryPromptSetStore` (tests). ``tests/
test_promptset_store_live.py`` runs the same assertions against both."""
from __future__ import annotations

import copy
import datetime as _dt
import json
import threading
import uuid
from typing import List, Optional

_SET_COLS = ("id", "slug", "name", "description", "status", "managed", "locked",
             "replaced_by", "current_revision", "created_by", "created_at", "updated_at")
_REV_COLS = ("set_id", "revision", "body", "hash", "state", "managed_by", "created_by",
             "created_at", "published_at")


class SlugTaken(Exception):
    """Another set already has this slug (a concurrent create lost the race)."""


def new_id() -> str:
    return "ps_" + uuid.uuid4().hex[:16]


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


class PromptSetStore:
    def __init__(self, config):
        self.config = config

    def _conn(self, tenant: str, readonly: bool = False):
        from ..db import connect_for_tenant
        # Writes provision the tenant's schema (and so these tables) on first use.
        return connect_for_tenant(self.config, tenant, provision=not readonly,
                                  readonly=readonly)

    @staticmethod
    def _set(row) -> dict:
        return dict(zip(_SET_COLS, row))

    @staticmethod
    def _rev(row) -> dict:
        d = dict(zip(_REV_COLS, row))
        if isinstance(d["body"], str):
            d["body"] = json.loads(d["body"])
        return d

    # ------------------------------------------------------------------ reads
    def list_sets(self, tenant: str) -> List[dict]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_SET_COLS)} FROM prompt_sets ORDER BY lower(name), id")
            return [self._set(r) for r in cur.fetchall()]

    def get_set(self, tenant: str, set_id: str) -> Optional[dict]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_SET_COLS)} FROM prompt_sets WHERE id = %s", (set_id,))
            r = cur.fetchone()
            return self._set(r) if r else None

    def by_slug(self, tenant: str, slug: str) -> Optional[dict]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_SET_COLS)} FROM prompt_sets WHERE slug = %s", (slug,))
            r = cur.fetchone()
            return self._set(r) if r else None

    def get_revision(self, tenant: str, set_id: str, revision: int) -> Optional[dict]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_REV_COLS)} FROM prompt_set_revisions "
                        "WHERE set_id = %s AND revision = %s", (set_id, int(revision)))
            r = cur.fetchone()
            return self._rev(r) if r else None

    def list_revisions(self, tenant: str, set_id: str) -> List[dict]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_REV_COLS)} FROM prompt_set_revisions "
                        "WHERE set_id = %s ORDER BY revision", (set_id,))
            return [self._rev(r) for r in cur.fetchall()]

    def count_sets(self, tenant: str, managed: str) -> int:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM prompt_sets WHERE managed = %s", (managed,))
            return int(cur.fetchone()[0])

    def revisions_since(self, tenant: str, set_id: str, since) -> int:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM prompt_set_revisions "
                        "WHERE set_id = %s AND created_at >= %s", (set_id, since))
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------------ writes
    def create_set(self, tenant: str, *, set_id: str, slug: Optional[str], name: str,
                   description: str, managed: str, created_by: str) -> dict:
        import psycopg
        try:
            with self._conn(tenant) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO prompt_sets (id, slug, name, description, managed, created_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (set_id, slug or None, name, description, managed, created_by))
                conn.commit()
        except psycopg.errors.UniqueViolation:
            raise SlugTaken(slug)
        return self.get_set(tenant, set_id)

    def add_revision(self, tenant: str, set_id: str, *, body: dict, hash: str, state: str,
                     user: str, managed_by: Optional[dict] = None) -> int:
        """Append a revision (numbered under a row lock on the set)."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM prompt_sets WHERE id = %s FOR UPDATE", (set_id,))
            if cur.fetchone() is None:
                raise KeyError(set_id)
            cur.execute("SELECT coalesce(max(revision), 0) + 1 FROM prompt_set_revisions "
                        "WHERE set_id = %s", (set_id,))
            n = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO prompt_set_revisions (set_id, revision, body, hash, state, "
                "managed_by, created_by, published_at) VALUES (%s, %s, %s::jsonb, %s, %s, "
                "%s::jsonb, %s, CASE WHEN %s = 'published' THEN now() END)",
                (set_id, n, json.dumps(body, ensure_ascii=False), hash, state,
                 json.dumps(managed_by) if managed_by else None, user, state))
            if state == "published":
                self._sync(cur, set_id, n, body)
            else:
                cur.execute("UPDATE prompt_sets SET updated_at = now() WHERE id = %s", (set_id,))
            conn.commit()
            return n

    def update_draft(self, tenant: str, set_id: str, revision: int, *, body: dict, hash: str,
                     user: str) -> bool:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("UPDATE prompt_set_revisions SET body = %s::jsonb, hash = %s, "
                        "created_by = %s, created_at = now() "
                        "WHERE set_id = %s AND revision = %s AND state = 'draft'",
                        (json.dumps(body, ensure_ascii=False), hash, user, set_id, int(revision)))
            ok = cur.rowcount == 1
            cur.execute("UPDATE prompt_sets SET updated_at = now() WHERE id = %s", (set_id,))
            conn.commit()
            return ok

    def publish_revision(self, tenant: str, set_id: str, revision: int) -> bool:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("UPDATE prompt_set_revisions SET state = 'published', published_at = now() "
                        "WHERE set_id = %s AND revision = %s AND state = 'draft' RETURNING body",
                        (set_id, int(revision)))
            r = cur.fetchone()
            if r is None:
                conn.rollback()
                return False
            body = r[0] if isinstance(r[0], dict) else json.loads(r[0])
            self._sync(cur, set_id, int(revision), body)
            conn.commit()
            return True

    @staticmethod
    def _sync(cur, set_id: str, revision: int, body: dict) -> None:
        """The set row mirrors its current published revision (name, description,
        slug) so listing does not read every body."""
        cur.execute("UPDATE prompt_sets SET current_revision = %s, name = %s, description = %s, "
                    "slug = coalesce(nullif(%s, ''), slug), status = 'active', "
                    "updated_at = now() WHERE id = %s",
                    (revision, body.get("name", ""), body.get("description", ""),
                     body.get("slug") or "", set_id))

    def update_set(self, tenant: str, set_id: str, **fields) -> bool:
        allowed = {"status", "managed", "locked", "replaced_by", "slug"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return False
        sets = ", ".join(f"{k} = %s" for k in cols)
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE prompt_sets SET {sets}, updated_at = now() WHERE id = %s",
                        (*cols.values(), set_id))
            ok = cur.rowcount == 1
            conn.commit()
            return ok


class MemoryPromptSetStore:
    """The same contract in memory (route and service tests)."""

    def __init__(self):
        self.sets: dict = {}
        self.revs: dict = {}
        self._lock = threading.Lock()

    def _t(self, tenant):
        return self.sets.setdefault(tenant, {}), self.revs.setdefault(tenant, {})

    def list_sets(self, tenant):
        sets, _ = self._t(tenant)
        return sorted((dict(s) for s in sets.values()), key=lambda s: (s["name"].lower(), s["id"]))

    def get_set(self, tenant, set_id):
        s = self._t(tenant)[0].get(set_id)
        return dict(s) if s else None

    def by_slug(self, tenant, slug):
        for s in self._t(tenant)[0].values():
            if s["slug"] == slug:
                return dict(s)
        return None

    def get_revision(self, tenant, set_id, revision):
        r = self._t(tenant)[1].get((set_id, int(revision)))
        return copy.deepcopy(r) if r else None

    def list_revisions(self, tenant, set_id):
        _, revs = self._t(tenant)
        return [self.get_revision(tenant, s, n) for (s, n) in sorted(revs) if s == set_id]

    def count_sets(self, tenant, managed):
        return sum(1 for s in self._t(tenant)[0].values() if s["managed"] == managed)

    def revisions_since(self, tenant, set_id, since):
        return sum(1 for (s, _n), r in self._t(tenant)[1].items()
                   if s == set_id and r["created_at"] >= since)

    def create_set(self, tenant, *, set_id, slug, name, description, managed, created_by):
        with self._lock:
            sets, _ = self._t(tenant)
            if slug and any(s["slug"] == slug for s in sets.values()):
                raise SlugTaken(slug)
            now = _now()
            sets[set_id] = {"id": set_id, "slug": slug or None, "name": name,
                            "description": description, "status": "active", "managed": managed,
                            "locked": False, "replaced_by": "", "current_revision": None,
                            "created_by": created_by, "created_at": now, "updated_at": now}
        return self.get_set(tenant, set_id)

    def add_revision(self, tenant, set_id, *, body, hash, state, user, managed_by=None):
        with self._lock:
            sets, revs = self._t(tenant)
            if set_id not in sets:
                raise KeyError(set_id)
            n = max([k[1] for k in revs if k[0] == set_id] or [0]) + 1
            now = _now()
            revs[(set_id, n)] = {"set_id": set_id, "revision": n,
                                 "body": json.loads(json.dumps(body)), "hash": hash,
                                 "state": state, "managed_by": managed_by, "created_by": user,
                                 "created_at": now,
                                 "published_at": now if state == "published" else None}
            if state == "published":
                self._sync(sets[set_id], n, body)
            sets[set_id]["updated_at"] = now
            return n

    def update_draft(self, tenant, set_id, revision, *, body, hash, user):
        r = self._t(tenant)[1].get((set_id, int(revision)))
        if not r or r["state"] != "draft":
            return False
        r.update(body=json.loads(json.dumps(body)), hash=hash, created_by=user,
                 created_at=_now())
        return True

    def publish_revision(self, tenant, set_id, revision):
        sets, revs = self._t(tenant)
        r = revs.get((set_id, int(revision)))
        if not r or r["state"] != "draft":
            return False
        r.update(state="published", published_at=_now())
        self._sync(sets[set_id], int(revision), r["body"])
        return True

    @staticmethod
    def _sync(s, n, body):
        s.update(current_revision=n, name=body.get("name", ""),
                 description=body.get("description", ""), status="active",
                 updated_at=_now())
        if body.get("slug"):
            s["slug"] = body["slug"]

    def update_set(self, tenant, set_id, **fields):
        s = self._t(tenant)[0].get(set_id)
        if not s:
            return False
        for k in ("status", "managed", "locked", "replaced_by", "slug"):
            if k in fields:
                s[k] = fields[k]
        s["updated_at"] = _now()
        return True
