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

"""Per-call user-consent bridge for MCP tools (MCP_INTEGRATIONS §6).

An MCP tool's ``run`` executes in a worker thread (the sync chat generator), but the
approve/deny decision comes from the user over the async ``/chat`` WebSocket. This
:class:`ConsentBroker` bridges the two: ``request`` (called from the tool thread)
emits a ``tool_consent_request`` event and blocks on a ``threading.Event`` until the
async side calls ``resolve`` with the client's reply — or the timeout elapses, in
which case it **denies** (fail-closed). ``shutdown`` releases every waiter as denied
(used when the socket drops).

A "remember for this conversation" approval is held in ``remembered`` (a set the
caller keeps for the WebSocket connection's lifetime), so the same tool is not
re-prompted within the conversation.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, Set


class ConsentBroker:
    def __init__(self, emit: Callable[[dict], None], *, timeout_s: float,
                 remembered: Optional[Set[str]] = None):
        # emit: a thread-safe callable that pushes an event to the client (ordered
        # with the answer's token stream). timeout_s: wait bound before default-deny.
        self._emit = emit
        self._timeout = max(1.0, float(timeout_s))
        self._remembered: Set[str] = remembered if remembered is not None else set()
        self._lock = threading.Lock()
        self._pending: dict = {}   # id -> {"event", "decision", "remember"}
        self._n = 0
        self._closed = False

    # -- called from the TOOL (worker) thread -------------------------------
    def request(self, req) -> bool:
        """Block until the user approves/denies this MCP tool call. Returns True only
        on an explicit approval; timeout, denial, or a closed socket all return False."""
        tool_full = getattr(req, "tool_full", "") or ""
        if tool_full and tool_full in self._remembered:
            return True
        with self._lock:
            if self._closed:
                return False
            self._n += 1
            rid = f"consent-{self._n}"
            slot = {"event": threading.Event(), "decision": False, "remember": False}
            self._pending[rid] = slot
        self._emit({
            "type": "tool_consent_request", "id": rid,
            "integration": getattr(req, "integration", ""),
            "tool": getattr(req, "tool", ""),
            "tool_full": tool_full,
            "args_summary": getattr(req, "args_summary", ""),
        })
        got = slot["event"].wait(self._timeout)
        with self._lock:
            self._pending.pop(rid, None)
        if not got:
            return False  # timed out waiting for the user — deny
        if slot["decision"] and slot["remember"] and tool_full:
            self._remembered.add(tool_full)
        return bool(slot["decision"])

    # -- called from the ASYNC (socket) side --------------------------------
    def resolve(self, rid: str, decision: bool, remember: bool = False) -> None:
        with self._lock:
            slot = self._pending.get(rid)
            if slot is None:
                return
            slot["decision"] = bool(decision)
            slot["remember"] = bool(remember)
            ev = slot["event"]
        ev.set()

    def shutdown(self) -> None:
        """Release all waiters as denied and reject any future request (socket down)."""
        with self._lock:
            self._closed = True
            slots = list(self._pending.values())
        for slot in slots:
            slot["event"].set()  # decision left False -> deny
