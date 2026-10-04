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

"""The media publish worker (MEDIA_SHARE.md §4.4) — MS2.

A third entrypoint beside the app and the ingest worker
(``convert-search-ai-media-worker``). Separate on purpose: a VP9 encode takes
every core it is offered for as long as it runs, and inside the ingest worker it
would stall the event stream for every other file in the tenant and be lost on
any restart.

One job at a time per process (``CSAI_MEDIA_WORKERS`` processes for more), each:

  claim → erasure check → stream the EXACT source version to disk → encode
  (heartbeat + progress every few seconds; a cancelled job kills the encoder)
  → erasure check AGAIN → write the rendition as a hidden child → record the
  outcome → announce it.

The source version is the one the job names, never "the current one": a new
upload during a long encode must not change what the job publishes.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Callable, Optional

from . import tools
from .media_encode import MediaSettings, PROFILE_OUTPUT, encode
from .media_jobs import MediaJob

log = logging.getLogger("convert_search_ai.media_worker")

#: How often the idle loop runs the orphan reaper (§4.3.1).
REAP_EVERY_SECONDS = 3600.0
#: How often a running job writes its heartbeat (and learns of a cancel).
HEARTBEAT_SECONDS = 15.0
#: The Redis key the worker refreshes so the capability block can say whether a
#: media worker is actually running (§4.6) — otherwise discovered only by
#: waiting an hour for a job that never starts.
ALIVE_KEY = "csai:media_worker:alive"
ALIVE_TTL_SECONDS = 300

MEDIA_PUBLISHED = "media.published"
MEDIA_PUBLISH_FAILED = "media.publish_failed"


class MediaWorker:
    def __init__(self, config, jobs, mf, store, writer, *, emitter=None,
                 settings: Optional[MediaSettings] = None, encode_fn: Callable = encode,
                 clock: Callable[[], float] = time.monotonic, redis=None):
        self.config = config
        self.jobs = jobs
        self.mf = mf               # the agent client: reads any source, writes children
        self.store = store         # DocumentStore — for is_erased
        self.writer = writer       # RenditionWriter
        self.emitter = emitter
        self.settings = settings or MediaSettings.from_config(config)
        self.encode_fn = encode_fn
        self.clock = clock
        self._redis = redis
        self._stop = threading.Event()
        self.refs = ShareRefs(config)
        self._last_reap = None

    # ── the loop ────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        poll = float(getattr(self.config, "media_poll_seconds", 5.0))
        log.info("media worker started (poll %.1fs)", poll)
        while not self._stop.is_set():
            self._mark_alive()
            try:
                worked = self.run_once()
            except Exception:
                log.exception("media worker cycle failed — continuing")
                worked = False
            if not worked:
                self._maybe_reap()
                self._stop.wait(poll)

    def run_once(self) -> bool:
        """Recover crashed jobs, then run at most ONE job. True if one ran."""
        try:
            tenants = self.jobs.tenants_with_jobs()
        except Exception:
            log.exception("could not list tenants with media jobs")
            return False
        for tenant in tenants:
            try:
                rec = self.jobs.requeue_stale(tenant, self.config.media_stale_seconds,
                                              self.config.media_max_attempts)
                for uid in rec.get("failed", []):
                    log.error("media job %s in %s gave up after %d attempts",
                              uid, tenant, self.config.media_max_attempts)
                    self._audit(tenant, "media_publish_abandoned", "-", job_uid=uid,
                                attempts=self.config.media_max_attempts)
            except Exception:
                log.exception("stale-job recovery failed for %s", tenant)
        for tenant in tenants:
            job = self.jobs.claim(tenant)
            if job is not None:
                self.process(tenant, job)
                return True
        return False

    def _maybe_reap(self) -> None:
        now = self.clock()
        if self._last_reap is not None and now - self._last_reap < REAP_EVERY_SECONDS:
            return
        self._last_reap = now
        try:
            gone = reap_orphans(self.config, self.jobs, self.mf, self.refs)
            if gone:
                log.info("reaped %d orphaned published rendition(s): %s", len(gone), gone)
        except Exception:
            log.exception("orphan reaper failed — keeping everything")

    # ── one job ─────────────────────────────────────────────────────────────

    def process(self, tenant: str, job: MediaJob) -> str:
        """Run one claimed job to a terminal state. Returns that state."""
        log.info("media job %s: %s %s@%s (attempt %d)", job.job_uid, job.profile,
                 job.file_uid, job.source_version, job.attempts)
        if self.store.is_erased(tenant, job.file_uid):
            return self._end(tenant, job, "cancelled", detail="the file was erased")
        try:
            with tools.workdir() as wd:
                src = os.path.join(wd, "source")
                fetched = self._fetch(tenant, job, src)
                if fetched is not None:
                    return fetched
                return self._encode_and_write(tenant, job, src)
        except Exception:
            log.exception("media job %s crashed", job.job_uid)
            return self._end(tenant, job, "failed", detail="internal error while publishing")

    def _fetch(self, tenant: str, job: MediaJob, path: str) -> Optional[str]:
        """Stream the job's source version to ``path``. Returns a terminal state
        if the job cannot proceed, else None."""
        limit = int(getattr(self.config, "media_max_input_bytes", 0) or 0)
        written = 0
        try:
            with open(path, "wb") as out:
                for chunk in self.mf.get_stream(job.file_uid, version=job.source_version,
                                                tenant=tenant):
                    written += len(chunk)
                    if limit and written > limit:
                        return self._end(tenant, job, "failed",
                                         detail=f"the source exceeds the {limit}-byte limit")
                    out.write(chunk)
        except Exception as e:
            from ._client import NotFoundError
            if isinstance(e, NotFoundError):
                return self._end(tenant, job, "failed",
                                 detail="the source version no longer exists")
            raise
        if written == 0:
            return self._end(tenant, job, "failed", detail="the source is empty")
        return None

    def _encode_and_write(self, tenant: str, job: MediaJob, src: str) -> str:
        state = {"last_beat": self.clock(), "status": "running", "pct": 0}

        def beat(force: bool = False) -> None:
            now = self.clock()
            if force or now - state["last_beat"] >= HEARTBEAT_SECONDS:
                state["last_beat"] = now
                state["status"] = self.jobs.heartbeat(tenant, job.job_uid, state["pct"])

        def on_progress(pct: int) -> None:
            state["pct"] = pct
            beat()

        def should_cancel() -> bool:
            beat()
            return state["status"] == "cancelled"

        result = self.encode_fn(job.profile, src, self.settings,
                                on_progress=on_progress, should_cancel=should_cancel)
        try:
            if result.status == "cancelled" or state["status"] == "cancelled":
                return "cancelled"          # cancel() already recorded it
            if result.status == "skipped":
                return self._end(tenant, job, "skipped", detail=result.detail)
            if result.status != "ok":
                return self._end(tenant, job, "failed", detail=result.detail or "encode failed")

            # The erasure race is far wider than the ingest path's: an encode can
            # run for hours. Checked again immediately before anything is written.
            if self.store.is_erased(tenant, job.file_uid):
                return self._end(tenant, job, "cancelled", detail="the file was erased")
            beat(force=True)
            if state["status"] == "cancelled":
                return "cancelled"

            self._clear_empty_child(tenant, job, result.rendition.ext)
            written = self.writer.write(job.file_uid, job.source_version,
                                        [result.rendition], tenant)
            name = written[0] if written else None
            if name is None:
                # The writer skips a name that already exists — a retry after a
                # crash between write and finish. The rendition is there.
                fmt, _e, _m = PROFILE_OUTPUT[job.profile]
                from .renditions import rendition_name
                name = rendition_name(job.source_version, fmt, result.rendition.ext)
            end = self._end(tenant, job, "succeeded", rendition_name=name,
                            output_bytes=result.output_bytes, duration_ms=result.duration_ms,
                            width=result.width or None, height=result.height or None,
                            encoder=result.encoder)
            return end
        finally:
            if result.rendition is not None:
                result.rendition.release()

    def _clear_empty_child(self, tenant: str, job: MediaJob, ext: str) -> None:
        """Remove a same-named child that holds no content.

        The writer creates a child with ``touch`` and then streams into it, and a
        touch alone leaves no version. A worker killed between the two leaves an
        empty child, and the writer — which skips names that exist — would let
        the retry report success while writing nothing. An hours-long encode makes
        that window real, so it is closed here rather than trusted."""
        from .renditions import rendition_name
        fmt, _e, _m = PROFILE_OUTPUT[job.profile]
        name = rendition_name(job.source_version, fmt, ext)
        for e in self.mf.dir(job.file_uid, tenant=tenant) or []:
            if getattr(e, "name", "") != name:
                continue
            try:
                size = int(getattr(self.mf.stat(e.uid, tenant=tenant), "size", 0) or 0)
            except Exception:
                # Could not ask — so do NOT delete. Only a child KNOWN to be empty
                # is removed; a transient error must never cost a good rendition.
                continue
            if size == 0:
                log.warning("removing an empty %s left by an interrupted write", name)
                self.mf.remove(e.uid, tenant=tenant)

    # ── outcomes ────────────────────────────────────────────────────────────

    def _end(self, tenant: str, job: MediaJob, status: str, **fields) -> str:
        done = self.jobs.finish(tenant, job.job_uid, status, **fields)
        if done is None:
            # Not running any more — cancelled under us. Its record stands.
            return "cancelled"
        try:
            self._retire_superseded(tenant, done)
        except Exception:
            log.warning("could not retire superseded copies of %s — keeping them",
                        done.file_uid, exc_info=True)
        if status == "succeeded":
            log.info("media job %s succeeded: %s (%s bytes, %s)", job.job_uid,
                     done.rendition_name, done.output_bytes, done.encoder)
            self._announce(MEDIA_PUBLISHED, tenant, done)
            self._audit(tenant, "media_published", done.requested_by, job_uid=done.job_uid,
                        file_uid=done.file_uid, profile=done.profile,
                        rendition=done.rendition_name, output_bytes=done.output_bytes,
                        duration_ms=done.duration_ms, encoder=done.encoder)
        elif status == "failed":
            log.warning("media job %s failed: %s", job.job_uid, done.detail)
            self._announce(MEDIA_PUBLISH_FAILED, tenant, done)
            self._audit(tenant, "media_publish_failed", done.requested_by,
                        job_uid=done.job_uid, file_uid=done.file_uid, profile=done.profile,
                        detail=done.detail, attempts=done.attempts)
        return status

    #: The rendition a version must have published before links move to it.
    _PRIMARY = ("video-720p-vp9", "audio-mp3")

    def _retire_superseded(self, tenant: str, job: MediaJob) -> None:
        """A share link plays the NEWEST PUBLISHED version (decided 2026-10-03,
        superseding §6.2's pinning): a new upload is a correction the outside
        viewer should see. Once every job for this version has finished and its
        primary rendition succeeded, the published copies of OLDER versions are
        removed — all of them, so a size the new version lacks (480p of a source
        that is already SD) is not left on offer with stale footage.

        Until then the old copies keep playing: a correction never takes a link
        dark. A failed publish retires nothing. An older version finishing late
        never touches a newer one."""
        from .renditions import PUBLISHED_FMTS, _safe_version, parse_rendition_name
        mine = self.jobs.for_file(tenant, job.file_uid, job.source_version)
        if not mine or any(j.status in ("queued", "running") for j in mine):
            return
        if not any(j.profile in self._PRIMARY and j.status == "succeeded" for j in mine):
            return
        current = _safe_version(job.source_version)
        retired = []
        for e in self.mf.dir(job.file_uid, tenant=tenant) or []:
            parsed = parse_rendition_name(getattr(e, "name", ""))
            if not parsed:
                continue
            version, fmt, _ext = parsed
            if fmt in PUBLISHED_FMTS and version < current:
                self.mf.remove(e.uid, tenant=tenant)
                retired.append(e.name)
        if retired:
            log.info("links on %s now play %s; retired %s", job.file_uid,
                     job.source_version, ", ".join(sorted(retired)))
            # What outside viewers see changed: on the record, with who caused it.
            self._audit(tenant, "media_link_retargeted", job.requested_by,
                        file_uid=job.file_uid, version=job.source_version,
                        retired=",".join(sorted(retired)))

    def _announce(self, etype: str, tenant: str, job: MediaJob) -> None:
        """media.published / media.publish_failed on the core events stream,
        siblings of conversion.complete (§4.8). Best-effort."""
        if self.emitter is None:
            return
        try:
            self.emitter.publish(
                etype, tenant=tenant, file_uid=job.file_uid, version=job.source_version,
                actor=job.requested_by,
                renditions=[job.rendition_name] if job.rendition_name else [],
                reason=job.detail if etype == MEDIA_PUBLISH_FAILED else None)
        except Exception:
            log.warning("could not announce %s for %s", etype, job.file_uid, exc_info=True)

    def _audit(self, tenant: str, action: str, user: str, **extra) -> None:
        try:
            from . import audit
            audit.record(action=action, user=user or "-", tenant=tenant,
                         result=("success" if action in ("media_published",
                                                         "media_link_retargeted")
                                 else "failure"),
                         **extra)
        except Exception:
            log.warning("could not audit %s", action, exc_info=True)

    def _mark_alive(self) -> None:
        try:
            r = self._redis or _redis_client(self.config)
            self._redis = r
            r.set(ALIVE_KEY, "1", ex=ALIVE_TTL_SECONDS)
        except Exception:
            log.debug("could not refresh the media worker liveness key", exc_info=True)


# ── the orphan reaper (§4.3.1) ──────────────────────────────────────────────


class ShareRefs:
    """share_service's ``GET /share/v1/internal/media-refs/{uid}``: which
    published renditions of a file a LIVE media link still points at.

    Returns None whenever it cannot answer — unset URL, network error, a status
    other than 200, a body it does not understand. None means KEEP: deleting
    content because a sibling service was briefly unreachable is the wrong
    direction to be wrong in."""

    def __init__(self, config, http=None):
        self.url = (getattr(config, "media_refs_url", "") or "").rstrip("/")
        self.secret = getattr(config, "internal_secret", "") or ""
        self.http = http

    def live_renditions(self, tenant: str, file_uid: str):
        if not self.url:
            return None
        try:
            import httpx
            client = self.http or httpx
            r = client.get(f"{self.url}/{file_uid}", timeout=10,
                           headers={"X-Tenant": tenant, "X-Internal-Secret": self.secret})
            if r.status_code != 200:
                return None
            names = (r.json() or {}).get("renditions")
            return set(names) if isinstance(names, list) else None
        except Exception:
            return None


def reap_orphans(config, jobs, mf, refs: ShareRefs, *, now=None) -> list:
    """Remove published renditions no live link has needed for
    ``media_orphan_days`` (§4.3.1). Returns the names removed.

    Only renditions this feature produced (succeeded jobs) are candidates, and
    only after the grace period — re-sharing last week's intro video must be
    instant, not a re-encode. ``poster`` and ``preview`` are never touched: they
    belong to the file browser and have no job. Any doubt keeps the file."""
    import datetime as _dt
    now = now or _dt.datetime.now(_dt.timezone.utc)
    grace = _dt.timedelta(days=int(getattr(config, "media_orphan_days", 30)))
    removed = []
    for tenant in jobs.tenants_with_jobs():
        by_file: dict = {}
        for job in jobs.finished_publishes(tenant):
            by_file.setdefault(job.file_uid, []).append(job)
        for file_uid, done in by_file.items():
            live = refs.live_renditions(tenant, file_uid)
            if live is None:
                continue                       # cannot ask → keep everything
            for job in done:
                if not job.rendition_name or job.rendition_name in live:
                    continue
                if not job.finished_at or now - job.finished_at < grace:
                    continue
                for e in mf.dir(file_uid, tenant=tenant) or []:
                    if getattr(e, "name", "") == job.rendition_name:
                        mf.remove(e.uid, tenant=tenant)
                        removed.append(job.rendition_name)
                        try:
                            from . import audit
                            audit.record(action="media_rendition_reaped", user="csai",
                                         tenant=tenant, result="success",
                                         file_uid=file_uid, rendition=job.rendition_name)
                        except Exception:
                            pass
    return removed


def _redis_client(config):
    import redis
    return redis.Redis(host=config.redis_host, port=config.redis_port,
                       password=config.redis_password or None, db=config.redis_db)


def worker_alive(config) -> bool:
    """Has a media worker refreshed its liveness key recently? For §4.6's
    capability block. False on any error: an unreachable Redis cannot vouch."""
    try:
        return bool(_redis_client(config).exists(ALIVE_KEY))
    except Exception:
        return False


def main() -> None:
    """``convert-search-ai-media-worker`` / ``python -m convert_search_ai.media_worker``."""
    from .config import Config, load_dotenv
    from .core_client import agent_client
    from .emit import EventEmitter
    from .media_jobs import PostgresMediaJobStore
    from .renditions import RenditionWriter
    from .store import DocumentStore

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()     # as the ingest worker does: run from the service dir, read ./.env
    config = Config()
    if not getattr(config, "media_enabled", True):
        log.warning("CSAI_MEDIA_ENABLED is false — the media worker has nothing to do; exiting")
        return
    mf = agent_client(config)
    worker = MediaWorker(config, PostgresMediaJobStore(config), mf, DocumentStore(config),
                         RenditionWriter(mf), emitter=EventEmitter(config))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop())
    worker.run_forever()


if __name__ == "__main__":
    main()
