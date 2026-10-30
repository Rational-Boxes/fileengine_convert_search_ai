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

"""Per-API-key limits and the usage status a desktop application shows (§3.8).

Four limits per key: requests per minute, model tokens per UTC day, concurrent
runs, files per extraction. Each is ``ok``, ``warning`` (at or above the warn
ratio) or ``exceeded``; the worst of them is the overall state. The same status
object goes out as response headers, as SSE ``usage`` events and from
``GET /v1/usage``, with a plain-language ``message`` the application can show.

Counters live in Redis so every csai instance shares them. If Redis cannot be
reached the request is refused (:class:`UsageUnavailable` -> 503) rather than let
through uncounted: the limits exist to bound cost, and headless use is optional.

A session minted from a generated key is counted by its key id; any other
session (a person's own sign-in using these routes) by its user."""
from __future__ import annotations

import contextlib
import datetime as _dt
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


class UsageUnavailable(Exception):
    """The counters could not be read or written."""


class RateLimited(Exception):
    """A limit refuses the request. ``status`` is the usage object to return."""

    def __init__(self, status: dict, limit: str):
        super().__init__(limit)
        self.status = status
        self.limit = limit


@dataclass(frozen=True)
class Limits:
    requests_per_minute: int
    tokens_per_day: int
    concurrent_runs: int
    files_per_run: int
    warn_ratio: float = 0.8

    @classmethod
    def from_config(cls, config) -> "Limits":
        return cls(
            requests_per_minute=int(getattr(config, "headless_requests_per_minute", 60)),
            tokens_per_day=int(getattr(config, "headless_tokens_per_day", 2_000_000)),
            concurrent_runs=int(getattr(config, "headless_concurrent_runs", 2)),
            files_per_run=int(getattr(config, "headless_files_per_run", 200)),
            warn_ratio=float(getattr(config, "headless_warn_ratio", 0.8)))


# ------------------------------------------------------------------ backends

class MemoryBackend:
    """In-process counters, for tests and a single-instance dev run."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._data: dict = {}          # key -> (value, expires_at)
        self._lock = threading.Lock()

    def _live(self, key):
        v = self._data.get(key)
        if v is None or v[1] <= self._clock():
            self._data.pop(key, None)
            return 0
        return v[0]

    def get(self, key: str) -> int:
        with self._lock:
            return self._live(key)

    def incrby(self, key: str, n: int, ttl: int) -> int:
        with self._lock:
            cur = self._live(key) + int(n)
            exp = self._data.get(key, (0, self._clock() + ttl))[1]
            self._data[key] = (cur, exp)
            return cur

    def decr(self, key: str) -> int:
        with self._lock:
            cur = max(0, self._live(key) - 1)
            if key in self._data:
                self._data[key] = (cur, self._data[key][1])
            return cur


class RedisBackend:
    """Counters in Redis. Every error becomes :class:`UsageUnavailable`."""

    def __init__(self, client):
        self._r = client

    def get(self, key: str) -> int:
        try:
            v = self._r.get(key)
        except Exception as e:
            raise UsageUnavailable(str(e)) from e
        return int(v or 0)

    def incrby(self, key: str, n: int, ttl: int) -> int:
        try:
            # SET NX EX creates the key with its window's TTL only if absent, then
            # INCRBY counts; works on every Redis version (EXPIRE NX needs 7.0).
            pipe = self._r.pipeline()
            pipe.set(key, 0, ex=int(ttl), nx=True)
            pipe.incrby(key, int(n))
            out = pipe.execute()
        except Exception as e:
            raise UsageUnavailable(str(e)) from e
        return int(out[1])

    def decr(self, key: str) -> int:
        try:
            return max(0, int(self._r.decr(key)))
        except Exception as e:
            raise UsageUnavailable(str(e)) from e


def redis_backend(config) -> RedisBackend:
    import redis
    return RedisBackend(redis.Redis(host=config.redis_host, port=config.redis_port,
                                    password=config.redis_password or None,
                                    db=config.redis_db, socket_timeout=2,
                                    socket_connect_timeout=2))


# ------------------------------------------------------------------ meter

_RUN_TTL = 3600   # a crashed run's concurrency slot frees itself within the hour


class UsageMeter:
    def __init__(self, limits: Limits, backend, clock: Callable[[], float] = time.time):
        self.limits = limits
        self.backend = backend
        self._clock = clock

    # --- keys
    @staticmethod
    def subject(identity) -> str:
        key = getattr(identity, "client_key_id", "") or ""
        return f"key:{key}" if key else f"user:{identity.user}"

    def _k(self, identity, what: str) -> str:
        return f"csai:usage:{identity.tenant}:{self.subject(identity)}:{what}"

    def _now(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self._clock(), tz=_dt.timezone.utc)

    def _minute(self):
        n = self._now().replace(second=0, microsecond=0)
        return n.strftime("%Y%m%d%H%M"), n + _dt.timedelta(minutes=1)

    def _day(self):
        n = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
        return n.strftime("%Y%m%d"), n + _dt.timedelta(days=1)

    # --- status
    def _state(self, used: int, limit: int) -> str:
        if limit <= 0:
            return "ok"
        if used >= limit:
            return "exceeded"
        if used >= limit * self.limits.warn_ratio:
            return "warning"
        return "ok"

    def status(self, identity) -> dict:
        minute, minute_reset = self._minute()
        day, day_reset = self._day()
        req = self.backend.get(self._k(identity, f"req:{minute}"))
        tok = self.backend.get(self._k(identity, f"tok:{day}"))
        conc = self.backend.get(self._k(identity, "runs"))
        hits = self.backend.get(self._k(identity, f"hits:{day}"))
        saved = self.backend.get(self._k(identity, f"saved:{day}"))
        L = self.limits
        limits = {
            "requests": {"used": req, "limit": L.requests_per_minute,
                         "resets_at": minute_reset.isoformat(),
                         "state": self._state(req, L.requests_per_minute)},
            "tokens": {"used": tok, "limit": L.tokens_per_day,
                       "resets_at": day_reset.isoformat(),
                       "state": self._state(tok, L.tokens_per_day)},
            "concurrent": {"used": conc, "limit": L.concurrent_runs,
                           "state": self._state(conc, L.concurrent_runs)},
            "files_per_run": {"limit": L.files_per_run},
        }
        order = {"ok": 0, "warning": 1, "exceeded": 2}
        state = max((v.get("state", "ok") for v in limits.values()), key=lambda s: order[s])
        return {"state": state, "subject": "key" if identity.client_key_id else "user",
                "limits": limits,
                "cache": {"hits": hits, "tokens_saved_estimate": saved},
                "message": _message(limits, state)}

    # --- admission
    def admit(self, identity, *, spends_tokens: bool) -> dict:
        """Count one request. Refused when the minute's requests are used up or,
        for a route that calls the model, the day's tokens are."""
        minute, _ = self._minute()
        n = self.backend.incrby(self._k(identity, f"req:{minute}"), 1, ttl=120)
        st = self.status(identity)
        if n > self.limits.requests_per_minute > 0:
            raise RateLimited(st, "requests")
        if spends_tokens and st["limits"]["tokens"]["state"] == "exceeded":
            raise RateLimited(st, "tokens")
        return st

    @contextlib.contextmanager
    def run(self, identity):
        """Hold a concurrency slot for the life of a run."""
        key = self._k(identity, "runs")
        n = self.backend.incrby(key, 1, ttl=_RUN_TTL)
        if n > self.limits.concurrent_runs > 0:
            self.backend.decr(key)
            raise RateLimited(self.status(identity), "concurrent")
        try:
            yield
        finally:
            try:
                self.backend.decr(key)
            except UsageUnavailable:
                pass          # the slot expires with its TTL

    def add_tokens(self, identity, n: int) -> None:
        if n > 0:
            day, _ = self._day()
            self.backend.incrby(self._k(identity, f"tok:{day}"), n, ttl=2 * 86400)

    def tokens_exhausted(self, identity) -> bool:
        day, _ = self._day()
        lim = self.limits.tokens_per_day
        return lim > 0 and self.backend.get(self._k(identity, f"tok:{day}")) >= lim

    def add_cache_hit(self, identity, tokens_saved: int) -> None:
        day, _ = self._day()
        self.backend.incrby(self._k(identity, f"hits:{day}"), 1, ttl=2 * 86400)
        if tokens_saved > 0:
            self.backend.incrby(self._k(identity, f"saved:{day}"), tokens_saved, ttl=2 * 86400)


def _message(limits: dict, state: str) -> str:
    if state == "ok":
        return ""
    parts = []
    t, r, c = limits["tokens"], limits["requests"], limits["concurrent"]
    if t["state"] == "exceeded":
        parts.append("This key has used today's token budget; extraction resumes after "
                     f"{t['resets_at']}.")
    elif t["state"] == "warning":
        parts.append(f"This key has used {round(100 * t['used'] / t['limit'])}% of today's "
                     "token budget.")
    if r["state"] == "exceeded":
        parts.append("Too many requests this minute; wait a moment.")
    elif r["state"] == "warning":
        parts.append("This key is close to its per-minute request limit.")
    if c["state"] == "exceeded":
        parts.append("This key already has the maximum number of runs in progress.")
    return " ".join(parts)


def headers(status: dict) -> dict:
    """Response headers carrying the status (IETF RateLimit draft for requests)."""
    r = status["limits"]["requests"]
    remaining = max(0, int(r["limit"]) - int(r["used"]))
    try:
        reset = max(0, int((_dt.datetime.fromisoformat(r["resets_at"])
                            - _dt.datetime.now(_dt.timezone.utc)).total_seconds()))
    except (ValueError, TypeError):
        reset = 60
    return {
        "RateLimit-Policy": f'"requests";q={r["limit"]};w=60',
        "RateLimit": f'"requests";r={remaining};t={reset}',
        "X-Usage-State": status["state"],
    }
