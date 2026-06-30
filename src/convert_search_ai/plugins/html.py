"""HTML documents → inline PDF rendition + page-1 preview images + extracted text.

A full-document HTML file (incl. chat-generated reports the SAVE_REPORT markers
writes) is rendered to PDF so it gets the same inline preview surface as PDF and
Office files. Two backends, fidelity-ordered:

- **Chromium headless** (``--print-to-pdf``) — full modern CSS, the default.
- **LibreOffice** (``--convert-to pdf``) — the dependency-light fallback.

Everything is fail-soft: missing tools yield ``[]``/``None`` like the other
plugins. Text for search is extracted by stripping tags (no dependency)."""
from __future__ import annotations

import html as _htmllib
import os
import re
from typing import List, Optional

from .base import ConversionPlugin, Rendition
from .doc_preview import DEFAULT_PREVIEW_PX, DEFAULT_THUMBNAIL_PX, page1_previews
from .. import tools

_MIMES = frozenset({"text/html", "application/xhtml+xml"})

# Drop entire non-content elements before stripping tags, so script/style bodies
# never leak into the search text.
_DROP_BLOCKS = re.compile(r"(?is)<(script|style|head|noscript)\b.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\f\v]+")
_BLANKS = re.compile(r"\n\s*\n\s*")
_TITLE = re.compile(r"(?is)<title\b[^>]*>(.*?)</title>")


def html_to_text(data: bytes) -> Optional[str]:
    """Human-readable text from an HTML document (tags stripped, entities decoded)
    for FTS + vector search. ``None`` when there is nothing meaningful."""
    if not data:
        return None
    doc = data.decode("utf-8", "replace")
    title = ""
    m = _TITLE.search(doc)
    if m:
        title = _htmllib.unescape(_TAG.sub("", m.group(1))).strip()
    body = _DROP_BLOCKS.sub(" ", doc)
    # Turn block-ish breaks into newlines so the text keeps some structure.
    body = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)\s*/?>", "\n", body)
    text = _htmllib.unescape(_TAG.sub("", body))
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text).strip()
    if not text:
        return None
    return f"# {title}\n\n{text}" if title else text


class HtmlPlugin(ConversionPlugin):
    name = "html"

    def __init__(self, *, chromium: str = "chromium-browser", pdf_backends=None,
                 thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
                 preview_px: int = DEFAULT_PREVIEW_PX, timeout_s: int = 60):
        self.chromium = chromium or "chromium-browser"
        self.pdf_backends = pdf_backends
        self.thumbnail_px = thumbnail_px
        self.preview_px = preview_px
        self.timeout_s = timeout_s

    def supports(self, mime: str) -> bool:
        return mime in _MIMES

    # --- geometry-free renditions ---------------------------------------- #

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        if not data:
            return []
        pdf = self._to_pdf(data)
        if not pdf:
            return []
        out = [Rendition("pdf", "pdf", pdf, "application/pdf")]
        out.extend(page1_previews(pdf, self.thumbnail_px, self.preview_px))
        return out

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        try:
            return html_to_text(data)
        except Exception:
            return None

    # --- HTML → PDF backends --------------------------------------------- #

    def _to_pdf(self, data: bytes) -> Optional[bytes]:
        return self._chromium_pdf(data) or self._libreoffice_pdf(data)

    def _chromium_pdf(self, data: bytes) -> Optional[bytes]:
        if not tools.have(self.chromium):
            return None
        with tools.workdir() as d:
            src = tools.write_temp(d, "in.html", data)
            out = os.path.join(d, "out.pdf")
            # A private user-data-dir keeps each run isolated; --headless=new is the
            # current headless mode; --no-pdf-header-footer drops the date/URL chrome.
            ok = tools.run(
                [self.chromium, "--headless=new", "--no-sandbox", "--disable-gpu",
                 f"--user-data-dir={os.path.join(d, 'profile')}",
                 "--no-pdf-header-footer", f"--print-to-pdf={out}",
                 "file://" + src],
                timeout=self.timeout_s,
            )
            pdf = tools.read_if_exists(out) if ok else None
            return pdf if pdf and pdf[:4] == b"%PDF" else None

    def _libreoffice_pdf(self, data: bytes) -> Optional[bytes]:
        binary = next((b for b in ("soffice", "libreoffice") if tools.have(b)), None)
        if not binary:
            return None
        with tools.workdir() as d:
            src = tools.write_temp(d, "in.html", data)
            ok = tools.run(
                [binary, "--headless", f"-env:UserInstallation=file://{d}/profile",
                 "--convert-to", "pdf", "--outdir", d, src],
                timeout=max(self.timeout_s, 120),
            )
            pdf = tools.read_if_exists(os.path.join(d, "in.pdf")) if ok else None
            return pdf if pdf and pdf[:4] == b"%PDF" else None
