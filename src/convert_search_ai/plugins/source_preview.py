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

"""Syntax-highlighted full-document preview for text & source-code formats.

Detects the language with Pygments (C/C++/C#/Java, Python, HTML/CSS/JS/TS, Go,
Rust, Ruby, PHP, SQL, shell, YAML/TOML/JSON/XML, Markdown, ... — the full Pygments
lexer set), renders the **entire** file as a colour-coded, paginated PDF with
reportlab, then reuses the shared ``page1_previews`` helper (poppler ``pdftoppm``)
to emit the icon-sized ``thumbnail`` + larger ``preview`` PNGs of the first page
every other document type gets. The extracted text (for search/RAG) is the
decoded content.

This supersedes the plain ``text`` plugin for the MIME types it claims (it is
registered ahead of it) by adding presentation renditions; it still degrades
gracefully — any failure in detection/rendering yields ``[]`` (or just the
``pdf`` when poppler is absent), never an exception."""
from __future__ import annotations

from typing import List, Optional

from .base import ConversionPlugin, Rendition
from .doc_preview import DEFAULT_PREVIEW_PX, DEFAULT_THUMBNAIL_PX, page1_previews

# Letter page, 1/2-inch margins, monospaced 8.5pt. Courier is metric-identical
# across its bold/oblique variants, so a fixed char width keeps columns aligned.
_PAGE_W, _PAGE_H = 612.0, 792.0
_MARGIN = 36.0
_FONT_SIZE = 8.5
_LEADING = 10.5
_CHAR_W = _FONT_SIZE * 0.6

# text/* is claimed wholesale (covers text/x-python, text/x-c, text/css,
# text/html, text/markdown, text/csv, ...). These are the non-text/* MIME types
# our detector / libmagic emit for source & structured text; the actual lexer is
# still chosen from the filename, which is far more reliable than the MIME type.
_SOURCE_MIMES = frozenset({
    "application/sql", "application/x-sql",
    "application/json", "application/ld+json",
    "application/xml", "application/x-xml",
    "application/javascript", "application/x-javascript", "application/ecmascript",
    "application/typescript", "application/x-typescript",
    "application/x-sh", "application/x-shellscript", "application/x-csh",
    "application/x-python", "application/x-python-code",
    "application/x-perl", "application/x-ruby",
    "application/x-php", "application/x-httpd-php",
    "application/x-yaml", "application/yaml", "application/toml",
    "application/graphql", "application/x-tex", "application/x-latex",
    "application/x-powershell",
})


def _sanitize(s: str) -> str:
    """Reduce to Latin-1 so reportlab's Courier (a Type-1 base-14 font) can draw
    every glyph; unsupported characters become ``?`` rather than raising."""
    return s.encode("latin-1", "replace").decode("latin-1")


def _detect_lexer(code: str, mime: str, name: str):
    """Best lexer for the content: filename first (most reliable — gives correct
    C/TS/SQL/etc.), then MIME type, then content sniffing, then plain text."""
    from pygments.lexers import (
        get_lexer_by_name, get_lexer_for_filename, get_lexer_for_mimetype, guess_lexer,
    )
    from pygments.util import ClassNotFound

    if name:
        try:
            return get_lexer_for_filename(name, code)
        except ClassNotFound:
            pass
    if mime:
        try:
            return get_lexer_for_mimetype(mime)
        except ClassNotFound:
            pass
    try:
        return guess_lexer(code)
    except ClassNotFound:
        return get_lexer_by_name("text")


def _style_font(tok_style) -> str:
    bold, ital = tok_style.get("bold"), tok_style.get("italic")
    if bold and ital:
        return "Courier-BoldOblique"
    if bold:
        return "Courier-Bold"
    if ital:
        return "Courier-Oblique"
    return "Courier"


def render_code_pdf(text: str, lexer, style_name: str, title: str, max_lines: int = 0) -> bytes:
    """A syntax-highlighted PDF of the **entire** file, paginated across as many
    pages as the content needs: a muted ``title · Language`` header + rule on each
    page, then the colour-coded source. Long lines wrap to the next row so nothing
    is lost. ``max_lines`` > 0 caps very large files as a safety valve; ``0`` (the
    default) renders the whole document."""
    from io import BytesIO

    from pygments import lex
    from pygments.styles import get_style_by_name
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas

    # Normalise EOLs + expand tabs. Render the whole file unless a cap is set.
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    if max_lines and max_lines > 0:
        text = "\n".join(text.split("\n")[:max_lines])

    try:
        style = get_style_by_name(style_name)
    except Exception:
        style = get_style_by_name("default")

    max_cols = max(1, int((_PAGE_W - 2 * _MARGIN) / _CHAR_W))
    max_rows = max(1, int((_PAGE_H - 2 * _MARGIN) / _LEADING))
    code_rows = max(1, max_rows - 2)  # reserve two rows for the header + rule

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(_PAGE_W, _PAGE_H))
    black = HexColor("#000000")
    # "·" (U+00B7) is within Latin-1, so Courier renders it (unlike an em dash).
    header = _sanitize(f"{title}  ·  {lexer.name}")[:max_cols]
    state = {"y": 0.0, "row": 0}

    def start_page() -> None:
        top = _PAGE_H - _MARGIN
        c.setFont("Courier-Bold", _FONT_SIZE)
        c.setFillColor(HexColor("#333333"))
        c.drawString(_MARGIN, top - _FONT_SIZE, header)
        sep_y = top - _FONT_SIZE - 4
        c.setStrokeColor(HexColor("#cccccc"))
        c.setLineWidth(0.5)
        c.line(_MARGIN, sep_y, _PAGE_W - _MARGIN, sep_y)
        state["y"] = sep_y - _LEADING
        state["row"] = 0

    def newline() -> None:
        state["row"] += 1
        state["y"] -= _LEADING
        if state["row"] >= code_rows:   # page full → next page
            c.showPage()
            start_page()

    start_page()
    col = 0
    for ttype, value in lex(text, lexer):
        ts = style.style_for_token(ttype)
        hexcol = ts.get("color")
        color = HexColor("#" + hexcol) if hexcol else black
        font = _style_font(ts)
        c.setFillColor(color)
        c.setFont(font, _FONT_SIZE)
        segments = value.split("\n")
        for i, seg in enumerate(segments):
            while seg:                  # wrap long lines instead of clipping them
                space = max_cols - col
                if space <= 0:
                    newline()
                    col = 0
                    # canvas font/colour reset on showPage — restore for this token.
                    c.setFillColor(color)
                    c.setFont(font, _FONT_SIZE)
                    space = max_cols
                chunk = seg[:space]
                if chunk.strip():
                    c.drawString(_MARGIN + col * _CHAR_W, state["y"], _sanitize(chunk))
                col += len(chunk)
                seg = seg[len(chunk):]
            if i < len(segments) - 1:   # a newline terminated this segment
                newline()
                col = 0
                c.setFillColor(color)
                c.setFont(font, _FONT_SIZE)
    c.showPage()
    c.save()
    return buf.getvalue()


class SourcePreviewPlugin(ConversionPlugin):
    """Full-document colour-coded PDF + first-page PNG previews for text/source files."""

    name = "source"

    def __init__(
        self,
        style: str = "default",
        max_lines: int = 0,
        thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
        preview_px: int = DEFAULT_PREVIEW_PX,
    ):
        self.style = style
        # Max source lines rendered into the PDF; 0 = the entire file.
        self.max_lines = max_lines
        self.thumbnail_px = thumbnail_px
        self.preview_px = preview_px

    def supports(self, mime: str) -> bool:
        if not mime:
            return False
        if mime.startswith("text/"):
            return True
        if mime in _SOURCE_MIMES:
            return True
        # Structured-text subtypes: application/<x>+xml | +json (e.g. atom+xml,
        # rss+xml, ld+json). image/svg+xml is image/* so it never reaches here.
        return mime.startswith("application/") and (mime.endswith("+xml") or mime.endswith("+json"))

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        if not data:
            return []
        text = data.decode("utf-8", "replace")
        if not text.strip():
            return []
        lexer = _detect_lexer(text, mime, name)
        pdf = render_code_pdf(text, lexer, self.style, name or "text", self.max_lines)
        if not pdf:
            return []
        out = [Rendition(fmt="pdf", ext="pdf", data=pdf, mime="application/pdf")]
        out.extend(page1_previews(pdf, self.thumbnail_px, self.preview_px))
        return out

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        if not data:
            return ""
        return data.decode("utf-8", "replace")
