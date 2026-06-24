"""PDF → page-1 preview images (poppler pdftoppm: icon-sized thumbnail + larger
preview) + structure/table-preserving Markdown (see pdf_backends — docling /
pymupdf4llm / pdfplumber, with a pdftotext fallback).

The source PDF is itself the inline document preview, so no ``pdf`` rendition is
emitted here (that would duplicate the source); see OfficePlugin, which converts
non-PDF documents to a ``pdf`` rendition for inline display."""
from __future__ import annotations

from typing import List, Optional

from .base import ConversionPlugin, Rendition
from .doc_preview import DEFAULT_PREVIEW_PX, DEFAULT_THUMBNAIL_PX, page1_previews
from .pdf_backends import DEFAULT_ORDER, extract_markdown


class PdfPlugin(ConversionPlugin):
    name = "pdf"

    def __init__(self, backends: Optional[List[str]] = None,
                 thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
                 preview_px: int = DEFAULT_PREVIEW_PX):
        # Backend preference order (see pdf_backends). Configurable via
        # CSAI_PDF_BACKENDS so deployments can opt into docling for max fidelity.
        self.backends = list(backends) if backends else list(DEFAULT_ORDER)
        # Page-1 preview sizes (longest edge, px); see CSAI_DOC_*_PX.
        self.thumbnail_px = thumbnail_px
        self.preview_px = preview_px

    def supports(self, mime: str) -> bool:
        return mime == "application/pdf"

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        # Icon-sized thumbnail + larger preview of the first page.
        return page1_previews(data, self.thumbnail_px, self.preview_px)

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        if not data:
            return None
        # Structure- and table-preserving Markdown via the configured backends.
        return extract_markdown(data, self.backends)
