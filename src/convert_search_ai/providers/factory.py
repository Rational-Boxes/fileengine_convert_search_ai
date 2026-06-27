"""Config-driven provider selection (DEVELOPMENT_PLAN §7).

The ``openai`` / ``ollama`` / ``openai-compatible`` providers all speak the OpenAI
API, so any OpenAI-compatible endpoint works by setting ``*_BASE_URL`` (``ollama``
just defaults the base URL to a local Ollama)."""
from __future__ import annotations

from .base import ChatProvider, EmbeddingProvider, WebSearchProvider

_OPENAI_COMPATIBLE = ("openai", "ollama", "openai-compatible")
_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"


def make_embedding_provider(config) -> EmbeddingProvider:
    name = (getattr(config, "embedding_provider", "") or "hash").lower()
    dim = getattr(config, "embedding_dimension", 1024)
    model = getattr(config, "embedding_model", "") or None
    if name in ("", "hash", "local"):
        from .embeddings import HashEmbeddingProvider
        return HashEmbeddingProvider(dimension=dim)
    if name == "voyage":
        from .embeddings import VoyageEmbeddingProvider
        return VoyageEmbeddingProvider(model=model or "voyage-3", dimension=dim)
    if name in _OPENAI_COMPATIBLE:
        from .embeddings import OpenAIEmbeddingProvider
        base_url = getattr(config, "embedding_base_url", "") or (
            _OLLAMA_DEFAULT_BASE_URL if name == "ollama" else None)
        return OpenAIEmbeddingProvider(
            model=model or "text-embedding-3-small", dimension=dim,
            api_key=getattr(config, "embedding_api_key", "") or None, base_url=base_url,
            send_dimensions=getattr(config, "embedding_send_dimensions", False))
    raise ValueError(f"unknown embedding provider: {name!r}")


def make_chat_provider(config) -> ChatProvider:
    name = (getattr(config, "chat_provider", "") or "anthropic").lower()
    model = getattr(config, "chat_model", "") or "claude-sonnet-4-6"
    if name == "anthropic":
        from .chat import AnthropicChatProvider
        return AnthropicChatProvider(model=model)
    if name in _OPENAI_COMPATIBLE:
        from .chat import OpenAICompatibleChatProvider
        base_url = getattr(config, "chat_base_url", "") or (
            _OLLAMA_DEFAULT_BASE_URL if name == "ollama" else None)
        return OpenAICompatibleChatProvider(
            model=model, base_url=base_url, api_key=getattr(config, "chat_api_key", "") or None)
    if name in ("echo", "fake"):
        from .chat import EchoChatProvider
        return EchoChatProvider()
    raise ValueError(f"unknown chat provider: {name!r}")


def make_web_search_provider(config) -> WebSearchProvider:
    """Select the chat ``web_search`` backend (WEB_SEARCH_TOOL_PLAN §5). Defaults to
    DuckDuckGo. This only chooses the *backend*; whether the tool is offered to the
    model is gated separately by ``web_search_enabled`` (off by default)."""
    name = (getattr(config, "web_search_provider", "") or "duckduckgo").lower()
    if name in ("duckduckgo", "ddg", "ddgs"):
        from .websearch import DuckDuckGoSearchProvider
        return DuckDuckGoSearchProvider(
            region=getattr(config, "web_region", "wt-wt"),
            safesearch=getattr(config, "web_safesearch", "moderate"),
            timelimit=getattr(config, "web_timelimit", ""),
            timeout_ms=getattr(config, "web_timeout_ms", 4000))
    if name in ("fake", "echo"):
        from .websearch import FakeWebSearchProvider
        return FakeWebSearchProvider()
    if name in ("none", "null", ""):
        from .websearch import NullWebSearchProvider
        return NullWebSearchProvider()
    raise ValueError(f"unknown web search provider: {name!r}")
