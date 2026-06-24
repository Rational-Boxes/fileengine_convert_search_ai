"""The HTTP / WebSocket API surface for convert_search_ai — one explicit router.

  GET  /healthz                    liveness
  GET  /readyz                     readiness (gRPC core + LDAP reachable)
  POST /auth/token                 LDAP bind -> bearer token
  GET  /whoami                     resolved identity (user, roles, tenant)
  POST /search                     permission-gated full-text + fuzzy search
  GET  /documents/{uid}/text       extracted Markdown (READ-gated)
  WS   /chat                       permission-scoped RAG chat (streamed)
  POST /ingest/reconcile           trigger a reconcile sweep

build_app() wires the shared services onto app.state and includes this router.
Handlers read those services from request/websocket ``app.state``."""
from __future__ import annotations

import anyio
from fastapi import (APIRouter, Body, Depends, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config
from .guards import GuardError
from .http_auth import extract_tenant, resolve_identity
from .ldap_auth import Identity, authenticate

router = APIRouter()


# --------------------------- shared helpers --------------------------------
def _check_ldap(config: Config) -> bool:
    try:
        if not config.agent_user or not config.agent_password:
            return False
        return authenticate(config, config.agent_user, config.agent_password).authenticated
    except Exception:
        return False


def _check_core(config: Config) -> bool:
    try:
        import grpc
        channel = grpc.insecure_channel(config.grpc_address)
        try:
            grpc.channel_ready_future(channel).result(timeout=2)
            return True
        finally:
            channel.close()
    except Exception:
        return False


def _identity(request: Request) -> Identity:
    """Resolve the requesting user from Authorization (Basic/Bearer) + tenant."""
    config: Config = request.app.state.config
    headers = {k.lower(): v for k, v in request.headers.items()}
    tenant = extract_tenant(headers, headers.get("host", ""), config.tenant)
    ident = resolve_identity(headers.get("authorization", ""), tenant, config,
                             request.app.state.token_store,
                             getattr(request.app.state, "bridge_verifier", None))
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return ident


# ------------------------------- health ------------------------------------
@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "convert_search_ai", "version": __version__}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    config = request.app.state.config
    checks = {"core": _check_core(config), "ldap": _check_ldap(config)}
    ready = all(checks.values())
    return JSONResponse(status_code=200 if ready else 503,
                        content={"ready": ready, "checks": checks})


# -------------------------------- auth -------------------------------------
@router.post("/auth/token")
def auth_token(request: Request, body: dict = Body(...)) -> JSONResponse:
    config = request.app.state.config
    ident = authenticate(config, body.get("username", ""), body.get("password", ""))
    if not ident.authenticated:
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})
    token = request.app.state.token_store.issue(ident)
    return JSONResponse(status_code=200, content={"access_token": token, "token_type": "bearer"})


@router.get("/whoami")
def whoami(identity: Identity = Depends(_identity)) -> dict:
    return {"user": identity.user, "roles": identity.roles, "tenant": identity.tenant}


# ------------------------------- search ------------------------------------
@router.post("/search")
def search(request: Request, body: dict = Body(...), identity: Identity = Depends(_identity)) -> dict:
    try:
        hits = request.app.state.search.search(
            identity, body.get("query", ""),
            limit=int(body.get("limit", 20)), fuzzy=bool(body.get("fuzzy", True)))
    except GuardError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"query": (body.get("query") or "").strip(), "tenant": identity.tenant,
            "hits": [{"file_uid": h.file_uid, "name": h.name, "snippet": h.snippet, "score": h.score}
                     for h in hits]}


@router.get("/documents/{file_uid}/text")
def document_text(file_uid: str, request: Request, identity: Identity = Depends(_identity)) -> dict:
    try:
        text, truncated = request.app.state.search.get_text(identity, file_uid)
    except PermissionError:
        raise HTTPException(status_code=403, detail="not permitted")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no extracted text for this file")
    return {"file_uid": file_uid, "tenant": identity.tenant, "text": text, "truncated": truncated}


# -------------------------------- chat -------------------------------------
@router.websocket("/chat")
async def chat(ws: WebSocket) -> None:
    """Permission-scoped RAG chat. Authenticate with a bearer token (Authorization
    header or ``?token=``); each message carries the conversation-specific
    ``system_prompt`` plus ``message`` (and optional ``history``/``k``). The server
    streams ``{type: token}`` deltas then ``{type: citations}``."""
    config = ws.app.state.config
    headers = {k.lower(): v for k, v in ws.headers.items()}
    auth = headers.get("authorization", "")
    if not auth and ws.query_params.get("token"):
        auth = "Bearer " + ws.query_params["token"]
    tenant = ws.query_params.get("tenant") or extract_tenant(headers, headers.get("host", ""), config.tenant)

    identity = await run_in_threadpool(
        resolve_identity, auth, tenant, config, ws.app.state.token_store,
        getattr(ws.app.state, "bridge_verifier", None))
    await ws.accept()
    if identity is None:
        await ws.send_json({"type": "error", "error": "authentication required"})
        await ws.close(code=4401)
        return

    chat_service = ws.app.state.chat
    try:
        while True:
            payload = await ws.receive_json()
            message = (payload.get("message") or "").strip()
            if not message:
                await ws.send_json({"type": "error", "error": "message is required"})
                continue
            await _stream_answer(ws, chat_service, identity, payload, message)
            await ws.send_json({"type": "done"})
    except WebSocketDisconnect:
        return


async def _stream_answer(ws: WebSocket, chat_service, identity, payload: dict, message: str) -> None:
    """Bridge the sync RAG generator (which does blocking I/O) to the async socket
    via a worker thread + memory stream, so the event loop is never blocked."""
    send, recv = anyio.create_memory_object_stream(256)

    def produce():
        try:
            for ev in chat_service.answer(
                identity, message=message,
                system_prompt=payload.get("system_prompt", ""),
                history=payload.get("history") or [],
                k=int(payload.get("k", 8)),
            ):
                anyio.from_thread.run(send.send, ev)
        except Exception as e:  # surface, don't crash the socket loop
            anyio.from_thread.run(send.send, {"type": "error", "error": str(e)})
        finally:
            anyio.from_thread.run(send.aclose)

    async with anyio.create_task_group() as tg:
        tg.start_soon(anyio.to_thread.run_sync, produce)
        async with recv:
            async for ev in recv:
                await ws.send_json(ev)


# ----------------------------- ingestion -----------------------------------
@router.post("/ingest/reconcile")
def ingest_reconcile(request: Request, tenant: str | None = Query(default=None),
                     max_files: int | None = Query(default=None)) -> JSONResponse:
    config = request.app.state.config
    if not _check_core(config):
        return JSONResponse(status_code=503, content={"error": "core not reachable"})
    from .reconcile import reconcile
    counts = reconcile(config, tenant, max_files=max_files)
    return JSONResponse(status_code=200, content={"tenant": tenant or config.tenant, "counts": counts})
