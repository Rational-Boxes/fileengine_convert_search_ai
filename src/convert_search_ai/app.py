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

import logging

from fastapi import FastAPI

from . import __version__, audit
from .api import router
from .chat import ChatService
from .config import Config, load_dotenv
from .permissions import PermissionGate
from .retrieval import Retriever
from .search import SearchService
from .token_store import TokenStore

log = logging.getLogger("convert_search_ai.app")

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"


def _endpoint_desc(provider: str, base_url: str) -> str:
    """Human-readable endpoint for the startup banner (no secrets)."""
    p = (provider or "").lower()
    if base_url:
        return base_url
    if p == "ollama":
        return _OLLAMA_DEFAULT_BASE_URL
    if p == "anthropic":
        return "anthropic-api"
    if p == "voyage":
        return "voyage-api"
    if p in ("openai", "openai-compatible"):
        return "openai-api"
    if p in ("", "hash", "local", "echo", "fake"):
        return "local/offline"
    return "default"


def _log_ai_config(config: Config) -> None:
    """Log the resolved embedder and chat providers so operators can confirm the
    dual configuration took effect — e.g. a CPU-local embedder and an external
    chat LLM are independent and may target different providers/endpoints."""
    emb_provider = config.embedding_provider or "hash"
    chat_provider = config.chat_provider or "anthropic"
    log.info(
        "AI providers — embeddings: provider=%s model=%s dim=%s endpoint=%s | "
        "chat: provider=%s model=%s endpoint=%s",
        emb_provider, config.embedding_model or "(provider default)",
        config.embedding_dimension, _endpoint_desc(emb_provider, config.embedding_base_url),
        chat_provider, config.chat_model, _endpoint_desc(chat_provider, config.chat_base_url),
    )


def build_app(config: Config | None = None, *, search: SearchService | None = None,
              chat: ChatService | None = None, token_store: TokenStore | None = None,
              enable_event_invalidation: bool = False) -> FastAPI:
    config = config or Config()
    audit.configure(config.audit_log_file)
    _log_ai_config(config)
    app = FastAPI(title="convert_search_ai", version=__version__)

    # Browser CORS for a SPA on another origin (off unless CSAI_CORS_ORIGINS set).
    # Explicit origins (never "*") so credentialed requests with the bearer token
    # + X-Tenant header are allowed. The /chat WebSocket isn't governed by CORS.
    if config.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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
