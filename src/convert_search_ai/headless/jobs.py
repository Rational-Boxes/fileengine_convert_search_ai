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

"""Extraction jobs for folder automation (EXTRACTION_PROMPT_SETS.md §6.1).

folder_actions asks (``POST /v1/extract/jobs``) and returns at once; the slow
part runs here, and the outcome is an ``extraction.complete`` /
``extraction.failed`` event. folder_actions' consumer is one loop, so an
extraction run inside it would stall every other folder's events.

The queue is a Redis stream (``csai:extract:jobs``) with a consumer group, so
any number of csai instances share the work and a job left behind by a stopped
instance is re-claimed. A job runs as the principal that asked (folder
automation's service account, which must be able to READ the file) and is
metered as the tenant's automation budget (``key:auto``), not as anyone's key.

Over budget, a job waits (left pending, retried every
``extract_job_budget_retry_s``) until the budget resets, for at most
``extract_job_budget_wait_s``; then it fails with reason ``budget``. A job for
the same file version, prompt set and revision as one already done costs
nothing: the extraction cache answers it, and no second event is emitted."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Callable, Optional

from ..ldap_auth import Identity

log = logging.getLogger("convert_search_ai.headless.jobs")

STREAM = "csai:extract:jobs"
GROUP = "csai-extract"
AUTO_KEY = "auto"

#: What may enqueue: folder automation's service account holds this role.
JOB_ROLES_DEFAULT = "file_services"


def automation_identity(user: str, tenant: str, roles) -> Identity:
    """The requester, metered as the tenant's automation budget."""
    return Identity(user=user, tenant=tenant, roles=list(roles or []), authenticated=True,
                    client_key_id=AUTO_KEY, client_label="Folder automation")


class JobQueue:
    def __init__(self, config, redis_factory: Optional[Callable] = None):
        self.config = config
        self._factory = redis_factory
        self._r = None

    def _redis(self):
        if self._r is None:
            if self._factory is not None:
                self._r = self._factory()
            else:
                import redis
                c = self.config
                self._r = redis.Redis(host=c.redis_host, port=c.redis_port,
                                      password=c.redis_password or None, db=c.redis_db)
        return self._r

    def ensure_group(self) -> None:
        try:
            self._redis().xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    def enqueue(self, job: dict) -> str:
        job = dict(job)
        job.setdefault("job_id", "xj_" + uuid.uuid4().hex[:16])
        job.setdefault("enqueued_at", time.time())
        self._redis().xadd(STREAM, {"job": json.dumps(job)}, maxlen=100_000, approximate=True)
        return job["job_id"]

    @staticmethod
    def _decode(fields) -> dict:
        raw = fields.get(b"job") if b"job" in fields else fields.get("job")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def read(self, consumer: str, block_ms: int = 5000):
        resp = self._redis().xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1, block=block_ms)
        for _s, messages in resp or []:
            for msg_id, fields in messages:
                yield (msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                       self._decode(fields))

    def claim(self, consumer: str, min_idle_ms: int):
        resp = self._redis().xautoclaim(STREAM, GROUP, consumer, min_idle_time=int(min_idle_ms),
                                        start_id="0-0", count=4)
        for msg_id, fields in (resp[1] if resp and len(resp) > 1 else []):
            yield (msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                   self._decode(fields or {}))

    def ack(self, msg_id: str) -> None:
        self._redis().xack(STREAM, GROUP, msg_id)


class JobRunner:
    """Runs one job; returns True when it is finished (ack), False to retry."""

    def __init__(self, app):
        self.app = app

    def run(self, job: dict) -> bool:
        from . import promptsets as ps
        from .events import FAILED, make_event
        from .extract import ExtractRequest, Extractor
        from .models import registry as model_registry
        from .promptset_routes import service as promptset_service
        from .promptset_service import ServiceError
        from .routes import _client_for, _convert_fn, _svc

        app, config = self.app, self.app.state.config
        s = _svc(app)
        ident = automation_identity(job.get("user", ""), job.get("tenant", "default"),
                                    job.get("roles"))
        if s.meter.tokens_exhausted(ident):
            age = time.time() - float(job.get("enqueued_at") or time.time())
            if age < int(getattr(config, "extract_job_budget_wait_s", 48 * 3600)):
                return False                       # wait for the budget to reset
            s.events.emit(make_event(FAILED, tenant=ident.tenant, file_uid=job["file_uid"],
                                     version=job.get("version", ""), actor=ident.user,
                                     source="automation", prompt_set=None, model=None,
                                     reason="budget", job_id=job["job_id"]))
            return True
        try:
            info, body = promptset_service(app).resolve_for_run(
                ident, job["prompt_set"], job.get("revision"))
        except ServiceError as e:
            s.events.emit(make_event(FAILED, tenant=ident.tenant, file_uid=job["file_uid"],
                                     version=job.get("version", ""), actor=ident.user,
                                     source="automation",
                                     prompt_set={"id": job["prompt_set"],
                                                 "revision": job.get("revision")},
                                     model=None, reason=e.kind, job_id=job["job_id"]))
            return True
        models = model_registry(app)
        entry, choice = models.resolve(body.get("model"))
        req = ExtractRequest(schema=ps.compile_schema(body), files=[job["file_uid"]],
                             instructions=body["instructions"], prompt_set=info,
                             model_choice=choice, markup=bool(job.get("markup", True)),
                             markup_fields=body["fields"],
                             markup_colours=(body.get("markup") or {}).get("colours") or {},
                             source="automation", job_id=job["job_id"])
        extractor = Extractor(config, client_for=_client_for(app),
                              gate=app.state.permission_gate, store=s.store,
                              chat=models.provider(entry, app.state.chat.chat), cache=s.cache,
                              markup=s.markup, events=s.events, convert=_convert_fn(app),
                              meter=s.meter)
        from . import usage as usage_mod
        try:
            with s.meter.run(ident):
                extractor.run(ident, req, lambda ev, data: None)
        except (usage_mod.RateLimited, usage_mod.UsageUnavailable):
            return False        # the tenant's automation is at its concurrency limit
        return True


class JobWorker:
    """Background threads that drain the queue (``CSAI_EXTRACT_JOB_WORKERS``)."""

    def __init__(self, app, queue: JobQueue, runner: Optional[JobRunner] = None,
                 consumer: str = ""):
        self.app, self.queue = app, queue
        self.runner = runner or JobRunner(app)
        import socket
        self.consumer = consumer or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()

    def step(self, block_ms: int = 5000) -> int:
        """One pass: new jobs, then jobs whose retry is due. Returns how many ran."""
        cfg = self.app.state.config
        n = 0
        batches = [self.queue.read(self.consumer, block_ms=block_ms),
                   self.queue.claim(self.consumer,
                                    int(getattr(cfg, "extract_job_budget_retry_s", 900)) * 1000)]
        for batch in batches:
            for msg_id, job in batch:
                n += 1
                done = True
                if job.get("file_uid") and job.get("prompt_set"):
                    try:
                        done = self.runner.run(job)
                    except Exception:
                        log.exception("extraction job %s failed", job.get("job_id"))
                        done = True        # a crash is not retried forever
                if done:
                    self.queue.ack(msg_id)
        return n

    def start(self, threads: int = 1) -> None:
        try:
            self.queue.ensure_group()
        except Exception:
            log.exception("extraction job queue unavailable; jobs will not run")
            return
        for i in range(max(0, threads)):
            t = threading.Thread(target=self._loop, name=f"extract-jobs-{i}", daemon=True)
            t.start()
        log.info("extraction job worker started (%d thread(s))", threads)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("extraction job loop error; retrying shortly")
                self._stop.wait(5)

    def stop(self) -> None:
        self._stop.set()
