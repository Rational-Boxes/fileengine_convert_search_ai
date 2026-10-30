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

"""Server-sent events over a blocking producer.

The work (core calls, model calls) is blocking, so it runs on its own thread and
hands events to a queue; the response body drains the queue as SSE frames. While
nothing arrives, a comment line goes out every few seconds so neither nginx nor
the client times out — which matters while a run waits for an MCP approval.

When the client goes away, Starlette stops consuming the body; the ``finally``
marks the run cancelled, and the producer stops at its next checkpoint (between
files, between streamed tokens), closing the provider stream so the model stops
generating — and billing — too.

The same producer serves ``Accept: application/json``: :meth:`EventRun.collect`
runs it to completion and returns every event plus the final result."""
from __future__ import annotations

import json
import queue
import threading
from typing import Callable, List, Optional, Tuple

import anyio
from starlette.responses import StreamingResponse

_END = object()

#: SSE response headers. X-Accel-Buffering stops nginx holding the stream back.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}


def frame(event: str, data) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n" \
        .encode("utf-8")


class EventRun:
    """``producer(emit, cancelled) -> dict`` on a thread; events through a queue."""

    def __init__(self, producer: Callable[[Callable[[str, dict], None], Callable[[], bool]], dict]):
        self._producer = producer
        self._q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self.result: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None

    def emit(self, event: str, data: dict) -> None:
        if not self._cancel.is_set():
            self._q.put((event, data))

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            self.result = self._producer(self.emit, self.cancelled) or {}
        except Exception as e:   # surfaced as an event, never a dead stream
            self.result = {"error": str(e)[:300]}
            self._q.put(("error", {"error": str(e)[:300], "kind": "internal"}))
        finally:
            self._q.put(_END)

    def start(self) -> "EventRun":
        # A plain daemon thread, not the request's thread pool: a cancelled
        # request must not wait for the producer to notice.
        import contextvars
        ctx = contextvars.copy_context()
        self._thread = threading.Thread(target=lambda: ctx.run(self._run), daemon=True)
        self._thread.start()
        return self

    async def sse(self, *, keepalive: float = 10.0):
        """The response body: SSE frames until the producer ends."""
        try:
            while True:
                try:
                    item = await anyio.to_thread.run_sync(
                        lambda: self._q.get(timeout=keepalive))
                except queue.Empty:
                    yield b": keep-alive\n\n"
                    continue
                if item is _END:
                    return
                yield frame(*item)
        finally:
            self.cancel()

    def collect(self) -> Tuple[List[Tuple[str, dict]], dict]:
        """Run to completion on the calling (worker) thread; every event and the
        result. For ``Accept: application/json``."""
        events: List[Tuple[str, dict]] = []
        self.result = self._producer(lambda e, d: events.append((e, d)), self.cancelled) or {}
        return events, self.result


class EventStreamResponse(StreamingResponse):
    """The SSE response for an :class:`EventRun`. The run is cancelled as soon as
    the response ends for any reason — the client hung up (the send failed, or
    the request was cancelled) or the stream finished — rather than whenever the
    abandoned body generator happens to be finalised, which can be long after the
    client left (measured: a hang-up after 2 s was noticed only when the model
    finished 20 s later)."""

    def __init__(self, run: EventRun, *, headers=None):
        super().__init__(run.sse(), media_type="text/event-stream", headers=headers)
        self._run = run

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._run.cancel()


def wants_json(accept: str) -> bool:
    """``Accept: application/json`` (and not event-stream) asks for one result."""
    a = (accept or "").lower()
    return "application/json" in a and "text/event-stream" not in a
