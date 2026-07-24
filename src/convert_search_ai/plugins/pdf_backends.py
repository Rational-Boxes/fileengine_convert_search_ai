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

"""Advanced PDF → Markdown backends that **retain structure and tables**.

`pdftotext -layout` flattens tables into ragged text — unacceptable when tables
and document structure must survive into Markdown for search/RAG. These backends
emit GitHub-flavored Markdown (headings, lists, and real ``| … |`` tables).

Fidelity-ordered (the configured order is a *preference*; the first backend that
is installed **and** yields content wins, so it degrades cleanly):

| backend       | structure + tables | license        | weight                    |
|---------------|--------------------|----------------|---------------------------|
| ``docling``   | best (layout model)| MIT            | heavy (downloads ML model)|
| ``pymupdf4llm``| very good          | **AGPL** (PyMuPDF) | medium (binary wheel) |
| ``pdfplumber``| solid tables       | MIT            | light (pure-ish)          |
| ``pdftotext`` | none (plain text)  | GPL (poppler)  | tiny — last-resort fallback|

Every backend is lazily imported and fail-soft (returns ``None`` if its library
is absent or errors), so the service runs with whichever are installed."""
from __future__ import annotations

from typing import Callable, List, Optional

from .. import tools


def rows_to_markdown(rows) -> str:
    """Render a table (list of rows of cells) as a GitHub-flavored Markdown table."""
    norm = []
    for row in rows or []:
        if row is None:
            continue
        norm.append([("" if c is None else str(c)).replace("\n", " ").strip() for c in row])
    if not norm:
        return ""
    width = max(len(r) for r in norm)
    norm = [r + [""] * (width - len(r)) for r in norm]

    def line(cells: List[str]) -> str:
        return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

    header, body = norm[0], norm[1:]
    out = [line(header), "| " + " | ".join(["---"] * width) + " |"]
    out += [line(r) for r in body]
    return "\n".join(out)


def _docling(data: bytes) -> Optional[str]:
    from docling.document_converter import DocumentConverter  # lazy
    with tools.workdir() as d:
        path = tools.write_temp(d, "in.pdf", data)
        result = DocumentConverter().convert(path)
        # Docling exports headings, lists, and tables to Markdown.
        return result.document.export_to_markdown()


def _pymupdf4llm(data: bytes) -> Optional[str]:
    import pymupdf4llm  # lazy (PyMuPDF — AGPL)
    with tools.workdir() as d:
        path = tools.write_temp(d, "in.pdf", data)
        return pymupdf4llm.to_markdown(path)


def _pdfplumber(data: bytes) -> Optional[str]:
    import io
    import pdfplumber  # lazy (MIT)
    parts: List[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            tables = page.find_tables()
            # Prose = page text with the table regions removed (no duplication).
            cropped = page
            for t in tables:
                try:
                    cropped = cropped.outside_bbox(t.bbox)
                except Exception:
                    pass
            page_parts: List[str] = []
            text = (cropped.extract_text() or "").strip()
            if text:
                page_parts.append(text)
            for t in tables:
                md = rows_to_markdown(t.extract())
                if md:
                    page_parts.append(md)
            if page_parts:
                parts.append("\n\n".join(page_parts))
    return "\n\n".join(parts) if parts else None


def _pdftotext(data: bytes) -> Optional[str]:
    if not tools.have("pdftotext"):
        return None
    with tools.workdir() as d:
        path = tools.write_temp(d, "in.pdf", data)
        out = tools.run_capture(["pdftotext", "-layout", path, "-"])
        return out.decode("utf-8", "replace") if out else None


BACKENDS: dict[str, Callable[[bytes], Optional[str]]] = {
    "docling": _docling,
    "pymupdf4llm": _pymupdf4llm,
    "pdfplumber": _pdfplumber,
    "pdftotext": _pdftotext,
}

# Fidelity-first default. Advanced backends activate only if installed; otherwise
# the chain falls through to pdftotext (always available with poppler).
DEFAULT_ORDER = ["docling", "pymupdf4llm", "pdfplumber", "pdftotext"]


def extract_markdown(data: bytes, order: Optional[List[str]] = None) -> Optional[str]:
    """Try each configured backend in order; return the first non-empty Markdown."""
    for name in (order or DEFAULT_ORDER):
        fn = BACKENDS.get(name)
        if fn is None:
            continue
        try:
            md = fn(data)
        except Exception:
            md = None  # library missing or conversion failed — try the next
        if md and md.strip():
            return md
    return None
