"""A scripted OpenAI-compatible chat model for the sandbox csai.

Deterministic, free, and observable: it answers ``POST /v1/chat/completions``
the way csai's ``openai-compatible`` provider expects (JSON mode for extraction,
streamed text and tool calls for research, a usage chunk at the end of every
stream) and records what happened, including streams the client hung up on.

Behaviour is scripted from the text it is given:

Extraction (the document between ``<<<DOCUMENT`` and ``DOCUMENT>>>``)
  Each requested field is looked up as a labelled line of the document: a
  Markdown table row ``| Fire rating | EI 60 |`` or ``Fire rating: EI 60``
  (the label is the field name with ``_`` read as a space, any case). The quote
  is that line, so csai finds it. Directives in the document change that:

  ``STUB-GARBAGE``         reply with no JSON at all (both attempts)
  ``STUB-WRONGTYPE``       numeric fields come back as text (both attempts)
  ``STUB-WRONGTYPE-ONCE``  ... on the first attempt only; the repair is right
  ``STUB-INVENT``          a value for every field the document lacks, with a
                           quote that is not in it (csai must drop them)
  ``STUB-EMPTY``           reply ``{}`` (a model that says nothing)
  ``STUB-SLEEP=<s>``       wait before replying
  ``STUB-TOKENS=<n>``      report n tokens used
  ``STUB-HTTP500``         fail the request

Research (the requester's message, i.e. user turns only)
  ``STUB-R-SLOW``          stream 400 small chunks 50 ms apart (about 20 s)
  ``STUB-R-TOOL=<part>``   call the first offered tool whose name contains
                           <part>, then answer with what it returned
  ``STUB-R-TOOL-TWICE``    with STUB-R-TOOL: call it twice, one after the other
  ``STUB-R-TOKENS=<n>``    report n tokens used
  otherwise                stream a short Markdown answer in ten chunks
  When the system prompt asks for a report (``SAVE_REPORT``), the answer is
  wrapped in the ``[[SAVE_REPORT …]] … [[/SAVE_REPORT]]`` markers csai saves.

``GET /stats`` returns the request log; ``POST /reset`` clears it."""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_DIRECTIVE = re.compile(r"STUB-([A-Z0-9]+(?:-[A-Z0-9]+)*)(?:=(\S+))?")


def directives(text: str) -> dict:
    return {m.group(1): (m.group(2) or True) for m in _DIRECTIVE.finditer(text or "")}


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def _lookup(doc: str, field: str):
    """(value text, line) for a labelled line naming ``field``, or (None, None)."""
    want = _norm(field)
    for raw in doc.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and _norm(cells[0]) == want and cells[1] and set(cells[1]) - set("-: "):
                return cells[1], line
        elif ":" in line:
            label, _, val = line.partition(":")
            if _norm(label) == want and val.strip():
                return val.strip(), line
    return None, None


def _typed(value: str, schema: dict):
    """(value, unit) shaped for the field's JSON Schema type."""
    t = (schema or {}).get("type")
    if t in ("number", "integer"):
        m = re.match(r"\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$", value)
        if m:
            n = float(m.group(1).replace(",", "."))
            return (int(n) if t == "integer" or n.is_integer() else n), (m.group(2).strip() or None)
    if t == "boolean":
        return value.strip().lower() in ("yes", "true", "1"), None
    return value, None


def extraction_reply(prompt: str, *, repair: bool) -> str:
    fields_json = prompt.split("Fields to extract (JSON Schema for each value):", 1)[-1]
    fields_json = fields_json.split("\n\n", 1)[0]
    try:
        fields = json.loads(fields_json)
    except ValueError:
        fields = {}
    doc = prompt.split("<<<DOCUMENT", 1)[-1].split("DOCUMENT>>>", 1)[0]
    d = directives(doc)
    if "GARBAGE" in d:
        return "I could not find a JSON object to give you, sorry."
    if "EMPTY" in d:
        return "{}"
    out = {}
    for name, schema in fields.items():
        text, line = _lookup(doc, name)
        if text is None:
            if "INVENT" in d:
                t = (schema or {}).get("type")
                made_up = 1234 if t in ("number", "integer") else (
                    True if t == "boolean" else "invented-" + name)
                out[name] = {"found": True, "value": made_up, "unit": None,
                             "evidence": {"quote": "this sentence is nowhere in the document"}}
            else:
                out[name] = {"found": False, "value": None, "unit": None, "evidence": None}
            continue
        value, unit = _typed(text, schema)
        wrong = "WRONGTYPE" in d or ("WRONGTYPE-ONCE" in d and not repair)
        if wrong and (schema or {}).get("type") in ("number", "integer"):
            value = "about " + text
        out[name] = {"found": True, "value": value, "unit": unit, "evidence": {"quote": line}}
    return json.dumps(out)


class StubModel:
    """The server, on a background thread. ``url`` is the base_url for csai."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.log: list = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.rstrip("/").endswith("/stats"):
                    with stub._lock:
                        body = json.dumps({"requests": list(stub.log)}).encode()
                    self._send(200, body)
                else:
                    self._send(404, b"{}")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                if self.path.rstrip("/").endswith("/reset"):
                    with stub._lock:
                        stub.log.clear()
                    return self._send(200, b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, b"{}")
                stub._complete(self, json.loads(raw or b"{}"))

            def _send(self, code: int, body: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.url = f"http://{host}:{self.port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "StubModel":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ------------------------------------------------------------------ log

    def _record(self, **rec) -> dict:
        rec.setdefault("at", time.time())
        with self._lock:
            self.log.append(rec)
        return rec

    def requests(self, kind: str | None = None) -> list:
        with self._lock:
            return [r for r in self.log if kind is None or r["kind"] == kind]

    def reset(self) -> None:
        with self._lock:
            self.log.clear()

    # ------------------------------------------------------------------ replies

    def _complete(self, h, body: dict) -> None:
        msgs = body.get("messages") or []
        users = [m.get("content") or "" for m in msgs if m.get("role") == "user"]
        first_user = users[0] if users else ""
        if not body.get("stream"):
            return self._json_reply(h, body, msgs, first_user)
        return self._stream_reply(h, body, msgs, users)

    def _json_reply(self, h, body, msgs, prompt: str) -> None:
        d = directives(prompt.split("<<<DOCUMENT", 1)[-1])
        repair = any(m.get("role") == "assistant" for m in msgs)
        rec = self._record(kind="json", repair=repair, directives=sorted(d),
                           json_mode=body.get("response_format") == {"type": "json_object"},
                           document=_document_name(prompt))
        if d.get("SLEEP"):
            time.sleep(float(d["SLEEP"]))
        if "HTTP500" in d:
            rec["status"] = 500
            return h._send(500, json.dumps({"error": {"message": "stub failure"}}).encode())
        content = extraction_reply(prompt, repair=repair)
        tokens = int(d["TOKENS"]) if d.get("TOKENS") else max(1, len(prompt) // 4)
        rec["tokens"] = tokens
        out_tokens = min(20, tokens // 2)
        h._send(200, json.dumps({
            "id": "stub", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model", "stub"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": tokens - out_tokens, "completion_tokens": out_tokens,
                      "total_tokens": tokens}}).encode())

    def _stream_reply(self, h, body, msgs, users) -> None:
        asked = users[-1] if users else ""
        system = " ".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
        report = "SAVE_REPORT" in system
        d = directives("\n".join(users))
        tools = [t["function"]["name"] for t in (body.get("tools") or [])]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        rec = self._record(kind="tools" if tools else "stream", directives=sorted(d),
                           tools=tools, tool_results=len(tool_msgs), aborted=False,
                           completed=False, chunks=0)
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True

        def send(obj) -> None:
            h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            h.wfile.flush()

        def chunk(delta, finish=None):
            return {"id": "stub", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": body.get("model", "stub"),
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            want = d.get("R-TOOL")
            target = next((t for t in tools if isinstance(want, str) and want in t), None)
            calls_wanted = 2 if "R-TOOL-TWICE" in d else 1
            if target and len(tool_msgs) < calls_wanted:
                args = json.dumps({"text": f"hello {len(tool_msgs) + 1}"})
                send(chunk({"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"call_{len(tool_msgs) + 1}", "type": "function",
                    "function": {"name": target, "arguments": ""}}]}))
                send(chunk({"tool_calls": [{"index": 0, "function": {"arguments": args}}]}))
                send(chunk({}, "tool_calls"))
                rec["tool_call"] = target
            else:
                if tool_msgs:
                    parts = [f"Tool returned: {tool_msgs[-1].get('content', '')[:200]}"]
                elif "R-SLOW" in d:
                    parts = [f"word{i} " for i in range(400)]
                else:
                    parts = ["## Stub answer\n\n", "The documents ", "were read. ",
                             "This is ", "a scripted ", "reply ", "from the ", "stub ",
                             "model for: ", asked.strip()[:80]]
                if report:
                    parts = ['[[SAVE_REPORT title="Stub report"]]\n'] + parts + ["\n[[/SAVE_REPORT]]"]
                for i, p in enumerate(parts):
                    send(chunk({"content": p}))
                    rec["chunks"] = i + 1
                    if "R-SLOW" in d:
                        time.sleep(0.05)
                send(chunk({}, "stop"))
            if (body.get("stream_options") or {}).get("include_usage"):
                tokens = int(d["R-TOKENS"]) if d.get("R-TOKENS") else 100 + rec["chunks"] * 2
                send({"id": "stub", "object": "chat.completion.chunk", "choices": [],
                      "model": body.get("model", "stub"),
                      "usage": {"prompt_tokens": tokens - 10, "completion_tokens": 10,
                                "total_tokens": tokens}})
                rec["tokens"] = tokens
            h.wfile.write(b"data: [DONE]\n\n")
            h.wfile.flush()
            rec["completed"] = True
        except (BrokenPipeError, ConnectionResetError):
            rec["aborted"] = True


def _document_name(prompt: str) -> str:
    m = re.search(r"^Document: (.+?)(?: \(part \d+ of \d+\))?$", prompt, re.M)
    return m.group(1) if m else ""


if __name__ == "__main__":   # handy for poking at it by hand
    s = StubModel(port=18199).start()
    print("stub model at", s.url)
    threading.Event().wait()
