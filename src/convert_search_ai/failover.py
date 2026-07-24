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

"""Primary/replica failover primitives (see design_documents/REPLICATION_FAILOVER.md).

A lazy circuit-breaker: a failed primary connection trips it for a cooldown, during
which reads fall back to the replica; after the cooldown the next operation re-probes
the primary and resumes normal operation on success. No background threads."""
from __future__ import annotations

import time
from typing import Callable


class DegradedReadOnly(RuntimeError):
    """A write was attempted while the primary is unavailable and the service is in
    read-only fallback mode."""


class CircuitBreaker:
    """Tracks primary availability with a cooldown. ``clock`` is injectable so the
    state transitions are deterministically testable."""

    def __init__(self, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.cooldown_s = float(cooldown_s)
        self._clock = clock
        self._down_until = 0.0

    def should_try_primary(self) -> bool:
        """True when the primary should be attempted (never tripped, or the cooldown
        has elapsed so it is time to re-probe)."""
        return self._clock() >= self._down_until

    def is_degraded(self) -> bool:
        """True while inside the cooldown window (primary considered down)."""
        return self._clock() < self._down_until

    def trip(self) -> None:
        """Mark the primary down for ``cooldown_s`` from now."""
        self._down_until = self._clock() + self.cooldown_s

    def reset(self) -> None:
        """Mark the primary healthy again (a probe/op succeeded)."""
        self._down_until = 0.0
