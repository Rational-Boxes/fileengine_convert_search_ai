"""Config-driven provider selection (DEVELOPMENT_PLAN §7)."""
from __future__ import annotations

from .base import ChatProvider, EmbeddingProvider


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
    if name == "openai":
        from .embeddings import OpenAIEmbeddingProvider
        return OpenAIEmbeddingProvider(model=model or "text-embedding-3-small", dimension=dim)
    raise ValueError(f"unknown embedding provider: {name!r}")


def make_chat_provider(config) -> ChatProvider:
    name = (getattr(config, "chat_provider", "") or "anthropic").lower()
    model = getattr(config, "chat_model", "") or "claude-sonnet-4-6"
    if name == "anthropic":
        from .chat import AnthropicChatProvider
        return AnthropicChatProvider(model=model)
    if name in ("echo", "fake"):
        from .chat import EchoChatProvider
        return EchoChatProvider()
    raise ValueError(f"unknown chat provider: {name!r}")
