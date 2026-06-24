"""Unit tests for structure/table-preserving PDF extraction backends."""
from convert_search_ai.plugins import pdf_backends as B
from convert_search_ai.plugins.pdf import PdfPlugin


def test_rows_to_markdown_is_gfm_table():
    md = B.rows_to_markdown([["Name", "Qty"], ["Apples", "3"], ["Pears", "10"]])
    lines = md.splitlines()
    assert lines[0] == "| Name | Qty |"
    assert lines[1] == "| --- | --- |"     # GFM header separator
    assert lines[2] == "| Apples | 3 |"
    assert lines[3] == "| Pears | 10 |"


def test_rows_to_markdown_pads_ragged_rows_and_escapes_pipes():
    md = B.rows_to_markdown([["a", "b", "c"], ["1", "2"], ["x|y"]])
    lines = md.splitlines()
    assert lines[0] == "| a | b | c |"
    assert lines[2] == "| 1 | 2 |  |"       # padded to width 3
    assert lines[3] == "| x\\|y |  |  |"     # pipe escaped, padded


def test_rows_to_markdown_empty():
    assert B.rows_to_markdown([]) == ""
    assert B.rows_to_markdown([[None]]) == "|  |\n| --- |"


def test_extract_markdown_uses_order_and_first_nonempty_wins(monkeypatch):
    calls = []

    def make(name, value):
        def fn(data):
            calls.append(name)
            return value
        return fn

    monkeypatch.setattr(B, "BACKENDS", {
        "docling": make("docling", None),       # installed-ish but yields nothing
        "pymupdf4llm": make("pymupdf4llm", "# Doc\n\n| a | b |"),
        "pdfplumber": make("pdfplumber", "should-not-reach"),
    })
    out = B.extract_markdown(b"%PDF", ["docling", "pymupdf4llm", "pdfplumber"])
    assert out == "# Doc\n\n| a | b |"
    assert calls == ["docling", "pymupdf4llm"]   # stopped at first non-empty


def test_extract_markdown_skips_uninstalled_and_erroring_backends(monkeypatch):
    def boom(data):
        raise ImportError("not installed")

    monkeypatch.setattr(B, "BACKENDS", {
        "docling": boom,                                   # raises -> skipped
        "pdfplumber": lambda data: "  ",                   # blank -> skipped
        "pdftotext": lambda data: "plain text fallback",
    })
    out = B.extract_markdown(b"%PDF", ["docling", "pdfplumber", "pdftotext", "unknown"])
    assert out == "plain text fallback"


def test_extract_markdown_returns_none_when_all_fail(monkeypatch):
    monkeypatch.setattr(B, "BACKENDS", {"pdftotext": lambda data: None})
    assert B.extract_markdown(b"%PDF", ["pdftotext"]) is None


def test_pdf_plugin_default_backend_order():
    assert PdfPlugin().backends == ["docling", "pymupdf4llm", "pdfplumber", "pdftotext"]
    assert PdfPlugin(backends=["pdfplumber"]).backends == ["pdfplumber"]
