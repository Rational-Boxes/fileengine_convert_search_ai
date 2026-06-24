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
