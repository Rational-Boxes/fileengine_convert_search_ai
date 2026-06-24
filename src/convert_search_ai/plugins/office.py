"""Office documents → inline PDF rendition + page-1 preview images + extracted
text (LibreOffice headless, then poppler for the previews)."""
from __future__ import annotations

import os
from typing import List, Optional

from .base import ConversionPlugin, Rendition
from .doc_preview import DEFAULT_PREVIEW_PX, DEFAULT_THUMBNAIL_PX, page1_previews
from .. import tools

# MIME -> a source extension LibreOffice recognizes (helps the import filter).
_EXT = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/msword": "doc",
    "application/vnd.ms-excel": "xls",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
}


class OfficePlugin(ConversionPlugin):
    name = "office"

    def __init__(self, pdf_backends=None,
                 thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
                 preview_px: int = DEFAULT_PREVIEW_PX):
        # Office text extraction routes through PDF so tables/structure survive
        # (LibreOffice's plain txt export flattens them); same backend chain as PdfPlugin.
        self.pdf_backends = pdf_backends
        # Page-1 preview sizes (longest edge, px); see CSAI_DOC_*_PX.
        self.thumbnail_px = thumbnail_px
        self.preview_px = preview_px

    def supports(self, mime: str) -> bool:
        return mime in _EXT

    def _binary(self) -> str | None:
        for b in ("soffice", "libreoffice"):
            if tools.have(b):
                return b
        return None

    def _convert(self, data: bytes, src_ext: str, target: str, out_ext: str) -> Optional[bytes]:
        binary = self._binary()
        if not binary or not data:
            return None
        with tools.workdir() as d:
            src = tools.write_temp(d, f"in.{src_ext}", data)
            # A private profile dir avoids clashing with any desktop LibreOffice.
            ok = tools.run(
                [binary, "--headless", f"-env:UserInstallation=file://{d}/profile",
                 "--convert-to", target, "--outdir", d, src],
                timeout=180,
            )
            return tools.read_if_exists(os.path.join(d, f"in.{out_ext}")) if ok else None

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        # One LibreOffice conversion to PDF serves both the inline document
        # preview and (via poppler) the page-1 thumbnail + larger preview images.
        pdf = self._convert(data, _EXT.get(mime, "bin"), "pdf", "pdf")
        if not pdf:
            return []
        out = [Rendition("pdf", "pdf", pdf, "application/pdf")]
        out.extend(page1_previews(pdf, self.thumbnail_px, self.preview_px))
        return out

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        # Prefer structure/table-preserving extraction: render to PDF, then run
        # the advanced PDF backends. Fall back to LibreOffice's plain text export.
        pdf = self._convert(data, _EXT.get(mime, "bin"), "pdf", "pdf")
        if pdf:
            from .pdf_backends import extract_markdown
            md = extract_markdown(pdf, self.pdf_backends)
            if md and md.strip():
                return md
        txt = self._convert(data, _EXT.get(mime, "bin"), "txt:Text", "txt")
        return txt.decode("utf-8", "replace") if txt else None
