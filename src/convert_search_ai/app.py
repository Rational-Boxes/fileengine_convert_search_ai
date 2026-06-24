"""FastAPI application for convert_search_ai.

M0: liveness/readiness. M1: POST /ingest/reconcile. M2: bearer-token auth and the
permission-gated search + text-request surface:
  POST /auth/token              - LDAP bind -> bearer token
  GET  /whoami                  - the resolved identity
  POST /search                  - permission-gated full-text + fuzzy search
  GET  /documents/{uid}/text    - extracted Markdown (READ-gated)

M3 adds the WebSocket RAG chat.
"""
from __future__ import annotations

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config, load_dotenv
from .http_auth import extract_tenant, resolve_identity
from .ldap_auth import Identity, authenticate
from .permissions import PermissionGate
from .search import SearchService
from .token_store import TokenStore


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
    """Resolve the requesting user from the Authorization header (Basic or Bearer)
    and the per-session tenant (X-Tenant / Host). 401 if unauthenticated."""
    config: Config = request.app.state.config
    store: TokenStore = request.app.state.token_store
    headers = {k.lower(): v for k, v in request.headers.items()}
    tenant = extract_tenant(headers, headers.get("host", ""), config.tenant)
    ident = resolve_identity(headers.get("authorization", ""), tenant, config, store)
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return ident


def build_app(config: Config | None = None, *, search: SearchService | None = None,
              token_store: TokenStore | None = None,
              enable_event_invalidation: bool = False) -> FastAPI:
    config = config or Config()
    app = FastAPI(title="convert_search_ai", version=__version__)
    app.state.config = config
    app.state.token_store = token_store or TokenStore(ttl_seconds=config.token_ttl)
    gate = PermissionGate(config.permission_cache_ttl)
    app.state.permission_gate = gate
    app.state.search = search or SearchService(config, gate=gate)

    if enable_event_invalidation:
        # Real-time permission-cache invalidation from the event stream (§8).
        from .cache_invalidation import PermissionCacheInvalidator
        app.state.invalidator = PermissionCacheInvalidator(config, gate)
        app.state.invalidator.start_background()

    # ----------------------------- health ---------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "service": "convert_search_ai", "version": __version__}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        checks = {"core": _check_core(config), "ldap": _check_ldap(config)}
        ready = all(checks.values())
        return JSONResponse(status_code=200 if ready else 503,
                            content={"ready": ready, "checks": checks})

    # ------------------------------ auth ----------------------------------
    @app.post("/auth/token")
    def auth_token(body: dict = Body(...)) -> JSONResponse:
        ident = authenticate(config, body.get("username", ""), body.get("password", ""))
        if not ident.authenticated:
            return JSONResponse(status_code=401, content={"error": "invalid credentials"})
        token = app.state.token_store.issue(ident)
        return JSONResponse(status_code=200, content={"access_token": token, "token_type": "bearer"})

    @app.get("/whoami")
    def whoami(identity: Identity = Depends(_identity)) -> dict:
        return {"user": identity.user, "roles": identity.roles, "tenant": identity.tenant}

    # ----------------------------- search ---------------------------------
    @app.post("/search")
    def search_endpoint(body: dict = Body(...), identity: Identity = Depends(_identity)) -> dict:
        query = (body.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        limit = int(body.get("limit", 20))
        fuzzy = bool(body.get("fuzzy", True))
        hits = app.state.search.search(identity, query, limit=limit, fuzzy=fuzzy)
        return {"query": query, "tenant": identity.tenant,
                "hits": [{"file_uid": h.file_uid, "name": h.name,
                          "snippet": h.snippet, "score": h.score} for h in hits]}

    @app.get("/documents/{file_uid}/text")
    def document_text(file_uid: str, identity: Identity = Depends(_identity)) -> dict:
        try:
            text = app.state.search.get_text(identity, file_uid)
        except PermissionError:
            raise HTTPException(status_code=403, detail="not permitted")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="no extracted text for this file")
        return {"file_uid": file_uid, "tenant": identity.tenant, "text": text}

    # --------------------------- ingestion --------------------------------
    @app.post("/ingest/reconcile")
    def ingest_reconcile(
        tenant: str | None = Query(default=None),
        max_files: int | None = Query(default=None),
    ) -> JSONResponse:
        if not _check_core(config):
            return JSONResponse(status_code=503, content={"error": "core not reachable"})
        from .reconcile import reconcile
        counts = reconcile(config, tenant, max_files=max_files)
        return JSONResponse(status_code=200, content={"tenant": tenant or config.tenant, "counts": counts})

    return app


def main() -> None:
    import uvicorn

    load_dotenv()
    config = Config()
    # Enable real-time permission-cache invalidation from the event stream.
    app = build_app(config, enable_event_invalidation=True)
    uvicorn.run(app, host=config.http_host, port=config.http_port)


if __name__ == "__main__":
    main()
