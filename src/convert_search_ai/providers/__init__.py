"""Pluggable AI providers (DEVELOPMENT_PLAN §7).

Embeddings and chat completion sit behind small interfaces selected by config, so
a deployment chooses concrete providers (Voyage/OpenAI/local for embeddings;
Anthropic Claude for chat) without touching the pipeline. A deterministic offline
``hash`` embedder and an ``echo`` chat provider keep dev/tests dependency-free."""
from .base import ChatProvider, EmbeddingProvider
from .factory import make_chat_provider, make_embedding_provider

__all__ = [
    "ChatProvider", "EmbeddingProvider",
    "make_chat_provider", "make_embedding_provider",
]
