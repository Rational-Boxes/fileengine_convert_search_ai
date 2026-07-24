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

"""Permission gating with a short-lived, fail-closed cache (DEVELOPMENT_PLAN §8).

Every search hit and text request is gated by the **requesting user's** FileEngine
READ permission, evaluated by the core's CheckPermission (never the agent's).
Decisions are cached per ``(tenant, user, file_uid)`` for at most
``permission_cache_ttl`` seconds (≤ 5 min) and the cache is **fail-closed**: any
error or unreachable core means *not permitted*.

The governance events from the core publisher (acl.changed / role.*) can evict
entries early via ``invalidate_*`` — tighter than the TTL — if the search process
also subscribes to them."""
from __future__ import annotations

import threading
import time
from typing import List


class PermissionGate:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._cache: dict = {}     # (tenant, user, file_uid) -> (allowed, expiry)
        self._lock = threading.Lock()

    def can_read(self, mf, identity, file_uid: str) -> bool:
        """READ check for ``identity`` on ``file_uid``, using the user-bound gRPC
        client ``mf``. Cached and fail-closed."""
        key = (identity.tenant, identity.user, file_uid)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
        allowed = self._check(mf, identity, file_uid)
        with self._lock:
            self._cache[key] = (allowed, now + self.ttl)
        return allowed

    def _check(self, mf, identity, file_uid: str) -> bool:
        try:
            return bool(mf.check_permission(file_uid, "r", tenant=identity.tenant))
        except Exception:
            return False  # fail-closed

    def filter_readable(self, mf, identity, file_uids: List[str]) -> List[str]:
        """Keep only the uids the identity may read (order preserved)."""
        return [u for u in file_uids if self.can_read(mf, identity, u)]

    # --- event-driven invalidation (acl.changed / role.*) ---
    def invalidate_resource(self, tenant: str, file_uid: str) -> None:
        with self._lock:
            for k in [k for k in self._cache if k[0] == tenant and k[2] == file_uid]:
                del self._cache[k]

    def invalidate_member(self, tenant: str, user: str) -> None:
        with self._lock:
            for k in [k for k in self._cache if k[0] == tenant and k[1] == user]:
                del self._cache[k]

    def invalidate_tenant(self, tenant: str) -> None:
        """Drop every cached decision for a tenant (e.g. on role.deleted, which
        changes effective access for an unknown set of members)."""
        with self._lock:
            for k in [k for k in self._cache if k[0] == tenant]:
                del self._cache[k]

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
