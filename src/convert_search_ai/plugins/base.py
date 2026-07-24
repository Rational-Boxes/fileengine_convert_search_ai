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

"""Plugin interface + result types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Rendition:
    """One alternate-format copy of a source file, to be stored as a hidden child."""
    fmt: str    # logical kind: "pdf" | "preview" | "thumbnail" | "poster"
    ext: str    # file extension: "pdf" | "png" | "webp" | "mp4"
    data: bytes
    mime: str


@dataclass
class ConversionResult:
    renditions: List[Rendition] = field(default_factory=list)
    markdown: Optional[str] = None   # extracted text content, or None
    supported: bool = True           # False when no plugin handled the MIME type


class ConversionPlugin(ABC):
    """A converter for a family of MIME types.

    ``render`` and ``extract`` must be side-effect-free and degrade gracefully
    (return ``[]`` / ``None``) when their external tool is unavailable — the
    pipeline records partial/unsupported status rather than failing the file."""

    name: str = "plugin"

    @abstractmethod
    def supports(self, mime: str) -> bool:
        ...

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        """Presentation renditions for the source (default: none)."""
        return []

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        """Extracted Markdown/text content for the source (default: none)."""
        return None
