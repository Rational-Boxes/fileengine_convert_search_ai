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

"""The headless HTTP surface (HEADLESS_RESEARCH_API.md §3).

  POST /v1/extract                  structured extraction from a folder or ids
  POST /v1/research                 a question or a pinned report, over SSE
  POST /v1/runs/{run_id}/consent    answer an MCP approval request
  GET  /v1/usage                    this key's usage status
  GET  /v1/runs                     the caller's headless runs, per application
  GET  /v1/runs/{run_id}            one run, read-only

Authentication shadows the REST door: the bearer a generated key receives at
``POST /api/v1/auth/token`` (scope ``api``). Every response carries the usage
status headers; streams carry ``usage`` events first and last."""
from __future__ import annotations

import logging
import secrets
from typing import List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import audit
from ..api import _identity, _ingestor
from ..ldap_auth import Identity
from ..providers.base import collect_usage
from . import usage as usage_mod
from .extract import ExtractRequestError, Extractor, parse_request
from .sse import SSE_HEADERS, EventRun, wants_json

log = logging.getLogger("convert_search_ai.headless")

router = APIRouter(prefix="/v1", tags=["headless"])


# ------------------------------------------------------------------ services

def _svc(app):
    """Shared headless services, built once per app."""
    s = getattr(app.state, "headless", None)
    if s is not None:
        return s
    config = app.state.config
    from ..core_client import agent_client
    from ..store import DocumentStore
    from .cache import ExtractionCache
    from .consent_relay import ConsentRelay
    from .runs import RunStore

    def redis_client():
        import redis
        return redis.Redis(host=config.redis_host, port=config.redis_port,
                           password=config.redis_password or None, db=config.redis_db)

    backend = getattr(app.state, "headless_usage_backend", None) or usage_mod.redis_backend(config)
    s = type("HeadlessServices", (), {})()
    s.meter = usage_mod.UsageMeter(usage_mod.Limits.from_config(config), backend)
    s.runs = getattr(app.state, "headless_runs", None) or RunStore(config)
    s.store = getattr(app.state, "headless_store", None) or DocumentStore(config)
    s.cache = getattr(app.state, "headless_cache", None) or ExtractionCache(
        lambda: agent_client(config))
    s.relay = getattr(app.state, "headless_relay", None) or ConsentRelay(redis_client)
    app.state.headless = s
    return s


def _convert_fn(app):
    """Re-index one file before reading its text (the stored text is stale)."""
    override = getattr(app.state, "headless_convert", None)
    if override is not None:
        return override
    config = app.state.config

    def convert(file_uid: str, tenant: str):
        from ..db import provision_tenant
        provision_tenant(config, tenant)
        ing = _ingestor(app)
        out = ing.pipeline.convert(file_uid, tenant)
        try:
            ing.emitter.emit_conversion({"tenant": tenant, "actor": "headless",
                                         "file_uid": file_uid}, out)
        except Exception:
            log.warning("conversion announcement failed for %s", file_uid, exc_info=True)
        return out
    return convert


def _client_for(app):
    override = getattr(app.state, "headless_client_for", None)
    if override is not None:
        return override
    from ..core_client import client_for
    return lambda identity: client_for(identity, app.state.config)


# ------------------------------------------------------------------ helpers

def _usage_error(e: Exception, identity) -> JSONResponse:
    if isinstance(e, usage_mod.RateLimited):
        st = e.status
        audit.record(action="rate_limited", user=identity.user, tenant=identity.tenant,
                     result="denied", limit=e.limit, client_key_id=identity.client_key_id)
        reset = st["limits"].get(e.limit, {}).get("resets_at", "")
        headers = usage_mod.headers(st)
        retry = 60 if e.limit == "requests" else (5 if e.limit == "concurrent" else 3600)
        headers["Retry-After"] = str(retry)
        return JSONResponse(status_code=429, headers=headers, content={
            "kind": "rate_limited", "limit": e.limit, "resets_at": reset, "usage": st,
            "message": st.get("message") or f"The {e.limit} limit for this key is reached."})
    return JSONResponse(status_code=503, content={
        "kind": "usage_unavailable",
        "error": "usage counters are unavailable; headless requests are refused until "
                 "they are back"})


def _admit(app, identity, *, spends_tokens: bool):
    """(status, None) or (None, error response)."""
    try:
        return _svc(app).meter.admit(identity, spends_tokens=spends_tokens), None
    except (usage_mod.RateLimited, usage_mod.UsageUnavailable) as e:
        return None, _usage_error(e, identity)


class _Slot:
    """A concurrency slot taken in the route and released by the producer."""

    def __init__(self, meter, identity):
        self._cm = meter.run(identity)
        self._held = False

    def take(self):
        self._cm.__enter__()
        self._held = True

    def release(self):
        if self._held:
            self._held = False
            self._cm.__exit__(None, None, None)


def _respond(request: Request, run: EventRun, status: dict, shape):
    """SSE by default; ``Accept: application/json`` runs to the end and returns
    ``shape(events, result)``."""
    headers = usage_mod.headers(status)
    if wants_json(request.headers.get("accept", "")):
        async def whole():
            events, result = await run_in_threadpool(run.collect)
            return JSONResponse(content=shape(events, result), headers=headers)
        return whole()
    run.start()
    return StreamingResponse(run.sse(), media_type="text/event-stream",
                             headers={**SSE_HEADERS, **headers})


def _final_usage(meter, identity, fallback: dict) -> dict:
    try:
        return meter.status(identity)
    except usage_mod.UsageUnavailable:
        return fallback


# ------------------------------------------------------------------ models

class MatchBody(BaseModel):
    extensions: List[str] = Field(default_factory=list, examples=[[".pdf"]])
    name: str = Field("", examples=["*datasheet*"])


class ExtractBody(BaseModel):
    """Exactly one of ``folder_uid`` or ``files``."""
    model_config = ConfigDict(populate_by_name=True)
    folder_uid: Optional[str] = None
    files: Optional[List[str]] = None
    recursive: bool = False
    match: Optional[MatchBody] = None
    schema_: dict = Field(alias="schema",
                          description="JSON Schema (type object) of the values wanted")
    instructions: str = ""
    cache: Literal["use", "refresh"] = "use"


class ReportTarget(BaseModel):
    folder_uid: str = ""
    filename: str
    path: str = ""


class ToolsBody(BaseModel):
    mcp: Literal["deny", "ask"] = "deny"


class ResearchBody(BaseModel):
    message: str
    history: List[dict] = Field(default_factory=list)
    k: int = 8
    web_search: Optional[bool] = None
    scope_folders: List[dict] = Field(default_factory=list)
    report: Optional[ReportTarget] = None
    tools: ToolsBody = Field(default_factory=ToolsBody)
    system_prompt: str = ""
    app_url: str = ""


class ConsentBody(BaseModel):
    id: str
    decision: bool
    remember: bool = False


# ------------------------------------------------------------------ routes

@router.get("/usage")
def get_usage(request: Request, identity: Identity = Depends(_identity)):
    """This key's usage status (or the user's, for a session without a key)."""
    try:
        st = _svc(request.app).meter.status(identity)
    except usage_mod.UsageUnavailable as e:
        return _usage_error(e, identity)
    return JSONResponse(content=st, headers=usage_mod.headers(st))


@router.post("/extract")
async def extract(request: Request, body: ExtractBody,
                  identity: Identity = Depends(_identity)):
    """Structured, evidence-backed extraction from each document in a folder or a
    list of document ids. SSE events: ``usage``, ``plan``, one ``item`` per file
    (or ``error``), ``usage``, ``done``."""
    app = request.app
    config = app.state.config
    try:
        req = parse_request(body.model_dump(by_alias=True), config)
    except ExtractRequestError as e:
        raise HTTPException(status_code=400, detail=str(e))
    status, err = _admit(app, identity, spends_tokens=True)
    if err is not None:
        return err
    s = _svc(app)
    slot = _Slot(s.meter, identity)
    try:
        slot.take()
    except (usage_mod.RateLimited, usage_mod.UsageUnavailable) as e:
        return _usage_error(e, identity)

    extractor = Extractor(config, client_for=_client_for(app), gate=app.state.permission_gate,
                          store=s.store, chat=app.state.chat.chat, cache=s.cache,
                          convert=_convert_fn(app), meter=s.meter)

    def producer(emit, cancelled):
        try:
            emit("usage", status)
            run_id = _start_run(s.runs, identity, title=_extract_title(req), request={
                "kind": "extract", "target": ({"folder_uid": req.folder_uid,
                                               "recursive": req.recursive}
                                              if req.folder_uid is not None
                                              else {"files": req.files}),
                "match": {"extensions": req.extensions, "name": req.name_glob},
                "schema": req.schema, "instructions": req.instructions, "cache": req.cache})
            try:
                summary = extractor.run(
                    identity, req, emit, cancelled,
                    on_item=lambda item: _save(s.runs.add_result, identity, run_id, item))
            except ExtractRequestError as e:
                emit("error", {"error": str(e), "kind": "bad_request"})
                summary = {"error": str(e)}
            _save(s.runs.finish, identity, run_id, summary)
            audit.record(action="extract", user=identity.user, tenant=identity.tenant,
                         result="error" if "error" in summary else "ok",
                         client_key_id=identity.client_key_id,
                         client_label=identity.client_label,
                         target="folder" if req.folder_uid is not None else "files",
                         folder_uid=req.folder_uid or "", requested=len(req.files),
                         fields=req.fields, model=getattr(app.state.chat.chat, "model_id", ""),
                         **{k: v for k, v in summary.items() if isinstance(v, (int, bool))})
            final = _final_usage(s.meter, identity, status)
            emit("usage", final)
            emit("done", {"run_id": run_id, **summary})
            return {"run_id": run_id, "summary": summary, "usage": final}
        finally:
            slot.release()

    def shape(events, result):
        return {"run_id": result.get("run_id"),
                "plan": next((d for e, d in events if e == "plan"), None),
                "items": [d for e, d in events if e in ("item", "error")],
                "summary": result.get("summary"), "usage": result.get("usage")}

    return await _maybe_await(_respond(request, EventRun(producer), status, shape))


@router.post("/research")
async def research(request: Request, body: ResearchBody,
                   identity: Identity = Depends(_identity)):
    """A question about the documents, or — with ``report`` — the existing
    pinned-destination report: saved into FileEngine with its provenance
    sidecar and announced by ``report_saved``; download it through the REST door.
    SSE events: ``usage``, ``token``, ``tool_call``/``tool_result``,
    ``tool_consent_request``, ``citations``, ``report_saved``, ``result``,
    ``usage``, ``done``."""
    app = request.app
    config = app.state.config
    cap = int(getattr(config, "headless_max_input_chars", 20000))
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    if len(body.message) > cap:
        raise HTTPException(status_code=400, detail=f"message exceeds {cap} characters")
    status, err = _admit(app, identity, spends_tokens=True)
    if err is not None:
        return err
    s = _svc(app)
    slot = _Slot(s.meter, identity)
    try:
        slot.take()
    except (usage_mod.RateLimited, usage_mod.UsageUnavailable) as e:
        return _usage_error(e, identity)

    from ..app_links import resolve_app_url
    from ..consent import ConsentBroker
    app_url = resolve_app_url(config, {k.lower(): v for k, v in request.headers.items()},
                              tenant=identity.tenant, client_url=body.app_url,
                              default_scheme="https" if request.url.scheme == "https" else "http")
    report_target = (body.report.model_dump() if body.report and body.report.filename.strip()
                     else None)
    ask = body.tools.mcp == "ask"
    scope = [str(f["uid"]) for f in body.scope_folders
             if isinstance(f, dict) and str(f.get("uid") or "").strip()]
    chat = app.state.chat

    def producer(emit, cancelled):
        try:
            emit("usage", status)
            run_id = _start_run(s.runs, identity, title=_title(body.message), request={
                "kind": "report" if report_target else "research", "message": body.message,
                "report": report_target, "scope_folders": body.scope_folders,
                "web_search": body.web_search, "tools": {"mcp": body.tools.mcp}})
            spent = [0]

            def sink(i, o):
                spent[0] += i + o

            broker = None
            if ask:
                def emit_consent(ev):
                    s.relay.register(ev["id"], broker, identity, run_id or "")
                    emit("tool_consent_request", {k: v for k, v in ev.items() if k != "type"}
                         | {"run_id": run_id})
                broker = ConsentBroker(
                    emit_consent, new_id=lambda: secrets.token_urlsafe(18),
                    timeout_s=max(1.0, getattr(config, "mcp_consent_timeout_ms", 120000) / 1000.0))
            parts, citations, report = [], [], None
            with s.relay.listen(identity, run_id or ""), collect_usage(sink):
                gen = chat.answer(identity, message=body.message,
                                  system_prompt=body.system_prompt, history=body.history,
                                  k=body.k, web_search=body.web_search,
                                  conversation_id=run_id, report_target=report_target,
                                  scope_folder_uids=scope or None, app_url=app_url,
                                  consent=broker.request if broker else None,
                                  allow_mcp=ask, max_chars=cap)
                try:
                    for ev in gen:
                        if cancelled():
                            break
                        t = ev.get("type")
                        if t == "token":
                            parts.append(ev.get("text", ""))
                        elif t == "citations":
                            citations = ev.get("citations", [])
                        elif t == "report_saved":
                            report = {k: v for k, v in ev.items() if k != "type"}
                        emit(t, {k: v for k, v in ev.items() if k != "type"})
                finally:
                    gen.close()
                    if broker is not None:
                        broker.shutdown()
            s.meter.add_tokens(identity, spent[0])
            answer = "".join(parts)
            _save(s.runs.append, identity, run_id, "assistant", answer, citations)
            audit.record(action="research", user=identity.user, tenant=identity.tenant,
                         result="cancelled" if cancelled() else "ok",
                         client_key_id=identity.client_key_id,
                         client_label=identity.client_label,
                         kind="report" if report_target else "research",
                         citations=len(citations), report_saved=bool(report), tokens=spent[0])
            result = {"answer": answer, "citations": citations, "report": report,
                      "tokens": spent[0]}
            emit("result", result)
            final = _final_usage(s.meter, identity, status)
            emit("usage", final)
            emit("done", {"run_id": run_id})
            return {"run_id": run_id, "result": result, "usage": final}
        finally:
            slot.release()

    def shape(events, result):
        return {"run_id": result.get("run_id"), **(result.get("result") or {}),
                "usage": result.get("usage")}

    return await _maybe_await(_respond(request, EventRun(producer), status, shape))


@router.post("/runs/{run_id}/consent")
def consent(run_id: str, request: Request, body: ConsentBody,
            identity: Identity = Depends(_identity)):
    """Answer a ``tool_consent_request``. Only the user and key that started the
    run can answer it; an unknown or foreign id is 404."""
    delivered = _svc(request.app).relay.answer(identity, run_id, body.id, body.decision,
                                               body.remember)
    if not delivered:
        raise HTTPException(status_code=404, detail="no such pending approval")
    return {"run_id": run_id, "id": body.id, "accepted": True}


@router.get("/runs")
def list_runs(request: Request, identity: Identity = Depends(_identity)):
    """The caller's headless runs, grouped per application (generated key)."""
    try:
        apps = _svc(request.app).runs.applications(identity.tenant, identity.user)
    except Exception:
        log.warning("headless run listing failed", exc_info=True)
        apps = []
    meter = _svc(request.app).meter
    for a in apps:
        try:
            probe = Identity(user=identity.user, tenant=identity.tenant, authenticated=True,
                             client_key_id=a["client_key_id"])
            a["usage"] = meter.status(probe)
        except usage_mod.UsageUnavailable:
            a["usage"] = None
    return {"applications": apps}


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request, identity: Identity = Depends(_identity)):
    run = _svc(request.app).runs.get(identity.tenant, identity.user, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


# ------------------------------------------------------------------ bits

async def _maybe_await(x):
    import inspect
    return await x if inspect.isawaitable(x) else x


def _start_run(runs, identity, *, title: str, request: dict) -> Optional[str]:
    try:
        return runs.create(identity, title=title, request=request)
    except Exception:
        log.warning("headless run not recorded (store unavailable)", exc_info=True)
        return None


def _save(fn, identity, run_id, *args) -> None:
    if not run_id:
        return
    try:
        fn(identity, run_id, *args)
    except Exception:
        log.warning("headless run record update failed", exc_info=True)


def _title(message: str) -> str:
    t = " ".join((message or "").split())[:60]
    return t or "Headless run"


def _extract_title(req) -> str:
    fields = ", ".join(req.fields[:4]) + ("…" if len(req.fields) > 4 else "")
    where = "folder" if req.folder_uid is not None else f"{len(req.files)} file(s)"
    return f"Extract {fields} from {where}"[:120]
