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

"""Syntax-highlighted first-page preview for text & source-code formats.

Language detection (Pygments) + the colour-coded PDF (reportlab) are pure-Python
and always exercised; the PNG previews additionally need poppler (pdftoppm) and
degrade to just the ``pdf`` rendition when it is absent.
"""
import struct

import pytest

from convert_search_ai import tools
from convert_search_ai.plugins.registry import default_registry
from convert_search_ai.plugins.source_preview import SourcePreviewPlugin, _detect_lexer


def _png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


SQL = b"SELECT id, name FROM users WHERE active = 1 ORDER BY name;\n-- a comment\n"
PY = b"import os\n\n\ndef main():\n    print('hi')\n"


# --- MIME claims -------------------------------------------------------------

def test_supports_text_source_and_structured_subtypes():
    p = SourcePreviewPlugin()
    for m in ("text/plain", "text/markdown", "text/x-python", "text/css", "text/html",
              "application/sql", "application/x-sh", "application/json", "application/xml",
              "application/javascript", "application/x-yaml", "application/atom+xml",
              "application/ld+json"):
        assert p.supports(m), m
    for m in ("application/pdf", "image/png", "image/svg+xml", "video/mp4",
              "application/octet-stream", ""):
        assert not p.supports(m), m


# --- language detection ("detect source code SQL, etc.") ---------------------

@pytest.mark.parametrize("name,data,lang", [
    ("q.sql", SQL, "SQL"),
    ("a.py", PY, "Python"),
    ("main.c", b"#include <stdio.h>\nint main(){return 0;}\n", "C"),
    ("app.ts", b"const x: number = 1\n", "TypeScript"),
    ("page.html", b"<!doctype html><html></html>", "HTML"),
    ("style.css", b"body { color: red; }\n", "CSS"),
    ("d.json", b'{"a": 1}', "JSON"),
    ("doc.xml", b"<root><a/></root>", "XML"),
    ("s.sh", b"#!/bin/bash\necho hi\n", "Bash"),
])
def test_detects_language_from_filename(name, data, lang):
    # SQL maps to a dialect lexer (Transact-SQL) — match on the family.
    assert lang.lower() in _detect_lexer(data.decode(), "", name).name.lower() or \
        _detect_lexer(data.decode(), "", name).name == lang


# --- rendition output --------------------------------------------------------

def test_render_emits_first_page_pdf_for_source():
    pytest.importorskip("reportlab")
    pytest.importorskip("pygments")
    out = SourcePreviewPlugin().render(SQL, "application/sql", "q.sql")
    pdf = [r for r in out if r.fmt == "pdf"]
    assert len(pdf) == 1
    assert pdf[0].ext == "pdf" and pdf[0].mime == "application/pdf"
    assert pdf[0].data[:5] == b"%PDF-"


def _pdf_page_count(pdf: bytes) -> int:
    # reportlab writes one "/Type /Page" object per page (and "/Type /Pages" once
    # for the tree); count the former.
    return pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")


def test_source_pdf_renders_entire_file_across_pages():
    pytest.importorskip("reportlab")
    pytest.importorskip("pygments")
    from convert_search_ai.plugins.source_preview import render_code_pdf, _detect_lexer
    code = "\n".join(f"row_{i} = step({i})" for i in range(800))
    lexer = _detect_lexer(code, "text/x-python", "big.py")
    pdf = render_code_pdf(code, lexer, "default", "big.py", 0)   # 0 = whole file
    assert pdf[:5] == b"%PDF-"
    assert _pdf_page_count(pdf) > 1                              # paginated, not one page


def test_source_pdf_wraps_long_lines_without_losing_content():
    pytest.importorskip("reportlab")
    pytest.importorskip("pygments")
    from convert_search_ai.plugins.source_preview import render_code_pdf, _detect_lexer
    code = "x = '" + "A" * 5000 + "'\n"          # one very long line
    lexer = _detect_lexer(code, "text/x-python", "long.py")
    pdf = render_code_pdf(code, lexer, "default", "long.py", 0)
    assert pdf[:5] == b"%PDF-"
    assert _pdf_page_count(pdf) >= 1             # wrapped across rows, renders fine


def test_max_lines_caps_the_pdf():
    pytest.importorskip("reportlab")
    pytest.importorskip("pygments")
    from convert_search_ai.plugins.source_preview import render_code_pdf, _detect_lexer
    code = "\n".join(f"row_{i} = step({i})" for i in range(800))
    lexer = _detect_lexer(code, "text/x-python", "big.py")
    full = render_code_pdf(code, lexer, "default", "big.py", 0)
    capped = render_code_pdf(code, lexer, "default", "big.py", 40)
    assert _pdf_page_count(capped) < _pdf_page_count(full)


def test_render_adds_png_previews_when_poppler_present():
    pytest.importorskip("reportlab")
    if not tools.have("pdftoppm"):
        pytest.skip("pdftoppm not installed")
    out = SourcePreviewPlugin().render(PY, "text/x-python", "a.py")
    fmts = {r.fmt for r in out}
    assert {"pdf", "thumbnail", "preview"} <= fmts
    for r in out:
        if r.fmt in ("thumbnail", "preview"):
            assert r.ext == "png" and _png_size(r.data)[0] > 0


def test_render_degrades_to_pdf_only_without_poppler(monkeypatch):
    pytest.importorskip("reportlab")
    monkeypatch.setattr(tools, "have", lambda tool: False)
    out = SourcePreviewPlugin().render(PY, "text/x-python", "a.py")
    assert [r.fmt for r in out] == ["pdf"]


def test_empty_and_blank_input_yield_nothing():
    p = SourcePreviewPlugin()
    assert p.render(b"", "text/plain", "x.txt") == []
    assert p.render(b"   \n\t\n", "text/plain", "x.txt") == []


def test_extract_returns_decoded_text_for_indexing():
    p = SourcePreviewPlugin()
    assert p.extract(SQL, "application/sql", "q.sql") == SQL.decode()
    assert p.extract(b"", "text/plain", "x.txt") == ""


# --- end-to-end dispatch -----------------------------------------------------

def test_registry_routes_sql_to_source_plugin_with_pdf():
    pytest.importorskip("reportlab")
    res = default_registry().convert(SQL, "application/sql", "q.sql")
    assert res.supported
    assert res.markdown == SQL.decode()          # full text still extracted
    assert any(r.fmt == "pdf" for r in res.renditions)


# --- one file cannot hold the ingest queue ----------------------------------
#
# The renderer runs in the ingest worker, one document at a time. A 12 MiB
# text file with no line breaks (share_service's live tests upload one) is a
# single source line, so CSAI_CODE_PREVIEW_MAX_LINES cannot bound it; it wrapped
# to thousands of pages, and the wrap loop re-sliced the remaining line on every
# row — quadratic, 37 s of a 43 s render even once the pages were capped.

def _pdf_pages(renditions):
    import io
    from pypdf import PdfReader
    pdf = next(r for r in renditions if r.fmt == "pdf").data
    return PdfReader(io.BytesIO(pdf)).pages


def test_a_huge_single_line_file_is_capped_by_pages_and_says_so():
    import time
    from convert_search_ai.plugins.source_preview import SourcePreviewPlugin
    t = time.monotonic()
    out = SourcePreviewPlugin(max_pages=50).render(b"m" * (12 * 1024 * 1024),
                                                   "text/plain", "doc.txt")
    elapsed = time.monotonic() - t
    pages = _pdf_pages(out)
    assert len(pages) == 50
    assert "truncated after 50 pages" in pages[-1].extract_text()
    assert {r.fmt for r in out} >= {"pdf", "thumbnail", "preview"}
    assert elapsed < 15, f"capped render took {elapsed:.1f}s"


def test_a_long_line_wraps_in_linear_time_and_completely():
    from convert_search_ai.plugins.source_preview import SourcePreviewPlugin
    out = SourcePreviewPlugin(max_pages=0).render(b"ab" * 25000, "text/plain", "w.txt")
    text = "".join(p.extract_text() for p in _pdf_pages(out))
    assert "truncated" not in text
    assert text.count("ab") >= 24000            # nothing lost to the wrap


def test_an_ordinary_file_is_not_truncated():
    from convert_search_ai.plugins.source_preview import SourcePreviewPlugin
    out = SourcePreviewPlugin().render(b"def f(x):\n    return x * 2\n" * 50,
                                       "text/x-python", "a.py")
    assert "truncated" not in _pdf_pages(out)[-1].extract_text()


def test_the_page_cap_is_configured():
    from convert_search_ai.config import Config
    from convert_search_ai.plugins.registry import default_registry
    reg = default_registry(Config())
    src = next(p for p in reg._plugins if p.name == "source")
    assert src.max_pages == 300
