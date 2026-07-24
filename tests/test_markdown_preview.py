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

"""Markdown is previewed as a *formatted* document (rendered headings/lists/
emphasis), not as raw source text. The formatted PDF (reportlab) is pure-Python
and always exercised; the PNG previews additionally need poppler (pdftoppm)."""
import struct

import pytest

from convert_search_ai import tools
from convert_search_ai.plugins.markdown_preview import MarkdownPlugin, inline_markup
from convert_search_ai.plugins.registry import default_registry

MD = b"""# Title

Some **bold** and *italic* and `code` and a [link](https://example.com).

## Section

- first
- second

1. one
2. two

> a quote

A fenced Python code block (should be syntax-highlighted, preformatted):

```python
def greet(name):
    # say hello
    return f"hello {name}"
```

A plain fenced block (preformatted, no language):

```
plain preformatted
    indented line
```

| col a | col b |
|-------|-------|
| 1     | 2     |
"""


def _png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


# --- MIME claims -------------------------------------------------------------

def test_supports_markdown_only():
    p = MarkdownPlugin()
    assert p.supports("text/markdown")
    assert p.supports("text/x-markdown")
    # plain text / source / other types are NOT claimed (left to the source plugin)
    for m in ("text/plain", "text/x-python", "text/html", "application/pdf", ""):
        assert not p.supports(m)


# --- inline formatting (the core "process Markdown into formatted output") ----

def test_inline_markup_converts_emphasis_code_links_and_escapes():
    out = inline_markup("**b** and *i* and `c` and [t](http://x) and 5 < 6 & 7")
    assert "<b>b</b>" in out
    assert "<i>i</i>" in out
    assert '<font face="Courier">c</font>' in out
    assert '<a href="http://x"' in out and ">t</a>" in out
    # XML-special chars are escaped (so reportlab markup stays well-formed)
    assert "&lt;" in out and "&amp;" in out


def test_inline_markup_does_not_format_inside_code_spans():
    out = inline_markup("`**not bold**`")
    assert "<b>" not in out
    assert "**not bold**" in out  # literal, inside the code font


# --- structured flowables (formatted, not flat text) -------------------------

def test_markdown_to_flowables_builds_headings_lists_and_code():
    pytest.importorskip("reportlab")
    from convert_search_ai.plugins.markdown_preview import markdown_to_flowables
    from reportlab.platypus import Paragraph

    flow = markdown_to_flowables(MD.decode())
    kinds = [type(f).__name__ for f in flow]
    assert "Paragraph" in kinds         # headings + paragraphs
    assert "ListFlowable" in kinds      # the bullet/ordered lists
    assert "XPreformatted" in kinds     # the fenced code block (highlighted, preformatted)
    # a heading is rendered with a heading style, not left as "# Title"
    heads = [f for f in flow if isinstance(f, Paragraph) and "Title" in f.text]
    assert heads and heads[0].style.fontSize >= 15


# --- embedded code blocks: source formatter (Pygments) integration -----------

def test_highlight_code_markup_uses_pygments_colours():
    pytest.importorskip("pygments")
    from convert_search_ai.plugins.markdown_preview import highlight_code_markup

    markup = highlight_code_markup('def greet(name):\n    return name\n', "python", "default")
    # Tokens wrapped in reportlab colour markup (the source-code formatter).
    assert "<font color=" in markup
    assert "<b>" in markup            # keywords styled bold by the Pygments style
    # source preserved (escaped, tokenised) — identifiers still present
    assert "greet" in markup and "name" in markup


def test_flowables_highlight_fenced_code_and_keep_other_blocks():
    pytest.importorskip("reportlab")
    pytest.importorskip("pygments")
    from convert_search_ai.plugins.markdown_preview import markdown_to_flowables
    from reportlab.platypus import ListFlowable, Paragraph, XPreformatted

    flow = markdown_to_flowables(MD.decode())
    kinds = [type(f).__name__ for f in flow]
    assert "XPreformatted" in kinds   # fenced code rendered as highlighted preformatted
    assert "ListFlowable" in kinds and "Paragraph" in kinds
    # the highlighted code flowable carries colour markup for the Python keywords
    code = next(f for f in flow if isinstance(f, XPreformatted))
    assert "<font color=" in code.text


def test_flowables_render_gfm_table():
    pytest.importorskip("reportlab")
    from convert_search_ai.plugins.markdown_preview import markdown_to_flowables
    from reportlab.platypus import Table

    flow = markdown_to_flowables(MD.decode())
    tables = [f for f in flow if isinstance(f, Table)]
    assert tables, "the Markdown table was not rendered as a Table flowable"
    # header + one data row, two columns; cell text parsed into the table
    flat = [c.text for row in tables[0]._cellvalues for c in row]
    assert any("col a" in x for x in flat) and any("col b" in x for x in flat)
    assert "1" in flat and "2" in flat


def test_render_pdf_with_table_builds():
    pytest.importorskip("reportlab")
    from convert_search_ai.plugins.markdown_preview import render_markdown_pdf

    md = "# T\n\n| name | qty |\n|:-----|----:|\n| apples | 3 |\n| pears | 12 |\n"
    pdf = render_markdown_pdf(md, "table.md", "default")
    assert pdf[:5] == b"%PDF-" and len(pdf) > 800


def test_render_pdf_builds_with_highlighted_code():
    pytest.importorskip("reportlab")
    pytest.importorskip("pygments")
    from convert_search_ai.plugins.markdown_preview import render_markdown_pdf

    # The highlighted code is XPreformatted colour markup; reportlab raises on
    # malformed markup, so a non-empty %PDF proves the coloured code was accepted.
    pdf = render_markdown_pdf(MD.decode(), "readme.md", "default")
    assert pdf[:5] == b"%PDF-" and len(pdf) > 800


# --- rendition output --------------------------------------------------------

def test_render_emits_formatted_pdf():
    pytest.importorskip("reportlab")
    out = MarkdownPlugin().render(MD, "text/markdown", "readme.md")
    pdf = [r for r in out if r.fmt == "pdf"]
    assert len(pdf) == 1
    assert pdf[0].mime == "application/pdf" and pdf[0].data[:5] == b"%PDF-"


def test_render_adds_png_previews_when_poppler_present():
    pytest.importorskip("reportlab")
    if not tools.have("pdftoppm"):
        pytest.skip("pdftoppm not installed")
    out = MarkdownPlugin().render(MD, "text/markdown", "readme.md")
    fmts = {r.fmt for r in out}
    assert {"pdf", "thumbnail", "preview"} <= fmts
    for r in out:
        if r.fmt in ("thumbnail", "preview"):
            assert r.ext == "png" and _png_size(r.data)[0] > 0


def test_extract_returns_raw_markdown_for_indexing():
    p = MarkdownPlugin()
    assert p.extract(MD, "text/markdown", "readme.md") == MD.decode()
    assert p.extract(b"", "text/markdown", "x.md") == ""


def test_empty_and_blank_input_yield_nothing():
    p = MarkdownPlugin()
    assert p.render(b"", "text/markdown", "x.md") == []
    assert p.render(b"   \n\t\n", "text/markdown", "x.md") == []


# --- dispatch ----------------------------------------------------------------

def test_registry_routes_markdown_to_markdown_plugin_before_source():
    reg = default_registry()
    order = [p.name for p in reg._plugins]
    assert "markdown" in order and order.index("markdown") < order.index("source")
    assert reg.for_mime("text/markdown").name == "markdown"


def test_registry_convert_markdown_has_pdf_and_extracts_text():
    pytest.importorskip("reportlab")
    res = default_registry().convert(MD, "text/markdown", "readme.md")
    assert res.supported
    assert res.markdown == MD.decode()                    # raw text still indexed
    assert any(r.fmt == "pdf" for r in res.renditions)    # formatted PDF produced
