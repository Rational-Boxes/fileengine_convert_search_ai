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

"""MCP approvals for headless runs (HEADLESS_RESEARCH_API.md §3.9).

The stream carries ``tool_consent_request`` down to the desktop application;
the answer comes back as ``POST /v1/runs/{run_id}/consent``. SSE is one-way, so
the answer may land on a different csai instance from the one whose worker is
waiting: it is delivered locally when the waiting broker is here, and otherwise
published on Redis (``csai:consent:<tenant>:<run_id>``), where the instance
running that run is subscribed.

An answer is accepted only from the same tenant, user and key that started the
run, for a request id issued to that run; ids are random and single-use. Each
pending request is recorded in Redis (``csai:consent:pending:<id>`` = its owner)
so the instance that receives the answer can check it before relaying it: an
answer from anyone else, for an id never issued, or for one already answered is
refused (404) and never reaches the broker, which keeps waiting and times out to
a denial."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("convert_search_ai.headless.consent")


@dataclass(frozen=True)
class _Owner:
    tenant: str
    user: str
    key: str
    run_id: str

    @classmethod
    def of(cls, identity, run_id: str) -> "_Owner":
        return cls(identity.tenant, identity.user, identity.client_key_id or "", run_id)


def _channel(tenant: str, run_id: str) -> str:
    return f"csai:consent:{tenant}:{run_id}"


def _pending_key(request_id: str) -> str:
    return f"csai:consent:pending:{request_id}"


def _owner_json(owner: "_Owner") -> str:
    return json.dumps({"tenant": owner.tenant, "user": owner.user, "key": owner.key,
                       "run_id": owner.run_id}, sort_keys=True)


# Delete the pending record only if it belongs to this owner: claim it, once.
_CLAIM = ("local v = redis.call('GET', KEYS[1]) "
          "if v == ARGV[1] then redis.call('DEL', KEYS[1]) return 1 end return 0")


class ConsentRelay:
    def __init__(self, redis_factory=None, pending_ttl_s: int = 600):
        self._redis_factory = redis_factory
        self._pending_ttl_s = pending_ttl_s
        self._lock = threading.Lock()
        self._pending: dict = {}     # request id -> (broker, owner)

    # --- run side
    def register(self, request_id: str, broker, identity, run_id: str,
                 ttl_s: Optional[float] = None) -> None:
        """Record a pending request. ``ttl_s`` (the approval timeout) bounds how
        long an answer can be claimed."""
        owner = _Owner.of(identity, run_id)
        with self._lock:
            self._pending[request_id] = (broker, owner)
        if self._redis_factory is not None:
            try:
                self._redis_factory().set(_pending_key(request_id), _owner_json(owner),
                                          ex=max(1, int(ttl_s or self._pending_ttl_s) + 1))
            except Exception:
                log.warning("consent relay: pending record not written; answers must "
                            "reach this instance directly", exc_info=True)

    def forget_run(self, run_id: str) -> None:
        with self._lock:
            gone = [r for r, (_, o) in self._pending.items() if o.run_id == run_id]
            for rid in gone:
                self._pending.pop(rid, None)
        self._drop_records(gone)

    def _drop_records(self, request_ids) -> None:
        if self._redis_factory is None or not request_ids:
            return
        try:
            self._redis_factory().delete(*[_pending_key(r) for r in request_ids])
        except Exception:
            log.warning("consent relay: pending records not removed", exc_info=True)

    def _deliver(self, owner: _Owner, request_id: str, decision: bool, remember: bool) -> bool:
        with self._lock:
            got = self._pending.get(request_id)
            if got is None or got[1] != owner:
                return False
            self._pending.pop(request_id, None)
            broker = got[0]
        broker.resolve(request_id, decision, remember)
        self._drop_records([request_id])
        return True

    def listen(self, identity, run_id: str) -> "_Listener":
        """Subscribe to this run's channel while it runs (a no-op without Redis)."""
        return _Listener(self, identity, run_id)

    # --- answer side
    def answer(self, identity, run_id: str, request_id: str, decision: bool,
               remember: bool) -> bool:
        """Deliver an answer. True when it reached a waiting broker here, or was
        handed to at least one instance subscribed to the run."""
        owner = _Owner.of(identity, run_id)
        with self._lock:
            local = self._pending.get(request_id)
        if local is not None:
            # Waiting here: answer directly, or refuse (wrong owner) outright.
            return self._deliver(owner, request_id, decision, remember)
        if self._redis_factory is None:
            return False
        try:
            r = self._redis_factory()
            # Claim the pending record first: only the run's owner can, once.
            if not int(r.eval(_CLAIM, 1, _pending_key(request_id), _owner_json(owner))):
                return False
            msg = json.dumps({"tenant": owner.tenant, "user": owner.user, "key": owner.key,
                              "run_id": run_id, "id": request_id,
                              "decision": bool(decision), "remember": bool(remember)})
            return int(r.publish(_channel(owner.tenant, run_id), msg)) > 0
        except Exception:
            log.warning("consent relay publish failed", exc_info=True)
            return False


class _Listener:
    def __init__(self, relay: ConsentRelay, identity, run_id: str):
        self._relay, self._identity, self._run_id = relay, identity, run_id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self):
        if self._relay._redis_factory is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._relay.forget_run(self._run_id)
        return False

    def _loop(self) -> None:
        try:
            ps = self._relay._redis_factory().pubsub(ignore_subscribe_messages=True)
            ps.subscribe(_channel(self._identity.tenant, self._run_id))
        except Exception:
            log.warning("consent relay subscribe failed; answers must reach this "
                        "instance directly", exc_info=True)
            return
        try:
            while not self._stop.is_set():
                m = ps.get_message(timeout=1.0)
                if not m or m.get("type") != "message":
                    continue
                try:
                    d = json.loads(m["data"])
                    owner = _Owner(d["tenant"], d["user"], d["key"], d["run_id"])
                    self._relay._deliver(owner, d["id"], bool(d["decision"]),
                                         bool(d["remember"]))
                except Exception:
                    continue
        finally:
            try:
                ps.close()
            except Exception:
                pass
