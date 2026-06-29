"""Formatted preview for Markdown files.

A ``.md`` file should preview as the *rendered document* — headings, lists,
emphasis, links and code blocks — not as raw source. This plugin parses the
Markdown and renders a **formatted** PDF with reportlab (Platypus), then reuses
the shared ``page1_previews`` helper (poppler) for the icon/thumbnail PNGs, just
like every other document type. The extracted text (search/RAG) stays the raw
Markdown source.

Registered ahead of the generic source-preview plugin so it claims
``text/markdown``. Pure-Python (reportlab is already a dependency); fail-soft —
if formatted rendering can't run it falls back to the source-style renderer, and
any failure degrades to ``[]`` / the ``pdf`` alone rather than raising."""
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
    # Protect code spans first (no emphasis applies inside them).
    spans: List[str] = []

    def _stash(m: "re.Match[str]") -> str:
        spans.append(_html.escape(m.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_SPAN.sub(_stash, text)
    text = _IMG.sub(lambda m: m.group(1), text)  # images -> their alt text
    text = _html.escape(text)                    # escape &, <, > (markers survive)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}" color="#2563eb">{m.group(1)}</a>', text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f'<font face="Courier">{spans[int(m.group(1))]}</font>', text)


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_ULI = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLI = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*```")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")


def markdown_to_flowables(text: str):
    """Parse Markdown into a list of reportlab flowables (headings, paragraphs,
    lists, code blocks, block quotes, rules)."""
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import HRFlowable, ListFlowable, ListItem, Paragraph, Preformatted, Spacer

    body = ParagraphStyle("md_body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=6, alignment=TA_LEFT)
    quote = ParagraphStyle("md_quote", parent=body, leftIndent=14, textColor="#555555", fontName="Helvetica-Oblique")
    code = ParagraphStyle("md_code", fontName="Courier", fontSize=8.5, leading=11, leftIndent=8, backColor="#f3f4f6")
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
        if _FENCE.match(line):                                   # fenced code block
            i += 1
            buf: List[str] = []
            while i < n and not _FENCE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            flow.append(Preformatted("\n".join(buf) or " ", code))
            continue
        if not line.strip():                                     # blank
            i += 1
            continue
        if _HR.match(line):                                      # horizontal rule
            flow.append(HRFlowable(width="100%", thickness=0.6, color="#cccccc",
                                   spaceBefore=6, spaceAfter=6))
            i += 1
            continue
        hm = _HEADING.match(line)
        if hm:                                                   # heading
            lvl = len(hm.group(1))
            flow.append(Paragraph(inline_markup(hm.group(2).strip()), heads[lvl]))
            i += 1
            continue
        if _QUOTE.match(line):                                   # block quote
            buf = []
            while i < n and _QUOTE.match(lines[i]):
                buf.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            flow.append(Paragraph(inline_markup(" ".join(buf)), quote))
            continue
        if _ULI.match(line) or _OLI.match(line):                 # list (ordered/unordered)
            ordered = bool(_OLI.match(line))
            items = []
            while i < n and (_ULI.match(lines[i]) or _OLI.match(lines[i])):
                m = _OLI.match(lines[i]) or _ULI.match(lines[i])
                items.append(ListItem(Paragraph(inline_markup(m.group(1)), body), leftIndent=18))
                i += 1
            flow.append(ListFlowable(items, bulletType="1" if ordered else "bullet",
                                     bulletFontName="Helvetica", start="1" if ordered else None))
            continue
        # paragraph: gather consecutive "plain" lines
        buf = []
        while i < n and lines[i].strip() and not (
            _FENCE.match(lines[i]) or _HEADING.match(lines[i]) or _HR.match(lines[i])
            or _QUOTE.match(lines[i]) or _ULI.match(lines[i]) or _OLI.match(lines[i])
        ):
            buf.append(lines[i].strip())
            i += 1
        flow.append(Paragraph(inline_markup(" ".join(buf)), body))
    if not flow:
        flow.append(Spacer(1, 1))
    return flow


def render_markdown_pdf(text: str, title: str = "") -> bytes:
    """Render Markdown ``text`` into a formatted PDF. Returns ``b""`` on failure."""
    try:
        from io import BytesIO

        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate

        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter, title=title or "Markdown",
                                leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=42)
        doc.build(markdown_to_flowables(text))
        return buf.getvalue()
    except Exception:
        return b""


class MarkdownPlugin(ConversionPlugin):
    """Formatted PDF + page-1 PNG previews for Markdown (rendered, not raw source)."""

    name = "markdown"

    def __init__(
        self,
        style: str = "default",
        head_lines: int = 200,
        thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
        preview_px: int = DEFAULT_PREVIEW_PX,
    ):
        self.style = style              # used only by the source-style fallback
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
        pdf = render_markdown_pdf(text, name or "Markdown")
        if not pdf:
            # Formatted rendering unavailable — fall back to the source-style PDF
            # so a Markdown file still gets a preview.
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
