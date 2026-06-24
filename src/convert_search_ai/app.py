"""FastAPI application for convert_search_ai.

M0 exposes only liveness/readiness. The conversion (M1), search (M2), and RAG
chat (M3) surfaces are added on top:
  POST /ingest/reconcile  - trigger a reconcile sweep
  POST /search            - permission-gated full-text + fuzzy search
  GET  /documents/{uid}/text - extracted Markdown
  WS   /chat              - RAG chat-with-documents
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config, load_dotenv


def _check_ldap(config: Config) -> bool:
    """Best-effort: can the agent authenticate against LDAP?"""
    try:
        from .ldap_auth import authenticate
        if not config.agent_user or not config.agent_password:
            return False
        return authenticate(config, config.agent_user, config.agent_password).authenticated
    except Exception:
        return False


def _check_core(config: Config) -> bool:
    """Best-effort: is the gRPC core reachable?"""
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


def build_app(config: Config | None = None) -> FastAPI:
    config = config or Config()
    app = FastAPI(title="convert_search_ai", version=__version__)
    app.state.config = config

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness — the process is up. Cheap, no dependencies."""
        return {"status": "ok", "service": "convert_search_ai", "version": __version__}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness — dependencies reachable. 200 only when core + LDAP are up."""
        checks = {"core": _check_core(config), "ldap": _check_ldap(config)}
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "checks": checks},
        )

    return app


def main() -> None:
    """Console entrypoint: load .env, build the app, serve with uvicorn."""
    import uvicorn

    load_dotenv()
    config = Config()
    uvicorn.run(build_app(config), host=config.http_host, port=config.http_port)


if __name__ == "__main__":
    main()
