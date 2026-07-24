# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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
