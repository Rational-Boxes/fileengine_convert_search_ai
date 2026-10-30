"""The simulated desktop application, and the dev stack it talks to.

``Stack`` is the person's side: their signed-in session (the web app's bearer),
minting and revoking generated keys, and the REST door for fixtures.

``DesktopApp`` is what a desktop client does with a key:secret and nothing
else:

* signs in at the REST door (``POST /api/v1/auth/token``, Basic key:secret),
  refreshes before the session expires and once on a 401, and signs out;
* reads SSE streams incrementally, as a UI would (time to first event,
  keep-alive comments, events in order), and can hang up mid-stream (cancel);
* answers ``tool_consent_request`` events through a policy, as its approval
  dialog would, optionally through another csai instance;
* watches the usage status in every response header and ``usage`` event and
  raises warnings, as its status bar would; backs off on 429 per Retry-After;
* uploads and downloads files through the REST door (reports are downloaded,
  never streamed as the deliverable).
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx


class SignedOut(Exception):
    """The session is gone and cannot be refreshed (key revoked, signed out)."""


# --------------------------------------------------------------------------- stack

class Stack:
    def __init__(self, *, rest: str, idm: str, csai: str, tenant: str, user_token: str,
                 timeout: float = 300):
        self.rest, self.idm, self.csai, self.tenant = rest, idm, csai, tenant
        self.user_token = user_token
        self.http = httpx.Client(timeout=timeout)

    @property
    def user(self) -> dict:
        return {"Authorization": f"Bearer {self.user_token}", "X-Tenant": self.tenant}

    # --- generated keys (what the person does in their profile)
    def mint_key(self, label: str, scopes=("api",)) -> tuple[str, str]:
        r = self.http.post(f"{self.idm}/v1/me/service-credentials", headers=self.user,
                           json={"label": label, "scopes": list(scopes)})
        r.raise_for_status()
        c = r.json()
        return c["key_id"], c["secret"]

    def revoke_key(self, key_id: str) -> int:
        return self.http.delete(f"{self.idm}/v1/me/service-credentials/{key_id}",
                                headers=self.user).status_code

    # --- REST door with the person's session (fixtures, cleanup)
    def mkdir(self, name: str, parent: str = "root", headers: dict | None = None) -> str:
        r = self.http.post(f"{self.rest}/v1/dirs/{parent}", headers=headers or self.user,
                           json={"name": name})
        r.raise_for_status()
        return r.json()["uid"]

    def put_file(self, folder: str, name: str, data: bytes, headers: dict | None = None) -> str:
        h = headers or self.user
        r = self.http.post(f"{self.rest}/v1/dirs/{folder}/files", headers=h, json={"name": name})
        r.raise_for_status()
        uid = r.json()["uid"]
        self.put_version(uid, data, headers=h)
        return uid

    def put_version(self, uid: str, data: bytes, headers: dict | None = None) -> None:
        r = self.http.put(f"{self.rest}/v1/files/{uid}/content",
                          headers={**(headers or self.user),
                                   "Content-Type": "application/octet-stream"}, content=data)
        r.raise_for_status()

    def rmdir(self, uid: str) -> None:
        self.http.delete(f"{self.rest}/v1/dirs/{uid}", headers=self.user)

    def renditions(self, uid: str) -> list:
        r = self.http.get(f"{self.rest}/v1/files/{uid}/renditions", headers=self.user)
        if r.status_code != 200:
            return []
        body = r.json()
        if isinstance(body, dict):
            return body.get("entries") or body.get("renditions") or []
        return body

    def wait_indexed(self, uids, *, timeout: float = 180, csai: str | None = None) -> dict:
        """{uid: text} once csai has text for every uid (the ingest worker ran)."""
        base, left, out = csai or self.csai, set(uids), {}
        deadline = time.time() + timeout
        while left and time.time() < deadline:
            for uid in list(left):
                r = self.http.get(f"{base}/documents/{uid}/text", headers=self.user)
                if r.status_code == 200 and (r.json().get("text") or "").strip():
                    out[uid] = r.json()["text"]
                    left.discard(uid)
            if left:
                time.sleep(2)
        if left:
            raise TimeoutError(f"not indexed after {timeout:.0f}s: {sorted(left)}")
        return out


def docx_text(data: bytes) -> str:
    """The paragraph and table text of a .docx (reports are saved as Word)."""
    import io
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def report_body(answer: str) -> str:
    """The text between the SAVE_REPORT markers (what csai saves), or the answer."""
    m = re.search(r"\[\[\s*SAVE_REPORT\b[^\]]*\]\](.*?)(\[\[\s*/\s*SAVE_REPORT\s*\]\]|$)",
                  answer or "", re.S | re.I)
    return m.group(1) if m else (answer or "")


def words(text: str) -> list:
    return re.findall(r"[A-Za-z0-9]{3,}", text or "")


# --------------------------------------------------------------------------- results

@dataclass
class Stream:
    """One SSE exchange as the desktop saw it."""
    status: int
    headers: dict
    events: list = field(default_factory=list)       # [(event, data)]
    keepalives: int = 0
    first_event_s: Optional[float] = None
    elapsed_s: float = 0.0
    cancelled: bool = False
    body: Optional[dict] = None                      # non-200 JSON body
    consents: list = field(default_factory=list)     # [(request, answer, http status)]

    def kinds(self) -> list:
        return [e for e, _ in self.events]

    def of(self, kind: str) -> list:
        return [d for e, d in self.events if e == kind]

    def one(self, kind: str) -> Optional[dict]:
        got = self.of(kind)
        return got[-1] if got else None

    def items(self) -> dict:
        return {d.get("name") or d.get("file_uid"): d for d in self.of("item")}


@dataclass
class Warning_:
    source: str        # header | event | usage
    state: str
    message: str


# --------------------------------------------------------------------------- the app

class DesktopApp:
    def __init__(self, stack: Stack, key_id: str, secret: str, *, name: str = "desktop",
                 csai: str | None = None, refresh_margin_s: float = 30,
                 honour_retry_after: bool = False, max_backoff_s: float = 10,
                 on_warning: Callable[[Warning_], None] | None = None):
        self.stack, self.key_id, self.secret, self.name = stack, key_id, secret, name
        self.csai = csai or stack.csai
        self.http = httpx.Client(timeout=300)
        self.token: Optional[str] = None
        self.token_exp: float = 0
        self.refresh_margin_s = refresh_margin_s
        self.honour_retry_after = honour_retry_after
        self.max_backoff_s = max_backoff_s
        self.warnings: list[Warning_] = []
        self.usage_states: list[str] = []
        self.backoffs: list[float] = []
        self.refreshes = 0
        self._on_warning = on_warning
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ session
    @property
    def basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.key_id}:{self.secret}".encode()).decode()

    def sign_in(self) -> httpx.Response:
        r = self.http.post(f"{self.stack.rest}/v1/auth/token",
                           headers={"Authorization": self.basic, "X-Tenant": self.stack.tenant})
        if r.status_code == 200:
            self._take(r.json()["token"])
        return r

    def refresh(self) -> httpx.Response:
        r = self.http.post(f"{self.stack.rest}/v1/auth/refresh", headers=self.auth)
        if r.status_code == 200:
            self.refreshes += 1
            self._take(r.json()["token"])
        return r

    def sign_out(self) -> int:
        return self.http.delete(f"{self.stack.rest}/v1/auth/token", headers=self.auth).status_code

    def _take(self, token: str) -> None:
        self.token = token
        self.token_exp = float(self.claims().get("exp", 0))

    def claims(self) -> dict:
        try:
            part = (self.token or "").split(".")[1]
            return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except Exception:
            return {}

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "X-Tenant": self.stack.tenant}

    def _ensure_fresh(self) -> None:
        if self.token is None:
            r = self.sign_in()
            if r.status_code != 200:
                raise SignedOut(f"sign-in refused: {r.status_code}")
        elif self.token_exp and self.token_exp - time.time() < self.refresh_margin_s:
            if self.refresh().status_code != 200:
                raise SignedOut("refresh refused")

    # ------------------------------------------------------------------ usage status
    def _observe(self, state: str | None, message: str, source: str) -> None:
        if not state:
            return
        with self._lock:
            self.usage_states.append(state)
            if state != "ok":
                w = Warning_(source, state, message or "")
                self.warnings.append(w)
                if self._on_warning:
                    self._on_warning(w)

    def _observe_headers(self, r: httpx.Response) -> None:
        self._observe(r.headers.get("x-usage-state"), "", "header")

    # ------------------------------------------------------------------ plain calls
    def call(self, method: str, path: str, *, base: str | None = None, retry_auth=True,
             **kw) -> httpx.Response:
        """A csai call with the session: refreshed when due, once more on 401,
        and (when the app honours it) retried after a 429's Retry-After."""
        self._ensure_fresh()
        url = f"{base or self.csai}{path}"
        headers = {**self.auth, **kw.pop("headers", {})}
        r = self.http.request(method, url, headers=headers, **kw)
        if r.status_code == 401 and retry_auth:
            if self.refresh().status_code != 200:
                return r
            headers.update(self.auth)
            r = self.http.request(method, url, headers=headers, **kw)
        if r.status_code == 429 and self.honour_retry_after:
            wait = min(float(r.headers.get("retry-after") or 1), self.max_backoff_s)
            self.backoffs.append(wait)
            time.sleep(wait)
            r = self.http.request(method, url, headers=headers, **kw)
        self._observe_headers(r)
        if r.status_code == 429:
            try:
                b = r.json()
                self._observe((b.get("usage") or {}).get("state") or "exceeded",
                              b.get("message", ""), "429")
            except ValueError:
                pass
        return r

    def usage(self) -> dict:
        r = self.call("GET", "/v1/usage")
        body = r.json() if r.status_code == 200 else {}
        self._observe(body.get("state"), body.get("message", ""), "usage")
        return body

    def extract_json(self, body: dict) -> httpx.Response:
        return self.call("POST", "/v1/extract", json=body,
                         headers={"Accept": "application/json"})

    def search(self, query: str, **kw) -> httpx.Response:
        return self.call("POST", "/search", json={"query": query, **kw})

    def text(self, uid: str) -> httpx.Response:
        return self.call("GET", f"/documents/{uid}/text")

    def runs(self) -> httpx.Response:
        return self.call("GET", "/v1/runs")

    def run(self, run_id: str) -> httpx.Response:
        return self.call("GET", f"/v1/runs/{run_id}")

    def answer_consent(self, run_id: str, request_id: str, decision: bool,
                       remember: bool = False, base: str | None = None) -> httpx.Response:
        return self.call("POST", f"/v1/runs/{run_id}/consent", base=base,
                         json={"id": request_id, "decision": decision, "remember": remember})

    def download(self, uid: str) -> httpx.Response:
        self._ensure_fresh()
        return self.http.get(f"{self.stack.rest}/v1/files/{uid}/content", headers=self.auth)

    # ------------------------------------------------------------------ streams
    def stream(self, path: str, body: dict, *,
               consent: Callable[[dict], Optional[dict]] | None = None,
               cancel_when: Callable[[str, dict, "Stream"], bool] | None = None,
               on_event: Callable[[str, dict], None] | None = None) -> Stream:
        """POST and read the SSE response as it arrives.

        ``consent(event)`` returns None (ignore it: the dialog is left open) or
        ``{"decision": bool, "remember": bool, "delay": s, "base": url}``.
        ``cancel_when(event, data, stream)`` True hangs up after that event."""
        self._ensure_fresh()
        t0 = time.time()
        out = Stream(status=0, headers={})
        with self.http.stream("POST", f"{self.csai}{path}", json=body,
                              headers={**self.auth, "Accept": "text/event-stream"}) as resp:
            out.status, out.headers = resp.status_code, dict(resp.headers)
            self._observe_headers(resp)
            if resp.status_code != 200:
                resp.read()
                try:
                    out.body = resp.json()
                    if resp.status_code == 429:
                        self._observe("exceeded", out.body.get("message", ""), "429")
                except ValueError:
                    out.body = {"raw": resp.text[:500]}
                out.elapsed_s = time.time() - t0
                return out
            ev, data_lines = None, []
            for line in resp.iter_lines():
                if line.startswith(":"):
                    out.keepalives += 1
                    continue
                if line.startswith("event:"):
                    ev = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line == "" and ev is not None:
                    data = json.loads("\n".join(data_lines)) if data_lines else {}
                    ev_name, ev, data_lines = ev, None, []
                    if out.first_event_s is None:
                        out.first_event_s = time.time() - t0
                    out.events.append((ev_name, data))
                    if ev_name == "usage":
                        self._observe(data.get("state"), data.get("message", ""), "event")
                    if on_event:
                        on_event(ev_name, data)
                    if ev_name == "tool_consent_request" and consent:
                        ans = consent(data)
                        if ans is not None:
                            if ans.get("delay"):
                                time.sleep(ans["delay"])
                            r = self.answer_consent(data.get("run_id"), data["id"],
                                                    bool(ans.get("decision")),
                                                    bool(ans.get("remember")),
                                                    base=ans.get("base"))
                            out.consents.append((data, ans, r.status_code))
                    if cancel_when and cancel_when(ev_name, data, out):
                        out.cancelled = True
                        break
        out.elapsed_s = time.time() - t0
        return out

    def extract(self, body: dict, **kw) -> Stream:
        return self.stream("/v1/extract", body, **kw)

    def research(self, body: dict, **kw) -> Stream:
        return self.stream("/v1/research", body, **kw)
