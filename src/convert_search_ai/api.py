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

"""The HTTP / WebSocket API surface for convert_search_ai — one explicit router.

  GET  /healthz                    liveness
  GET  /readyz                     readiness (gRPC core + LDAP reachable)
  POST /auth/token                 LDAP bind -> bearer token
  GET  /whoami                     resolved identity (user, roles, tenant)
  POST /search                     permission-gated full-text + fuzzy search
  GET  /documents/{uid}/text       extracted Markdown (READ-gated)
  POST /internal/documents/{uid}/text   the same, for an in-cluster service
                                   asserting whose behalf it acts (shared secret)
  WS   /chat                       permission-scoped RAG chat (streamed)
  POST /ingest/reconcile           trigger a reconcile sweep

build_app() wires the shared services onto app.state and includes this router.
Handlers read those services from request/websocket ``app.state``."""
from __future__ import annotations

import logging
import secrets
from functools import partial

import anyio
from fastapi import (APIRouter, Body, Depends, Header, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__, audit
from .app_links import resolve_app_url
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


def _ingestor(app):
    """The agent-backed ingestor (gRPC client + pipeline), built lazily and cached
    on app.state so build_app() stays cheap and import-only for tests."""
    ing = getattr(app.state, "ingestor", None)
    if ing is None:
        from .ingest import build_ingestor
        ing = build_ingestor(app.state.config)
        app.state.ingestor = ing
    return ing


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


# ------------------------------ internal API ---------------------------------
#
# For a service that has already authenticated a user of its own and needs this
# service to act for them. The MCP door is the caller this exists for: it holds
# an identity and an ACL-enforced core client, but no credential CSAI accepts —
# `resolve_identity` takes this service's tokens, http_bridge's tokens, or an
# LDAP password, and MCP has none of the three for its caller. Minting a bridge
# token instead would mean handing MCP the bridge's signing key, i.e. the ability
# to impersonate anyone, which is a far larger grant than reading one document.
#
# What is trusted here is narrow: the CALLER'S NAME. Everything downstream is
# unchanged — the same `get_text`, the same READ check against the core for that
# principal, the same audit record and the same 403. So the assertion can name a
# user, but it cannot give that user access they do not have; the worst a stolen
# secret buys is reading what some OTHER named user is already allowed to read,
# and only for documents this service has extracted.
#
# The secret is required, and an unset secret disables the route rather than
# opening it. That is not paranoia about defaults: the edge proxies /csai/ as a
# whole prefix, so anything mounted here is reachable from the public internet
# unless something stops it — which is exactly how /ingest/reconcile came to be
# an open denial-of-service lever. The ingress also 404s /csai/internal/ at the
# edge, so this route is in-cluster only even if the secret leaks.
def _require_internal(config: Config, presented: str | None) -> None:
    secret = config.internal_secret
    if not secret:
        raise HTTPException(status_code=404, detail="internal API not enabled")
    if not presented or not secrets.compare_digest(presented, secret):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/internal/documents/{file_uid}/text")
def internal_document_text(file_uid: str, request: Request, body: dict = Body(default={}),
                           x_internal_auth: str | None = Header(default=None)) -> dict:
    """Extracted Markdown for ``file_uid`` as the principal the caller names.

    Body: ``{"user": "<uid>", "roles": [...], "tenant": "<tenant>"}``. The tenant
    is taken from the body rather than the Host header — an in-cluster caller
    reaches this service by container name, so there is no tenant in the URL to
    infer one from."""
    config: Config = request.app.state.config
    _require_internal(config, x_internal_auth)

    user = (body or {}).get("user") or ""
    tenant = (body or {}).get("tenant") or ""
    roles = list((body or {}).get("roles") or [])
    if not user or not tenant:
        raise HTTPException(status_code=400, detail="user and tenant are required")
    # authenticated=True states that the CALLER authenticated them, which is the
    # whole content of the assertion. It buys no access by itself: get_text runs
    # the READ check against the core as this principal before returning a byte.
    identity = Identity(user=user, roles=roles, tenant=tenant, authenticated=True)
    try:
        text, truncated = request.app.state.search.get_text(identity, file_uid)
    except PermissionError:
        raise HTTPException(status_code=403, detail="not permitted")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no extracted text for this file")
    return {"file_uid": file_uid, "tenant": tenant, "text": text, "truncated": truncated}


# ---------------------------- conversations --------------------------------
# Persisted chat history, scoped to the authenticated user within their tenant.
@router.get("/conversations")
def list_conversations(request: Request, identity: Identity = Depends(_identity)) -> dict:
    return {"conversations": request.app.state.conversations.list(identity.tenant, identity.user)}


@router.post("/conversations")
def create_conversation(request: Request, body: dict = Body(default={}),
                        identity: Identity = Depends(_identity)) -> dict:
    cid = request.app.state.conversations.create(
        identity.tenant, identity.user, title=(body or {}).get("title", ""))
    return {"id": cid}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request,
                     identity: Identity = Depends(_identity)) -> dict:
    convo = request.app.state.conversations.get(identity.tenant, identity.user, conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return convo


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request,
                        identity: Identity = Depends(_identity)) -> dict:
    if not request.app.state.conversations.delete(identity.tenant, identity.user, conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"deleted": conversation_id}


def _title_from(message: str) -> str:
    """A short conversation title derived from the first user message."""
    t = " ".join((message or "").split())[:60]
    return t or "New chat"


# -------------------------------- chat -------------------------------------
@router.websocket("/chat")
async def chat(ws: WebSocket) -> None:
    """Permission-scoped RAG chat. Authenticate with a bearer token (Authorization
    header or ``?token=``). Each message carries ``message`` (+ optional
    ``system_prompt``/``history``/``k``/``web_search``/``conversation_id``/``app_url``
    — the URL the SPA is running on, used for absolute deep-links in saved reports).
    The server persists the turn, emits ``{type: conversation, id}`` (so the client can
    resume later), then streams ``{type: token}`` deltas, tool events, and
    ``{type: citations}``, finishing with ``{type: done}``."""
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
    convos = ws.app.state.conversations
    # MCP tool consent approvals the user chose to "remember" persist for the life of
    # this WebSocket connection (the conversation), so the same tool isn't re-prompted.
    remembered_consents: set[str] = set()
    try:
        while True:
            payload = await ws.receive_json()
            message = (payload.get("message") or "").strip()
            if not message:
                await ws.send_json({"type": "error", "error": "message is required"})
                continue
            # Resolve/create the conversation + store the user turn (and persist the
            # RAG folder scope so resuming restores it). Best-effort: a persistence
            # outage must not break the chat (conv_id falls to None).
            conv_id = await run_in_threadpool(
                _begin_turn, convos, identity, payload.get("conversation_id"), message,
                _scope_from_payload(payload))
            if conv_id:
                await ws.send_json({"type": "conversation", "id": conv_id})
            answer, citations = await _stream_answer(
                ws, chat_service, identity, payload, message, conv_id,
                remembered_consents=remembered_consents)
            if conv_id:
                await run_in_threadpool(_end_turn, convos, identity, conv_id, answer, citations)
            await ws.send_json({"type": "done"})
    except WebSocketDisconnect:
        return


def _scope_from_payload(payload: dict):
    """Normalize the RAG folder scope from a chat frame: ``scope_folders`` is a list
    of ``{uid, path}``. Returns the cleaned list when the key is present (even empty,
    so a cleared scope persists), or ``None`` when absent (leave the stored scope)."""
    if "scope_folders" not in payload:
        return None
    out = []
    for f in payload.get("scope_folders") or []:
        if isinstance(f, dict) and str(f.get("uid") or "").strip():
            out.append({"uid": str(f["uid"]), "path": str(f.get("path") or "")})
    return out


def _begin_turn(convos, identity: Identity, conv_id, message: str, scope=None):
    """Resolve/create the conversation and store the user message. Returns the
    conversation id, or None if persistence is unavailable (chat still proceeds).
    When ``scope`` is not None it is persisted as the conversation's RAG folder scope
    (so resuming restores the "Limit to folders" tool); ``None`` leaves it unchanged."""
    try:
        if not (conv_id and convos.owns(identity.tenant, identity.user, conv_id)):
            conv_id = convos.create(identity.tenant, identity.user, title=_title_from(message))
        convos.append(identity.tenant, identity.user, conv_id, "user", message)
        convos.set_title_if_empty(identity.tenant, identity.user, conv_id, _title_from(message))
        if scope is not None:
            convos.set_scope(identity.tenant, identity.user, conv_id, scope)
        return conv_id
    except Exception:
        logging.getLogger("convert_search_ai.chat").warning(
            "conversation persist (begin) failed", exc_info=True)
        return None


def _end_turn(convos, identity: Identity, conv_id, answer: str, citations) -> None:
    try:
        convos.append(identity.tenant, identity.user, conv_id, "assistant", answer, citations=citations)
    except Exception:
        logging.getLogger("convert_search_ai.chat").warning(
            "conversation persist (end) failed", exc_info=True)


async def _stream_answer(ws: WebSocket, chat_service, identity, payload: dict, message: str,
                         conversation_id=None, remembered_consents: set | None = None):
    """Bridge the sync RAG generator (blocking I/O) to the async socket via a worker
    thread + memory stream. Forwards every event to the client and returns the
    accumulated ``(answer_text, citations)`` so the turn can be persisted.

    An MCP tool call pauses in the worker thread on a :class:`~.consent.ConsentBroker`
    while the user approves/denies over the same socket; a concurrent reader task
    routes ``tool_consent`` replies back to it. On a socket drop the broker is shut
    down (every pending/future consent denies) so the worker thread unblocks."""
    from .consent import ConsentBroker

    send, recv = anyio.create_memory_object_stream(256)
    config = ws.app.state.config
    consent_timeout_s = max(1.0, getattr(config, "mcp_consent_timeout_ms", 120000) / 1000.0)

    def _emit(ev: dict) -> None:  # worker thread -> ordered client event stream
        anyio.from_thread.run(send.send, ev)

    broker = ConsentBroker(_emit, timeout_s=consent_timeout_s,
                           remembered=remembered_consents if remembered_consents is not None else set())

    # "Generate report" (GENERATE_REPORT_TO_TARGET): the user pinned an exact
    # destination in the UI. Presence of the folder UID + a non-empty filename puts
    # this turn in report mode; the destination is authoritative (the model never
    # chooses it). folder UID "" means the filesystem root.
    report_target = None
    if "report_target_folder_uid" in payload and str(payload.get("report_target_filename", "")).strip():
        report_target = {
            "folder_uid": str(payload.get("report_target_folder_uid") or ""),
            "filename": str(payload.get("report_target_filename", "")),
            "path": str(payload.get("report_target_path", "") or ""),
        }

    # The application URL this chat is running on — resolved per turn and per
    # TENANT, since each tenant reaches the app on its own subdomain or domain (the
    # SPA sends the door it is on; the request's own origin is the fallback).
    # Reports are saved as documents that leave the browser — .docx, PDF, mail — so
    # their file references must be complete deep-links, not app-relative paths.
    # See app_links.resolve_app_url.
    app_url = resolve_app_url(
        config, {k.lower(): v for k, v in ws.headers.items()},
        tenant=getattr(identity, "tenant", "") or "",
        client_url=str(payload.get("app_url", "") or ""),
        default_scheme="https" if ws.url.scheme == "wss" else "http")

    # Optional RAG folder scope: confine retrieval to these folder UIDs + subfolders.
    # The frame carries `scope_folders` as [{uid, path}] (path is for display/persist);
    # here we take the UIDs. Absent/empty ⇒ all documents (default).
    scope_folder_uids = [str(f["uid"]) for f in (payload.get("scope_folders") or [])
                         if isinstance(f, dict) and str(f.get("uid") or "").strip()]

    def produce():
        try:
            for ev in chat_service.answer(
                identity, message=message,
                system_prompt=payload.get("system_prompt", ""),
                history=payload.get("history") or [],
                k=int(payload.get("k", 8)),
                web_search=payload.get("web_search"),
                conversation_id=conversation_id,
                report_target=report_target,
                scope_folder_uids=scope_folder_uids or None,
                app_url=app_url,
                consent=broker.request,
            ):
                anyio.from_thread.run(send.send, ev)
        except Exception as e:  # surface, don't crash the socket loop
            anyio.from_thread.run(send.send, {"type": "error", "error": str(e)})
        finally:
            anyio.from_thread.run(send.aclose)

    parts: list[str] = []
    citations: list = []
    async with anyio.create_task_group() as tg:
        # Read inbound control messages (consent replies) for the duration of this
        # turn only; the task is cancelled once the answer stream is exhausted, so the
        # outer chat() loop resumes ownership of receive_json for the next message.
        async def read_control():
            try:
                while True:
                    msg = await ws.receive_json()
                    if isinstance(msg, dict) and msg.get("type") == "tool_consent":
                        broker.resolve(str(msg.get("id", "")), bool(msg.get("decision")),
                                       bool(msg.get("remember")))
            except Exception:  # disconnect / bad frame — deny pending consent (CancelledError is BaseException, so a normal cancel is unaffected)
                broker.shutdown()  # unblock the worker thread -> deny

        tg.start_soon(read_control)
        tg.start_soon(anyio.to_thread.run_sync, produce)
        async with recv:
            async for ev in recv:
                t = ev.get("type")
                if t == "token":
                    parts.append(ev.get("text", ""))
                elif t == "citations":
                    citations = ev.get("citations", [])
                try:
                    await ws.send_json(ev)
                except Exception:  # socket closed mid-answer — stop, deny pending consent
                    broker.shutdown()
                    break
        tg.cancel_scope.cancel()  # answer complete — stop the control reader
    return "".join(parts), citations


# ----------------------------- ingestion -----------------------------------
#: Tenant-administrator roles, matching routers/mcp_admin.
_RECONCILE_ADMIN_ROLES = {"administrators", "tenant_admin", "system_admin"}


def _require_admin(request: Request) -> Identity:
    """Tenant administrator, or 403."""
    ident = _identity(request)
    if not (set(ident.roles) & _RECONCILE_ADMIN_ROLES):
        raise HTTPException(status_code=403, detail="tenant administrator required")
    return ident


@router.post("/ingest/reconcile")
def ingest_reconcile(request: Request, tenant: str | None = Query(default=None),
                     mode: str = Query(default="sweep", pattern="^(sweep|full)$"),
                     max_files: int | None = Query(default=None),
                     ident: Identity = Depends(_require_admin)) -> JSONResponse:
    """Trigger a reconcile pass. Tenant administrators only.

    This route previously took no identity at all, while every other route on the
    service takes one. The edge proxies /csai/ as a prefix, so it was reachable
    unauthenticated from the public internet — and with max_files omitted it walks
    the entire corpus synchronously as the indexing agent, which makes an open
    endpoint both a denial-of-service lever and a disclosure of how many documents
    a tenant holds and what state they are in.

    ``mode=sweep`` (default) re-judges the recorded documents against the current
    plugin registry and retries what needs it — bounded by the number of broken
    documents. ``mode=full`` additionally walks the tree to find files that were
    never recorded at all; it is O(corpus) and should carry ``max_files``.

    The tenant is taken from the caller's identity. It was previously a free query
    parameter on an unauthenticated route, so anyone could name any tenant; a
    tenant admin now reconciles their own tenant and no one else's."""
    config = request.app.state.config
    if not _check_core(config):
        return JSONResponse(status_code=503, content={"error": "core not reachable"})
    if tenant and tenant != ident.tenant:
        raise HTTPException(status_code=403, detail="cannot reconcile another tenant")
    from .reconcile import reconcile, sweep
    target = ident.tenant
    counts = (reconcile if mode == "full" else sweep)(config, target, max_files=max_files)
    audit.record(action="reconcile", user=ident.user, tenant=target, result="ok",
                 mode=mode, counts=counts)
    return JSONResponse(status_code=200,
                        content={"tenant": target, "mode": mode, "counts": counts})


@router.post("/documents/{file_uid}/convert")
async def convert_document(file_uid: str, request: Request,
                           identity: Identity = Depends(_identity)) -> JSONResponse:
    """(Re)generate a document's renditions (thumbnail / preview / inline PDF) and
    index it, on demand — e.g. when the SPA opens a file that has no preview yet.

    Indexing and rendering are unconditional system operations: if data is in the
    system it gets indexed and rendered, regardless of ACLs. Conversion runs as the
    indexing agent (system_admin bypass) so it can always read the source and write
    the hidden-child renditions. Per-user permissions are enforced *later*, when
    content is actually served — search/chat retrieval and document text are gated
    as the end user; rendition bytes are gated by the core. So generating a
    rendition here never leaks content; it only requires an authenticated caller."""
    config = request.app.state.config
    if not _check_core(config):
        return JSONResponse(status_code=503, content={"error": "core not reachable"})

    # Ensure the tenant's schema + tables exist (idempotent) before converting —
    # a never-indexed tenant would otherwise hit "relation documents does not
    # exist" when the pipeline reads prior status.
    from .db import provision_tenant
    await run_in_threadpool(provision_tenant, config, identity.tenant)

    # Conversion does blocking I/O (gRPC + tools + embedding) — off the event loop.
    # force=True: this is an explicit user (re)generate, so run the plugins even
    # if the version was already converted/indexed (e.g. a text file indexed
    # before the preview plugin existed has no renditions yet).
    ing = _ingestor(request.app)
    out = await run_in_threadpool(
        partial(ing.pipeline.convert, force=True), file_uid, identity.tenant)

    # Announce the outcome, exactly as the worker path does.
    #
    # This endpoint used to convert and return in silence, and folder_actions'
    # sorter depends on the announcement: on file.moved with no text yet it calls
    # this endpoint and defers, expecting the ensuing conversion.complete to
    # re-fire the sort. The event never came, so a deferral was a dead end — the
    # file was converted and indexed while the sort that asked for it waited
    # forever. Five files sat in an inbox that way.
    #
    # A conversion that resolves must say so, whichever path ran it.
    await run_in_threadpool(
        ing.emitter.emit_conversion,
        {"tenant": identity.tenant, "actor": identity.user, "file_uid": file_uid},
        out)

    return JSONResponse(status_code=200, content={
        "file_uid": file_uid,
        "status": out.status,
        "renditions": out.renditions_written,
        "has_markdown": out.has_markdown,
    })
