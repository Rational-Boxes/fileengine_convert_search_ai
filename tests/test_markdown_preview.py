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

```
code block
```
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
    from reportlab.platypus import ListFlowable, Paragraph, Preformatted

    flow = markdown_to_flowables(MD.decode())
    kinds = [type(f).__name__ for f in flow]
    assert "Paragraph" in kinds      # headings + paragraphs
    assert "ListFlowable" in kinds   # the bullet/ordered lists
    assert "Preformatted" in kinds   # the fenced code block
    # a heading is rendered with a heading style, not left as "# Title"
    heads = [f for f in flow if isinstance(f, Paragraph) and "Title" in f.text]
    assert heads and heads[0].style.fontSize >= 15


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
