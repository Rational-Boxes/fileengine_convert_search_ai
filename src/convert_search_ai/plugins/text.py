"""Plain-text / Markdown / structured-text → Markdown (no external tools)."""
from __future__ import annotations

from typing import List, Optional

from .base import ConversionPlugin, Rendition

_EXACT = {
    "text/markdown", "text/x-markdown", "text/plain", "text/csv",
    "application/json", "application/xml", "text/xml",
}


class TextMarkdownPlugin(ConversionPlugin):
    """Text-like content is already valid Markdown — decode and pass through.

    No presentation renditions are produced (text needs no preview); the value
    is the extracted content for search/RAG."""

    name = "text"

    def supports(self, mime: str) -> bool:
        return mime.startswith("text/") or mime in _EXACT

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        return []

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        if not data:
            return ""
        return data.decode("utf-8", "replace")
