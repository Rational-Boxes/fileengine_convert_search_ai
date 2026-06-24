"""FastAPI application factory for convert_search_ai.

The HTTP/WebSocket surface is defined in ``api.py`` (one explicit APIRouter);
``build_app`` wires the shared services onto ``app.state`` and includes it:

  state.config           Config
  state.token_store      TokenStore (bearer tokens)
  state.permission_gate  PermissionGate (shared by search + chat retrieval)
  state.search           SearchService   (M2)
  state.chat             ChatService      (M3 RAG)
  state.invalidator      PermissionCacheInvalidator (optional, real-time eviction)
"""
from __future__ import annotations

from fastapi import FastAPI

from . import __version__, audit
from .api import router
from .chat import ChatService
from .config import Config, load_dotenv
from .permissions import PermissionGate
from .retrieval import Retriever
from .search import SearchService
from .token_store import TokenStore


def build_app(config: Config | None = None, *, search: SearchService | None = None,
              chat: ChatService | None = None, token_store: TokenStore | None = None,
              enable_event_invalidation: bool = False) -> FastAPI:
    config = config or Config()
    audit.configure(config.audit_log_file)
    app = FastAPI(title="convert_search_ai", version=__version__)

    app.state.config = config
    app.state.token_store = token_store or TokenStore(ttl_seconds=config.token_ttl)
    # Auth coordination: accept http_bridge bearer tokens via introspection
    # (disabled when CSAI_BRIDGE_URL is unset). One login spans both services.
    from .bridge_auth import BridgeTokenVerifier
    app.state.bridge_verifier = BridgeTokenVerifier(
        config.bridge_url, config.bridge_introspect_ttl)
    gate = PermissionGate(config.permission_cache_ttl)
    app.state.permission_gate = gate
    app.state.search = search or SearchService(config, gate=gate)
    # Chat retrieval shares the same gate, so real-time cache invalidation applies.
    app.state.chat = chat or ChatService(config, retriever=Retriever(config, gate=gate))

    if enable_event_invalidation:
        from .cache_invalidation import PermissionCacheInvalidator
        app.state.invalidator = PermissionCacheInvalidator(config, gate)
        app.state.invalidator.start_background()

    app.include_router(router)
    return app


def main() -> None:
    import uvicorn

    load_dotenv()
    config = Config()
    # Real-time permission-cache invalidation from the event stream (§8).
    app = build_app(config, enable_event_invalidation=True)
    uvicorn.run(app, host=config.http_host, port=config.http_port)


if __name__ == "__main__":
    main()
