"""Chat provider implementations (lazy external imports)."""
from __future__ import annotations

import os
from typing import Iterator, List, Optional

from .base import ChatProvider


class AnthropicChatProvider(ChatProvider):
    """Claude (Anthropic) — the ecosystem default and reference implementation."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None,
                 max_tokens: int = 1024):
        self.model_id = model
        self.max_tokens = max_tokens
        self._key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        import anthropic
        client = anthropic.Anthropic(api_key=self._key)
        kwargs = dict(model=self.model_id, max_tokens=self.max_tokens, messages=messages)
        if system:
            kwargs["system"] = system
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text


class EchoChatProvider(ChatProvider):
    """Offline dev/test chat: deterministic, no external calls. Echoes the last
    user message and notes whether retrieved context was supplied."""

    model_id = "echo"

    def stream(self, messages: List[dict], *, system: Optional[str] = None) -> Iterator[str]:
        last = ""
        for m in reversed(messages or []):
            if m.get("role") == "user":
                last = m.get("content", "")
                break
        has_ctx = bool(system and "context" in system.lower())
        yield f"[echo{' +context' if has_ctx else ''}] {last}"
