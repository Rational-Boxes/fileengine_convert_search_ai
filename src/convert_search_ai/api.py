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
  POST /internal/search            likewise for search
  WS   /chat                       permission-scoped RAG chat (streamed)
  POST /ingest/reconcile           trigger a reconcile sweep

build_app() wires the shared services onto app.state and includes this router.
Handlers read those services from request/websocket ``app.state``."""
from __future__ import annotations

import logging
import secrets
from functools import partial

import anyio
# The SUBMODULES, explicitly. `import anyio` alone does not bind them: anyio
# 4.15 stopped importing them from its __init__, so `anyio.from_thread.run(...)`
# raised AttributeError at the first streamed chat token — after the model had
# already been called and paid for. It worked until then only because something
# else in the process happened to have imported them first, which is not a
# guarantee, and stopped being true when the dependency moved.
import anyio.from_thread
import anyio.to_thread
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


@router.post("/internal/search")
def internal_search(request: Request, body: dict = Body(default={}),
                    x_internal_auth: str | None = Header(default=None)) -> dict:
    """Permission-gated search as the principal the caller names.

    Same shape and same trust as ``/internal/documents/{uid}/text`` above: the
    caller says who it is acting for, and the filtering is done here, against the
    core, for that principal. Search is the case where that matters most — a hit
    list is a disclosure in itself, so it is the SearchService's own per-hit
    permission filter that decides what comes back, not the caller's good
    intentions."""
    config: Config = request.app.state.config
    _require_internal(config, x_internal_auth)

    user = (body or {}).get("user") or ""
    tenant = (body or {}).get("tenant") or ""
    roles = list((body or {}).get("roles") or [])
    if not user or not tenant:
        raise HTTPException(status_code=400, detail="user and tenant are required")
    identity = Identity(user=user, roles=roles, tenant=tenant, authenticated=True)
    try:
        hits = request.app.state.search.search(
            identity, (body or {}).get("query", ""),
            limit=int((body or {}).get("limit", 20)),
            fuzzy=bool((body or {}).get("fuzzy", True)))
    except GuardError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"query": ((body or {}).get("query") or "").strip(), "tenant": tenant,
            "hits": [{"file_uid": h.file_uid, "name": h.name, "snippet": h.snippet,
                      "score": h.score} for h in hits]}


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
    tenant admin now reconciles their own tenant and no one else's.

    Both modes honour ``CSAI_RECONCILE_MAX_BYTES``: this runs in the same process
    that serves this request, and a document large enough to kill the worker
    would kill the API here just as readily. To convert one oversized file
    deliberately, use ``POST /documents/{file_uid}/convert``, which is the
    on-demand path and carries no limit."""
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


# ------------------------------- media publishing ---------------------------
#
# MEDIA_SHARE.md §4.6. The full-length, web-playable renditions are produced ON
# REQUEST TO PUBLISH — normally by share_service when a media link is minted
# (MS3), or directly here, which is also the retry / re-encode path. Never on
# ingest (§4.3). Unlike /convert, which renders as the agent for anyone signed
# in, publishing writes a child and spends the tenant's quota, so it requires
# WRITE on the file AS THE CALLER (§14-Q6, settled).

def _media_jobs(app):
    jobs = getattr(app.state, "media_jobs", None)
    if jobs is None:
        from .media_jobs import PostgresMediaJobStore
        jobs = PostgresMediaJobStore(app.state.config)
        app.state.media_jobs = jobs
    return jobs


def _caller_client(identity: Identity, config: Config):
    from . import core_client
    return core_client.client_for(identity, config)


def _may(mf, identity: Identity, file_uid: str, perm: str) -> bool:
    """The permission AND existence. The core grants READ by default to a uid
    with no matching rule — including one that does not exist — so the bare
    check would answer yes for a deleted file. Fail closed on any error."""
    try:
        return bool(mf.check_permission(file_uid, perm, tenant=identity.tenant)
                    and mf.entity_exists(file_uid))
    except Exception:
        return False


def _media_source(request: Request, mf, identity: Identity, file_uid: str):
    """(info, mime) for the file, or an HTTPException. The MIME is the one the
    ingest worker sniffed from the content when there is one, else judged from
    the name — publishing is not worth reading the whole file to decide."""
    from . import mime as mimelib
    try:
        info = mf.stat(file_uid, tenant=identity.tenant)
    except Exception:
        raise HTTPException(status_code=404, detail="no such file")
    if getattr(info, "is_dir", False):
        raise HTTPException(status_code=400, detail="a folder cannot be published")
    mime = ""
    try:
        doc = _ingestor(request.app).store.get_status(identity.tenant, file_uid)
        mime = (doc.mime or "") if doc else ""
    except Exception:
        mime = ""
    if not (mime.startswith("video/") or mime.startswith("audio/")):
        mime = mimelib.detect(b"", info.name)
    return info, mime


def _published_names(request: Request, identity: Identity, file_uid: str, version: str) -> list:
    from .renditions import PUBLISHED_FMTS, parse_rendition_name
    try:
        names = _ingestor(request.app).pipeline.writer.names_for_version(
            file_uid, version, identity.tenant)
    except Exception:
        return []
    out = []
    for n in names:
        parsed = parse_rendition_name(n)
        if parsed and parsed[1] in PUBLISHED_FMTS:
            out.append(n)
    return out


async def _queue_publish(request: Request, mf, identity: Identity, file_uid: str,
                         requested_profile: str | None) -> dict:
    """Queue publication of the file's CURRENT version. Shared by the caller-facing
    route and share_service's internal republish, so the two cannot drift on
    what a publish is."""
    from .media_encode import MediaSettings, PROFILE_OUTPUT, profiles_for
    config = request.app.state.config
    info, mime = await run_in_threadpool(_media_source, request, mf, identity, file_uid)
    wanted = profiles_for(mime, MediaSettings.from_config(config))
    if not wanted:
        raise HTTPException(status_code=415, detail=f"{mime or 'this file'} is not audio or video")
    if requested_profile:
        if requested_profile not in PROFILE_OUTPUT or requested_profile not in wanted:
            raise HTTPException(status_code=400,
                                detail=f"profile {requested_profile!r} does not apply to {mime}")
        wanted = [requested_profile]

    from .db import provision_tenant
    await run_in_threadpool(provision_tenant, config, identity.tenant)
    jobs = _media_jobs(request.app)
    version = getattr(info, "version", "") or ""
    out = []
    for profile in wanted:
        job, created = await run_in_threadpool(jobs.request, identity.tenant, file_uid,
                                               version, profile, identity.user)
        out.append({**job.to_api(), "created": created})
    return {"file_uid": file_uid, "source_version": version, "mime": mime,
            "jobs": out, "profiles": wanted}


@router.post("/documents/{file_uid}/media")
async def publish_media(file_uid: str, request: Request, body: dict = Body(default={}),
                        identity: Identity = Depends(_identity)) -> JSONResponse:
    """Request publication of the file's CURRENT version. 202 with the jobs —
    existing or new; asking twice never queues twice. Never blocks on the encode."""
    config = request.app.state.config
    if not getattr(config, "media_enabled", True):
        raise HTTPException(status_code=404, detail="media publishing is disabled")
    mf = _caller_client(identity, config)
    if not await run_in_threadpool(_may, mf, identity, file_uid, "w"):
        raise HTTPException(status_code=403, detail="publishing requires write access to the file")
    out = await _queue_publish(request, mf, identity, file_uid, (body or {}).get("profile"))
    audit.record(action="media_publish_requested", user=identity.user, tenant=identity.tenant,
                 result="success", file_uid=file_uid, version=out["source_version"],
                 profiles=",".join(out.pop("profiles")))
    return JSONResponse(status_code=202, content=out)


@router.post("/internal/documents/{file_uid}/media")
async def internal_republish_media(file_uid: str, request: Request, body: dict = Body(default={}),
                                   x_internal_auth: str | None = Header(default=None)) -> JSONResponse:
    """share_service: a file with a LIVE media link has a new version — publish it
    (MEDIA_SHARE.md §6.2 rule 3: a link plays the newest version that has finished
    publishing, so a correction must be published to be seen).

    Body: ``{"tenant", "user", "roles", "link_uid"}`` — ``user`` is the LINK'S
    CREATOR, on whose authority the link already serves this file.

    Gated on READ as that creator, not WRITE. The caller-facing route asks for
    WRITE because publishing spends the tenant's CPU and storage on a file
    nobody has shared; here the spending was decided when the link was minted,
    and the link already exposes this file's newest version by design. Re-checking
    READ means a creator who has lost access cannot keep a link fed."""
    config: Config = request.app.state.config
    _require_internal(config, x_internal_auth)
    if not getattr(config, "media_enabled", True):
        raise HTTPException(status_code=404, detail="media publishing is disabled")
    b = body or {}
    user, tenant = b.get("user") or "", b.get("tenant") or ""
    if not user or not tenant:
        raise HTTPException(status_code=400, detail="user and tenant are required")
    identity = Identity(user=user, roles=list(b.get("roles") or []), tenant=tenant,
                        authenticated=True)
    mf = _caller_client(identity, config)
    if not await run_in_threadpool(_may, mf, identity, file_uid, "r"):
        raise HTTPException(status_code=403, detail="the link's creator can no longer read this file")
    out = await _queue_publish(request, mf, identity, file_uid, None)
    audit.record(action="media_publish_requested", user=user, tenant=tenant, result="success",
                 file_uid=file_uid, version=out["source_version"],
                 profiles=",".join(out.pop("profiles")), via="share_service",
                 link_uid=b.get("link_uid") or "")
    return JSONResponse(status_code=202, content=out)


@router.get("/documents/{file_uid}/media")
async def media_state(file_uid: str, request: Request,
                      identity: Identity = Depends(_identity)) -> dict:
    """The publish state of the file's current version: its jobs and the
    published renditions present. READ-gated."""
    config = request.app.state.config
    mf = _caller_client(identity, config)
    gate = request.app.state.permission_gate
    if not await run_in_threadpool(gate.can_read, mf, identity, file_uid):
        raise HTTPException(status_code=403, detail="not permitted")
    info, mime = await run_in_threadpool(_media_source, request, mf, identity, file_uid)
    version = getattr(info, "version", "") or ""
    from .db import provision_tenant
    await run_in_threadpool(provision_tenant, config, identity.tenant)
    jobs = await run_in_threadpool(_media_jobs(request.app).for_file, identity.tenant,
                                   file_uid, version)
    return {"file_uid": file_uid, "source_version": version, "mime": mime,
            "jobs": [j.to_api() for j in jobs],
            "renditions": await run_in_threadpool(_published_names, request, identity,
                                                  file_uid, version)}


@router.delete("/documents/{file_uid}/media")
async def unpublish_media(file_uid: str, request: Request,
                          identity: Identity = Depends(_identity)) -> JSONResponse:
    """Cancel the file's queued and running publish jobs, and — when no live media
    link plays the file — remove its published renditions. WRITE-gated.

    Removing a published copy needs share_service to say POSITIVELY that no live
    media link plays this file (§6.2): unpublishing would otherwise silently break
    every embed on a customer's site. Unreachable, unconfigured or unintelligible
    is answered 409 — the safe direction."""
    from .media_worker import ShareRefs
    from .renditions import PUBLISHED_FMTS, parse_rendition_name
    config = request.app.state.config
    mf = _caller_client(identity, config)
    if not await run_in_threadpool(_may, mf, identity, file_uid, "w"):
        raise HTTPException(status_code=403, detail="unpublishing requires write access to the file")
    from .db import provision_tenant
    await run_in_threadpool(provision_tenant, config, identity.tenant)
    cancelled = await run_in_threadpool(_media_jobs(request.app).cancel, identity.tenant, file_uid)

    live = await run_in_threadpool(ShareRefs(config).live_links, identity.tenant, file_uid)
    removed: list = []
    if live == 0:
        # The agent removes them, as it wrote them: renditions are the service's
        # children, and the caller's WRITE on the source is what was checked.
        agent = _ingestor(request.app).pipeline.writer.mf
        for e in await run_in_threadpool(lambda: agent.dir(file_uid, tenant=identity.tenant) or []):
            parsed = parse_rendition_name(getattr(e, "name", ""))
            if parsed and parsed[1] in PUBLISHED_FMTS:
                await run_in_threadpool(agent.remove, e.uid, tenant=identity.tenant)
                removed.append(e.name)
    if cancelled or removed:
        audit.record(action="media_unpublished", user=identity.user, tenant=identity.tenant,
                     result="success", file_uid=file_uid, jobs=len(cancelled),
                     removed=",".join(removed))
    if live is None:
        kept = ("published copies are kept until it can be confirmed that no live share "
                "link depends on them (share_service did not answer)")
    elif live:
        kept = f"{live} live share link(s) play this file; revoke them to remove its published copies"
    else:
        kept = ""
    if cancelled or removed:
        body = {"file_uid": file_uid, "cancelled": [j.to_api() for j in cancelled],
                "removed": removed}
        if kept:
            body["kept"] = kept
        return JSONResponse(status_code=200, content=body)
    if kept:
        raise HTTPException(status_code=409, detail=kept)
    raise HTTPException(status_code=404, detail="nothing is published or being published")


