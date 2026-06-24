"""Markdown chunking for embedding + retrieval.

Packs Markdown blocks (paragraphs, lists, GFM tables — split on blank lines) into
chunks of a target size with a small overlap so context isn't lost at chunk
boundaries. A single oversized block (e.g. a big table) is kept whole rather than
cut mid-structure."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

_BLANKS = re.compile(r"\n\s*\n")


@dataclass
class Chunk:
    ordinal: int
    text: str


def chunk_markdown(md: str, *, target_chars: int = 1200, overlap_chars: int = 150) -> List[Chunk]:
    text = (md or "").strip()
    if not text:
        return []
    blocks = [b.strip() for b in _BLANKS.split(text) if b.strip()]

    chunks: List[str] = []
    buf = ""
    for b in blocks:
        candidate = (buf + "\n\n" + b) if buf else b
        if buf and len(candidate) > target_chars:
            chunks.append(buf)
            tail = buf[-overlap_chars:] if overlap_chars > 0 else ""
            buf = (tail + "\n\n" + b).strip() if tail else b
        else:
            buf = candidate
    if buf.strip():
        chunks.append(buf.strip())

    return [Chunk(i, c) for i, c in enumerate(chunks)]
