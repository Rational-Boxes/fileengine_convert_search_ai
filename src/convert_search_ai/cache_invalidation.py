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

"""Real-time permission-cache invalidation from the file-activity event stream.

The ``PermissionGate`` TTL bounds staleness to ≤ 5 min, but governance changes
should take effect immediately where possible. This consumer subscribes to the
core publisher's events (its **own** consumer group, independent of the ingest
worker — EVENT_CONTRACT §1) and evicts affected cache entries as soon as a change
is published:

  acl.changed                          → invalidate the resource (file_uid)
  role.assigned / role.member_removed  → invalidate the member across the tenant
  role.deleted                         → invalidate the whole tenant (members unknown)
  file.deleted                         → invalidate the resource (stale allow on a gone file)

Runs as a daemon thread alongside the search API. If Redis is unreachable the
thread exits and the TTL remains the only staleness bound (degrade, don't crash)."""
from __future__ import annotations

import logging
import threading

from .config import Config
from .permissions import PermissionGate

log = logging.getLogger("convert_search_ai.permcache")


class PermissionCacheInvalidator:
    def __init__(self, config: Config, gate: PermissionGate, source=None):
        self.config = config
        self.gate = gate
        self.source = source

    def _ensure_source(self):
        if self.source is None:
            from .events import RedisEventSource
            self.source = RedisEventSource(
                self.config, consumer_name="permcache-1",
                group=self.config.events_group + "-permcache",
            )
        return self.source

    def handle(self, event: dict) -> None:
        etype = event.get("type", "")
        tenant = event.get("tenant") or "default"
        if etype == "acl.changed":
            uid = event.get("file_uid")
            if uid:
                self.gate.invalidate_resource(tenant, uid)
        elif etype in ("role.assigned", "role.member_removed"):
            member = event.get("member")
            if member:
                self.gate.invalidate_member(tenant, member)
        elif etype == "role.deleted":
            self.gate.invalidate_tenant(tenant)
        elif etype == "file.deleted":
            uid = event.get("file_uid")
            if uid:
                self.gate.invalidate_resource(tenant, uid)

    def run_once(self, count: int = 64, block_ms: int = 5000) -> int:
        src = self._ensure_source()
        batch = src.read(count=count, block_ms=block_ms)
        for msg_id, event in batch:
            try:
                self.handle(event)
            except Exception:
                log.exception("permcache: failed handling %s", msg_id)
            finally:
                src.ack([msg_id])
        return len(batch)

    def run_forever(self) -> None:
        src = self._ensure_source()
        src.ensure_group()
        log.info("permission-cache invalidator started; stream=%s group=%s",
                 src.stream, src.group)
        while True:
            self.run_once()

    def start_background(self) -> threading.Thread:
        """Run ``run_forever`` in a daemon thread. Best-effort: if events are
        unavailable the thread logs and exits, leaving the TTL as the bound."""
        def _run():
            try:
                self.run_forever()
            except Exception:
                log.warning("permission-cache invalidator stopped (events unavailable); "
                            "TTL still bounds staleness", exc_info=True)

        th = threading.Thread(target=_run, name="permcache-invalidator", daemon=True)
        th.start()
        return th
