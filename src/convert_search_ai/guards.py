"""Pure guardrail helpers — request caps for the search/text/chat surface.

These layer *on top of* the permission gate; they bound resource use (oversized
queries, unbounded result sets, huge text responses, runaway RAG context) but
never grant access the core would deny. Dependency-free so they unit-test without
a live stack."""
from __future__ import annotations

from typing import List, Tuple


class GuardError(Exception):
    """A guardrail rejected the request (e.g. query too long / empty)."""


def check_query(query: str, max_chars: int) -> str:
    """Return the trimmed query, or raise GuardError if empty / over the cap."""
    q = (query or "").strip()
    if not q:
        raise GuardError("query is required")
    if max_chars and len(q) > max_chars:
        raise GuardError(f"query of {len(q)} chars exceeds CSAI_MAX_QUERY_CHARS={max_chars}")
    return q


def cap_limit(requested: int, max_results: int) -> int:
    r = max(1, int(requested))
    return min(r, max_results) if max_results else r


def cap_k(requested: int, max_k: int) -> int:
    r = max(1, int(requested))
    return min(r, max_k) if max_k else r


def trim_context(chunks: list, max_chars: int) -> Tuple[list, bool]:
    """Keep chunks in order until the total text budget is exceeded.

    The first chunk is always kept (even if it alone exceeds the budget) so chat
    never loses all context. Returns ``(kept, trimmed)``."""
    if not max_chars:
        return chunks, False
    kept: List = []
    total = 0
    for c in chunks:
        if kept and total + len(c.text) > max_chars:
            return kept, True
        kept.append(c)
        total += len(c.text)
    return kept, total > max_chars


def cap_text_bytes(text: str, max_bytes: int) -> Tuple[str, bool]:
    """Truncate ``text`` to ``max_bytes`` UTF-8 bytes. Returns ``(text, truncated)``."""
    if not max_bytes:
        return text, False
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", "ignore"), True
