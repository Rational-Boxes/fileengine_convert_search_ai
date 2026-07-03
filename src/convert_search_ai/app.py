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
from .conversations import ConversationStore
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
              conversations: ConversationStore | None = None,
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
        config.bridge_url, config.bridge_introspect_ttl, jwt_secret=config.jwt_secret)
    gate = PermissionGate(config.permission_cache_ttl)
    app.state.permission_gate = gate
    app.state.search = search or SearchService(config, gate=gate)
    # Chat retrieval shares the same gate, so real-time cache invalidation applies.
    app.state.chat = chat or ChatService(config, retriever=Retriever(config, gate=gate))
    app.state.conversations = conversations or ConversationStore(config)

    if enable_event_invalidation:
        from .cache_invalidation import PermissionCacheInvalidator
        app.state.invalidator = PermissionCacheInvalidator(config, gate)
        app.state.invalidator.start_background()

    app.include_router(router)
    return app


def create_app() -> FastAPI:
    """ASGI factory that loads ``./.env`` then builds the app — for launching via
    ``uvicorn convert_search_ai.app:create_app --factory``. The ``convert-search-ai``
    console script (``main``) does the same. ``build_app`` itself stays pure (no
    .env side effects) so tests are hermetic; use this when you don't go through
    ``main``, otherwise CSAI_* settings (e.g. CSAI_CHAT_PROVIDER) won't be read and
    the chat provider falls back to its ``anthropic`` default."""
    load_dotenv()
    return build_app(Config(), enable_event_invalidation=True)


def main() -> None:
    import uvicorn

    # Surface our own INFO logs (the AI-providers banner, etc.) — without this the
    # root logger defaults to WARNING under uvicorn and the banner is dropped.
    logging.basicConfig(level=logging.INFO)

    app = create_app()
    cfg = app.state.config
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port)


if __name__ == "__main__":
    main()
