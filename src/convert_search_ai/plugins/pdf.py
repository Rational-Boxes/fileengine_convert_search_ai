"""PDF → page-1 preview image (poppler pdftoppm) + structure/table-preserving
Markdown (see pdf_backends — docling / pymupdf4llm / pdfplumber, with a pdftotext
fallback)."""
from __future__ import annotations

import os
from typing import List, Optional

from .base import ConversionPlugin, Rendition
from .pdf_backends import DEFAULT_ORDER, extract_markdown
from .. import tools


class PdfPlugin(ConversionPlugin):
    name = "pdf"

    def __init__(self, backends: Optional[List[str]] = None):
        # Backend preference order (see pdf_backends). Configurable via
        # CSAI_PDF_BACKENDS so deployments can opt into docling for max fidelity.
        self.backends = list(backends) if backends else list(DEFAULT_ORDER)

    def supports(self, mime: str) -> bool:
        return mime == "application/pdf"

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        if not tools.have("pdftoppm") or not data:
            return []
        with tools.workdir() as d:
            src = tools.write_temp(d, "in.pdf", data)
            base = os.path.join(d, "preview")
            ok = tools.run(["pdftoppm", "-png", "-singlefile", "-scale-to", "1024", src, base])
            png = tools.read_if_exists(base + ".png") if ok else None
            return [Rendition("preview", "png", png, "image/png")] if png else []

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        if not data:
            return None
        # Structure- and table-preserving Markdown via the configured backends.
        return extract_markdown(data, self.backends)
