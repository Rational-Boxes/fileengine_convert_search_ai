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

"""The durable media publish job (MEDIA_SHARE.md §4.4) — MS2.

A publish is a JOB, not a call: a VP9 encode of a long source is tens of minutes
of CPU, it must survive a worker restart, and the person who asked wants to see
progress rather than a spinner. One row per (file, source version, profile) in
the tenant's ``media_jobs`` table; the UNIQUE constraint is the idempotency.

Two implementations with one contract: :class:`PostgresMediaJobStore` (the real
one) and :class:`MemoryMediaJobStore` (for the worker's unit tests). The
Postgres one is also tested live, because an in-memory double once passed every
test while the real query failed.
"""
from __future__ import annotations

import datetime as _dt
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

#: Terminal states. A job in one of these is never picked up again.
TERMINAL = ("succeeded", "skipped", "failed", "cancelled")
#: States a re-request may restart: the retry / re-encode path (§4.6).
RESTARTABLE = ["failed", "cancelled"]

_COLS = ("job_uid, file_uid, source_version, profile, status, requested_by, requested_at, "
         "started_at, finished_at, heartbeat_at, attempts, progress_pct, source_bytes, "
         "output_bytes, duration_ms, width, height, encoder, rendition_name, detail")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


@dataclass
class MediaJob:
    job_uid: str
    file_uid: str
    source_version: str
    profile: str
    status: str
    requested_by: str
    requested_at: Optional[_dt.datetime] = None
    started_at: Optional[_dt.datetime] = None
    finished_at: Optional[_dt.datetime] = None
    heartbeat_at: Optional[_dt.datetime] = None
    attempts: int = 0
    progress_pct: int = 0
    source_bytes: Optional[int] = None
    output_bytes: Optional[int] = None
    duration_ms: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    encoder: Optional[str] = None
    rendition_name: Optional[str] = None
    detail: Optional[str] = None

    def to_api(self) -> dict:
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        return {
            "job_uid": str(self.job_uid), "file_uid": self.file_uid,
            "source_version": self.source_version, "profile": self.profile,
            "status": self.status, "requested_by": self.requested_by,
            "requested_at": iso(self.requested_at), "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at), "attempts": self.attempts,
            "progress_pct": self.progress_pct, "output_bytes": self.output_bytes,
            "duration_ms": self.duration_ms, "width": self.width, "height": self.height,
            "encoder": self.encoder, "rendition_name": self.rendition_name,
            "detail": self.detail,
        }


def _row(r) -> MediaJob:
    return MediaJob(job_uid=str(r[0]), file_uid=r[1], source_version=r[2], profile=r[3],
                    status=r[4], requested_by=r[5], requested_at=r[6], started_at=r[7],
                    finished_at=r[8], heartbeat_at=r[9], attempts=int(r[10] or 0),
                    progress_pct=int(r[11] or 0), source_bytes=r[12], output_bytes=r[13],
                    duration_ms=r[14], width=r[15], height=r[16], encoder=r[17],
                    rendition_name=r[18], detail=r[19])


# ── Postgres ────────────────────────────────────────────────────────────────


class PostgresMediaJobStore:
    """``media_jobs`` in each tenant's schema (created by the guarded tenant DDL)."""

    def __init__(self, config):
        self.config = config

    def _conn(self, tenant: str):
        from .db import connect_for_tenant
        return connect_for_tenant(self.config, tenant, provision=True)

    def request(self, tenant: str, file_uid: str, source_version: str, profile: str,
                requested_by: str) -> tuple[MediaJob, bool]:
        """The job for (file, version, profile), creating it if absent. Returns
        ``(job, created)``. A failed or cancelled job is RESTARTED — the retry /
        re-encode path — and reported as created; any other existing job is
        returned unchanged (asking twice never queues twice)."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO media_jobs (job_uid, file_uid, source_version, profile, status, "
                f"requested_by) VALUES (%s, %s, %s, %s, 'queued', %s) "
                f"ON CONFLICT (file_uid, source_version, profile) DO NOTHING RETURNING {_COLS}",
                (str(uuid.uuid4()), file_uid, source_version, profile, requested_by))
            row = cur.fetchone()
            if row is not None:
                conn.commit()
                return _row(row), True
            cur.execute(
                f"UPDATE media_jobs SET status = 'queued', requested_by = %s, requested_at = now(), "
                f"started_at = NULL, finished_at = NULL, heartbeat_at = NULL, attempts = 0, "
                f"progress_pct = 0, detail = NULL "
                f"WHERE file_uid = %s AND source_version = %s AND profile = %s "
                f"AND status = ANY(%s) RETURNING {_COLS}",
                (requested_by, file_uid, source_version, profile, RESTARTABLE))
            row = cur.fetchone()
            if row is not None:
                conn.commit()
                return _row(row), True
            cur.execute(f"SELECT {_COLS} FROM media_jobs WHERE file_uid = %s AND "
                        f"source_version = %s AND profile = %s",
                        (file_uid, source_version, profile))
            row = cur.fetchone()
            conn.commit()
            return _row(row), False

    def claim(self, tenant: str) -> Optional[MediaJob]:
        """Take the oldest queued job, atomically. ``SKIP LOCKED`` so two workers
        never take the same one and neither waits on the other."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE media_jobs SET status = 'running', started_at = now(), "
                f"heartbeat_at = now(), attempts = attempts + 1, progress_pct = 0 "
                f"WHERE job_uid = (SELECT job_uid FROM media_jobs WHERE status = 'queued' "
                f"ORDER BY requested_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING {_COLS}")
            row = cur.fetchone()
            conn.commit()
            return _row(row) if row else None

    def heartbeat(self, tenant: str, job_uid: str, progress_pct: int) -> str:
        """Record liveness and progress (never backwards). Returns the job's
        CURRENT status — the worker's cancellation signal."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE media_jobs SET heartbeat_at = now(), "
                "progress_pct = GREATEST(progress_pct, %s) WHERE job_uid = %s RETURNING status",
                (int(progress_pct), job_uid))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else "cancelled"

    def finish(self, tenant: str, job_uid: str, status: str, **fields) -> Optional[MediaJob]:
        """Record a terminal outcome — but only for a job still ``running``: a
        job cancelled while it encoded stays cancelled, whatever the encoder
        says afterwards."""
        if status not in TERMINAL:
            raise ValueError(f"not a terminal status: {status}")
        allowed = {"output_bytes", "duration_ms", "width", "height", "encoder",
                   "rendition_name", "detail", "source_bytes"}
        sets = ["status = %s", "finished_at = now()"]
        vals: list = [status]
        if status == "succeeded":
            sets.append("progress_pct = 100")
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"unknown job field {k}")
            sets.append(f"{k} = %s")
            vals.append(v)
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE media_jobs SET {', '.join(sets)} WHERE job_uid = %s "
                        f"AND status = 'running' RETURNING {_COLS}", (*vals, job_uid))
            row = cur.fetchone()
            conn.commit()
            return _row(row) if row else None

    def requeue_stale(self, tenant: str, stale_seconds: int, max_attempts: int) -> dict:
        """Return crashed jobs to the queue — up to ``max_attempts``, after which
        they fail for good. Without the cap, a source that reliably kills the
        encoder is an infinite loop that looks exactly like a busy worker."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE media_jobs SET status = 'failed', finished_at = now(), "
                "detail = 'repeatedly crashed' WHERE status = 'running' "
                "AND heartbeat_at < now() - make_interval(secs => %s) AND attempts >= %s "
                "RETURNING job_uid", (stale_seconds, max_attempts))
            failed = [str(r[0]) for r in cur.fetchall()]
            cur.execute(
                "UPDATE media_jobs SET status = 'queued', started_at = NULL "
                "WHERE status = 'running' AND heartbeat_at < now() - make_interval(secs => %s) "
                "RETURNING job_uid", (stale_seconds,))
            requeued = [str(r[0]) for r in cur.fetchall()]
            conn.commit()
            return {"requeued": requeued, "failed": failed}

    def cancel(self, tenant: str, file_uid: str) -> List[MediaJob]:
        """Cancel every queued or running job for the file. The worker sees it
        at its next heartbeat and kills the encoder; nothing is written."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE media_jobs SET status = 'cancelled', finished_at = now(), "
                f"detail = 'cancelled' WHERE file_uid = %s AND status = ANY(%s) RETURNING {_COLS}",
                (file_uid, ["queued", "running"]))
            rows = cur.fetchall()
            conn.commit()
            return [_row(r) for r in rows]

    def for_file(self, tenant: str, file_uid: str,
                 source_version: Optional[str] = None) -> List[MediaJob]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            if source_version is None:
                cur.execute(f"SELECT {_COLS} FROM media_jobs WHERE file_uid = %s "
                            f"ORDER BY requested_at", (file_uid,))
            else:
                cur.execute(f"SELECT {_COLS} FROM media_jobs WHERE file_uid = %s AND "
                            f"source_version = %s ORDER BY requested_at",
                            (file_uid, source_version))
            rows = cur.fetchall()
            conn.commit()
            return [_row(r) for r in rows]

    def finished_publishes(self, tenant: str) -> List[MediaJob]:
        """Succeeded jobs that wrote a rendition — the reaper's candidates."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM media_jobs WHERE status = 'succeeded' "
                        f"AND rendition_name IS NOT NULL ORDER BY finished_at")
            rows = cur.fetchall()
            conn.commit()
            return [_row(r) for r in rows]

    def tenants_with_jobs(self) -> List[str]:
        """Tenants whose schema has a media_jobs table — where the worker looks.
        Discovered rather than configured, so a new tenant needs no restart."""
        from .db import connect
        with connect(self.config, readonly=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT table_schema FROM information_schema.tables "
                        "WHERE table_name = 'media_jobs' AND table_schema LIKE 'tenant\\_%%'")
            out = sorted(r[0][len("tenant_"):] for r in cur.fetchall())
            conn.commit()
            return out


# ── in memory ───────────────────────────────────────────────────────────────


class MemoryMediaJobStore:
    """The same contract in a dict — for the worker's unit tests. Time is
    injectable so stale-heartbeat recovery can be driven directly."""

    def __init__(self, clock=_now):
        self._jobs: Dict[str, Dict[str, MediaJob]] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def _t(self, tenant):
        return self._jobs.setdefault(tenant, {})

    def request(self, tenant, file_uid, source_version, profile, requested_by):
        with self._lock:
            for j in self._t(tenant).values():
                if (j.file_uid, j.source_version, j.profile) == (file_uid, source_version, profile):
                    if j.status in RESTARTABLE:
                        nj = replace(j, status="queued", requested_by=requested_by,
                                     requested_at=self.clock(), started_at=None,
                                     finished_at=None, heartbeat_at=None, attempts=0,
                                     progress_pct=0, detail=None)
                        self._t(tenant)[j.job_uid] = nj
                        return nj, True
                    return j, False
            j = MediaJob(job_uid=str(uuid.uuid4()), file_uid=file_uid,
                         source_version=source_version, profile=profile, status="queued",
                         requested_by=requested_by, requested_at=self.clock())
            self._t(tenant)[j.job_uid] = j
            return j, True

    def claim(self, tenant):
        with self._lock:
            queued = sorted((j for j in self._t(tenant).values() if j.status == "queued"),
                            key=lambda j: j.requested_at)
            if not queued:
                return None
            j = replace(queued[0], status="running", started_at=self.clock(),
                        heartbeat_at=self.clock(), attempts=queued[0].attempts + 1,
                        progress_pct=0)
            self._t(tenant)[j.job_uid] = j
            return j

    def heartbeat(self, tenant, job_uid, progress_pct):
        with self._lock:
            j = self._t(tenant).get(job_uid)
            if j is None:
                return "cancelled"
            self._t(tenant)[job_uid] = replace(j, heartbeat_at=self.clock(),
                                               progress_pct=max(j.progress_pct, int(progress_pct)))
            return j.status

    def finish(self, tenant, job_uid, status, **fields):
        if status not in TERMINAL:
            raise ValueError(f"not a terminal status: {status}")
        with self._lock:
            j = self._t(tenant).get(job_uid)
            if j is None or j.status != "running":
                return None
            extra = {"progress_pct": 100} if status == "succeeded" else {}
            nj = replace(j, status=status, finished_at=self.clock(), **extra, **fields)
            self._t(tenant)[job_uid] = nj
            return nj

    def requeue_stale(self, tenant, stale_seconds, max_attempts):
        out = {"requeued": [], "failed": []}
        with self._lock:
            cutoff = self.clock() - _dt.timedelta(seconds=stale_seconds)
            for uid, j in list(self._t(tenant).items()):
                if j.status != "running" or not j.heartbeat_at or j.heartbeat_at >= cutoff:
                    continue
                if j.attempts >= max_attempts:
                    self._t(tenant)[uid] = replace(j, status="failed", finished_at=self.clock(),
                                                   detail="repeatedly crashed")
                    out["failed"].append(uid)
                else:
                    self._t(tenant)[uid] = replace(j, status="queued", started_at=None)
                    out["requeued"].append(uid)
        return out

    def cancel(self, tenant, file_uid):
        with self._lock:
            out = []
            for uid, j in list(self._t(tenant).items()):
                if j.file_uid == file_uid and j.status in ("queued", "running"):
                    nj = replace(j, status="cancelled", finished_at=self.clock(),
                                 detail="cancelled")
                    self._t(tenant)[uid] = nj
                    out.append(nj)
            return out

    def for_file(self, tenant, file_uid, source_version=None):
        with self._lock:
            return sorted((j for j in self._t(tenant).values()
                           if j.file_uid == file_uid
                           and (source_version is None or j.source_version == source_version)),
                          key=lambda j: j.requested_at)

    def finished_publishes(self, tenant):
        with self._lock:
            return [j for j in self._t(tenant).values()
                    if j.status == "succeeded" and j.rendition_name]

    def tenants_with_jobs(self):
        return sorted(self._jobs)
