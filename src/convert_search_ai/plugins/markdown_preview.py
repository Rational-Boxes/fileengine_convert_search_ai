"""Formatted preview for Markdown files.

A ``.md`` file should preview as the *rendered document* — headings, lists,
emphasis, links and code blocks — not as raw source. This plugin renders Markdown
to a **formatted** PDF with reportlab (sans-serif body), then reuses the shared
``page1_previews`` helper (poppler) for the icon/thumbnail PNGs like every other
document type. The extracted text (search/RAG) stays the raw Markdown source.

Fenced code blocks are run through the **source-code formatter** (Pygments — the
same engine the source preview uses) so they render **syntax-highlighted** and
**preformatted** (monospace, whitespace preserved). Pygments + reportlab are core
dependencies; fail-soft — any failure degrades to the source-style PDF, then to
``[]``/the ``pdf`` alone, rather than raising.

Registered ahead of the generic source-preview plugin so it claims
``text/markdown``."""
from __future__ import annotations

import html as _html
import re
from typing import List, Optional

from .base import ConversionPlugin, Rendition
from .doc_preview import DEFAULT_PREVIEW_PX, DEFAULT_THUMBNAIL_PX, page1_previews

_MD_MIMES = frozenset({"text/markdown", "text/x-markdown"})

# --- inline Markdown -> reportlab mini-markup -------------------------------

_CODE_SPAN = re.compile(r"`([^`]+)`")
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)")


def inline_markup(text: str) -> str:
    """Convert inline Markdown (bold/italic/code/links) into the small HTML-ish
    markup reportlab's ``Paragraph`` understands, XML-escaping everything else."""
    spans: List[str] = []

    def _stash(m: "re.Match[str]") -> str:
        spans.append(_html.escape(m.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_SPAN.sub(_stash, text)          # protect code spans (no emphasis inside)
    text = _IMG.sub(lambda m: m.group(1), text)  # images -> alt text
    text = _html.escape(text)                    # escape &, <, > (markers survive)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}" color="#2563eb">{m.group(1)}</a>', text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f'<font face="Courier">{spans[int(m.group(1))]}</font>', text)


# --- fenced code blocks -> Pygments-highlighted reportlab flowable -----------

def _code_lexer(lang: str, code: str):
    from pygments.lexers import get_lexer_by_name, guess_lexer
    from pygments.lexers.special import TextLexer
    from pygments.util import ClassNotFound

    if lang:
        try:
            return get_lexer_by_name(lang)
        except ClassNotFound:
            pass
    try:
        return guess_lexer(code)
    except ClassNotFound:
        return TextLexer()


def highlight_code_markup(code: str, lang: str, style_name: str = "default") -> str:
    """Syntax-highlight ``code`` with Pygments into reportlab ``XPreformatted``
    markup: each token wrapped in ``<font color=…>`` (bold where the style says
    so). XML-escaped so the markup stays well-formed. Whitespace is preserved."""
    from xml.sax.saxutils import escape

    from pygments import lex
    from pygments.styles import get_style_by_name

    try:
        style = get_style_by_name(style_name)
    except Exception:
        style = get_style_by_name("default")
    lexer = _code_lexer(lang, code)

    parts: List[str] = []
    for tok, val in lex(code, lexer):
        if not val:
            continue
        seg = escape(val)
        s = style.style_for_token(tok)
        color = s.get("color")
        if color:
            seg = f'<font color="#{color}">{seg}</font>'
        if s.get("bold"):
            seg = f"<b>{seg}</b>"
        parts.append(seg)
    return "".join(parts).rstrip("\n")


# --- GFM tables -------------------------------------------------------------

def _table_cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_separator(line: str) -> bool:
    if "-" not in line:
        return False
    cells = _table_cells(line)
    return bool(cells) and all(re.match(r"^:?-{1,}:?$", c) for c in cells if c != "")


def _build_table(header: List[str], rows: List[List[str]], aligns: List[str], body_style):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle

    ncols = max([len(header)] + [len(r) for r in rows])
    cell = ParagraphStyle("md_td", parent=body_style, fontSize=9, leading=11, spaceAfter=0)
    hcell = ParagraphStyle("md_th", parent=cell, fontName="Helvetica-Bold")

    def _row(cells, st):
        cells = cells + [""] * (ncols - len(cells))
        return [Paragraph(inline_markup(c), st) for c in cells]

    data = [_row(header, hcell)] + [_row(r, cell) for r in rows]
    usable = 612.0 - 84.0  # letter width minus the document margins
    table = Table(data, colWidths=[usable / ncols] * ncols)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for ci, a in enumerate(aligns[:ncols]):
        style.append(("ALIGN", (ci, 0), (ci, -1), a))
    table.setStyle(TableStyle(style))
    return table


def markdown_to_flowables(text: str, style_name: str = "default"):
    """Parse Markdown into reportlab flowables: headings, paragraphs, lists, block
    quotes, rules, and **syntax-highlighted** fenced code blocks."""
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        HRFlowable, ListFlowable, ListItem, Paragraph, Preformatted, Spacer, XPreformatted,
    )

    body = ParagraphStyle("md_body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=6, alignment=TA_LEFT)
    quote = ParagraphStyle("md_quote", parent=body, leftIndent=14, textColor="#555555", fontName="Helvetica-Oblique")
    code = ParagraphStyle("md_code", fontName="Courier", fontSize=8.5, leading=11,
                          leftIndent=8, spaceBefore=4, spaceAfter=6, backColor="#f3f4f6")
    sizes = {1: 18, 2: 15, 3: 13, 4: 12, 5: 11, 6: 10}
    heads = {
        lvl: ParagraphStyle(f"md_h{lvl}", fontName="Helvetica-Bold", fontSize=sz,
                            leading=sz + 4, spaceBefore=10, spaceAfter=4)
        for lvl, sz in sizes.items()
    }

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    flow: list = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        fence = re.match(r"^\s*```+\s*([\w+-]*)", line)
        if fence:                                                # fenced code block
            lang = fence.group(1)
            i += 1
            buf: List[str] = []
            while i < n and not re.match(r"^\s*```+\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            src = "\n".join(buf)
            try:
                flow.append(XPreformatted(highlight_code_markup(src, lang, style_name) or " ", code))
            except Exception:
                flow.append(Preformatted(src or " ", code))     # plain monospace fallback
            continue
        if not line.strip():
            i += 1
            continue
        if re.match(r"^\s*([-*_])(?:\s*\1){2,}\s*$", line):       # horizontal rule
            flow.append(HRFlowable(width="100%", thickness=0.6, color="#cccccc", spaceBefore=6, spaceAfter=6))
            i += 1
            continue
        hm = re.match(r"^(#{1,6})\s+(.*)$", line)
        if hm:                                                   # heading
            flow.append(Paragraph(inline_markup(hm.group(2).strip()), heads[len(hm.group(1))]))
            i += 1
            continue
        qm = re.match(r"^\s*>\s?(.*)$", line)
        if qm:                                                   # block quote
            buf = [qm.group(1)]
            i += 1
            while i < n and re.match(r"^\s*>\s?(.*)$", lines[i]):
                buf.append(re.match(r"^\s*>\s?(.*)$", lines[i]).group(1))
                i += 1
            flow.append(Paragraph(inline_markup(" ".join(buf)), quote))
            continue
        lm = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", line)
        if lm:                                                   # list
            ordered = bool(re.match(r"^\s*\d+[.)]", line))
            items = []
            while i < n:
                m = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if not m:
                    break
                items.append(ListItem(Paragraph(inline_markup(m.group(2)), body), leftIndent=18))
                i += 1
            flow.append(ListFlowable(items, bulletType="1" if ordered else "bullet"))
            continue
        if "|" in line and i + 1 < n and _is_table_separator(lines[i + 1]):  # GFM table
            header = _table_cells(line)
            aligns = []
            for c in _table_cells(lines[i + 1]):
                left, right = c.startswith(":"), c.endswith(":")
                aligns.append("CENTER" if left and right else "RIGHT" if right else "LEFT")
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i] and not re.match(r"^\s*```", lines[i]):
                rows.append(_table_cells(lines[i]))
                i += 1
            flow.append(_build_table(header, rows, aligns, body))
            continue
        # paragraph: gather consecutive plain lines
        buf = []
        while i < n and lines[i].strip() and not (
            re.match(r"^\s*```", lines[i]) or re.match(r"^#{1,6}\s", lines[i])
            or re.match(r"^\s*>\s?", lines[i]) or re.match(r"^\s*([-*+]|\d+[.)])\s", lines[i])
            or re.match(r"^\s*([-*_])(?:\s*\1){2,}\s*$", lines[i])
            or ("|" in lines[i] and i + 1 < n and _is_table_separator(lines[i + 1]))
        ):
            buf.append(lines[i].strip())
            i += 1
        flow.append(Paragraph(inline_markup(" ".join(buf)), body))
    if not flow:
        flow.append(Spacer(1, 1))
    return flow


def render_markdown_pdf(text: str, title: str = "", style_name: str = "default") -> bytes:
    """Render Markdown ``text`` into a formatted PDF (sans body, highlighted code).
    Returns ``b""`` on failure."""
    try:
        from io import BytesIO

        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate

        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter, title=title or "Markdown",
                                leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=42)
        doc.build(markdown_to_flowables(text, style_name))
        return buf.getvalue()
    except Exception:
        return b""


class MarkdownPlugin(ConversionPlugin):
    """Formatted PDF + page-1 PNG previews for Markdown (rendered document with
    syntax-highlighted code, not raw source)."""

    name = "markdown"

    def __init__(
        self,
        style: str = "default",
        head_lines: int = 200,
        thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
        preview_px: int = DEFAULT_PREVIEW_PX,
    ):
        self.style = style              # Pygments style (highlighting + source fallback)
        self.head_lines = head_lines
        self.thumbnail_px = thumbnail_px
        self.preview_px = preview_px

    def supports(self, mime: str) -> bool:
        return mime in _MD_MIMES

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        if not data:
            return []
        text = data.decode("utf-8", "replace")
        if not text.strip():
            return []
        pdf = render_markdown_pdf(text, name or "Markdown", self.style)
        if not pdf:
            # Last resort: the source-style colour-coded PDF.
            from .source_preview import _detect_lexer, render_code_pdf
            lexer = _detect_lexer(text, mime, name)
            pdf = render_code_pdf(text, lexer, self.style, name or "markdown", self.head_lines)
        if not pdf:
            return []
        out = [Rendition(fmt="pdf", ext="pdf", data=pdf, mime="application/pdf")]
        out.extend(page1_previews(pdf, self.thumbnail_px, self.preview_px))
        return out

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        if not data:
            return ""
        return data.decode("utf-8", "replace")
