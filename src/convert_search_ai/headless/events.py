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

"""``extraction.complete`` / ``extraction.failed`` on the shared event bus
(EXTRACTION_PROMPT_SETS.md §6.0, EVENT_CONTRACT.md).

Extracted data is announced so any subsystem with an interest in it can react,
folder automation among them. The event FLAGS that values exist and NAMES the
JSON payload to read (the ``<version>-extract.json`` sidecar and the entry in
it); it never carries values, quotes or field names. A consumer reads the
sidecar with its own identity, so it sees values only for documents it may
read, and an erased document's sidecar goes with it.

Emitted once per file per newly written entry (an automation job, a headless
run with a prompt set or a raw schema, a ``cache: refresh``); not for a cache
hit (announced when written) or a builder test (stores nothing).
``extraction.failed`` is for a file that was attempted and produced no entry.

Best-effort and fail-open, like ``conversion.complete``: a Redis outage never
fails an extraction; the sidecar is the durable record."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import uuid
from typing import Callable, Optional

from .cache import SCHEMA as ENTRY_SCHEMA

log = logging.getLogger("convert_search_ai.headless.events")

COMPLETE = "extraction.complete"
FAILED = "extraction.failed"
_SCHEMA = 1
_MAXLEN = 100_000


def _ts() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S.%f")[:-3]


def make_event(etype: str, *, tenant: str, file_uid: str, version: str, actor: str,
               source: str, prompt_set: Optional[dict], model: Optional[dict],
               counts: Optional[dict] = None, payload: Optional[dict] = None,
               markup: Optional[dict] = None, reason: Optional[str] = None,
               job_id: str = "", run_id: str = "") -> dict:
    counts = dict(counts or {})
    evt = {
        "event_id": uuid.uuid4().hex, "type": etype, "schema": _SCHEMA,
        "tenant": tenant or "default", "file_uid": file_uid or "", "version": version or "",
        "actor": actor or "", "ts": _ts(), "source": source,
        "prompt_set": ({k: prompt_set.get(k) for k in ("id", "name", "revision")}
                       if prompt_set else None),
        "model": dict(model) if model else None,
        "job_id": job_id or "", "run_id": run_id or "",
    }
    if etype == COMPLETE:
        evt["values_present"] = int(counts.get("found") or 0) > 0
        evt["counts"] = {k: int(counts.get(k) or 0)
                         for k in ("fields", "found", "not_found", "dropped")}
        evt["payload"] = dict(payload or {}, schema=ENTRY_SCHEMA)
        if markup:
            evt["markup"] = {k: markup.get(k) for k in ("status", "name") if markup.get(k)}
    else:
        evt["reason"] = reason or "error"
    return evt


class ExtractionEvents:
    """Publishes onto ``config.events_stream`` (``fileengine:events``). The
    ``publish`` callable is injectable for tests."""

    def __init__(self, config=None, publish: Optional[Callable[[dict], None]] = None):
        self.config = config
        self._publish = publish
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis  # lazy
            c = self.config
            self._redis = redis.Redis(host=c.redis_host, port=c.redis_port,
                                      password=c.redis_password or None, db=c.redis_db)
        return self._redis

    def emit(self, evt: dict) -> dict:
        try:
            if self._publish is not None:
                self._publish(evt)
            else:
                self._client().xadd(self.config.events_stream, {"payload": json.dumps(evt)},
                                    maxlen=_MAXLEN, approximate=True)
        except Exception:
            log.warning("extraction event publish failed (%s for %s) — continuing",
                        evt.get("type"), evt.get("file_uid"), exc_info=True)
        return evt
