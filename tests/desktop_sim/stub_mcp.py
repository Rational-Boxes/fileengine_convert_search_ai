"""A stub MCP server (streamable HTTP) for the approval scenarios.

One harmless tool, ``record_note(text)``, that remembers every call. A scenario
asserts on ``calls``: a denied, timed-out or abandoned approval must leave it
untouched, since csai asks before the tool touches the network."""
from __future__ import annotations

import threading
import time


class StubMcp:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        import socket
        from mcp.server.fastmcp import FastMCP

        if port == 0:
            with socket.socket() as s:
                s.bind((host, 0))
                port = s.getsockname()[1]
        self.host, self.port = host, port
        self.url = f"http://{host}:{port}/mcp"
        self.calls: list = []
        mcp = FastMCP("desktop-sim", host=host, port=port)

        @mcp.tool()
        def record_note(text: str) -> str:
            """Record a short note and return a receipt."""
            self.calls.append({"text": text, "at": time.time()})
            return f"receipt-{len(self.calls)}: {text}"

        self._app = mcp.streamable_http_app()
        self._server = None
        self._thread = None

    def start(self) -> "StubMcp":
        import uvicorn
        cfg = uvicorn.Config(self._app, host=self.host, port=self.port, log_level="warning",
                             lifespan="on")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("stub MCP server did not start")
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._thread.join(timeout=5)
