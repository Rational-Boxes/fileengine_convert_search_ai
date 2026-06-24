"""Page-1 document preview renditions: icon-sized thumbnail + larger preview for
PDF and Office documents, plus the Office inline ``pdf`` rendition.

Real rendering is exercised when poppler (pdftoppm) + fpdf2 are present; the
graceful-degradation and wiring paths run with no external tools.
"""
import struct

import pytest

from convert_search_ai.plugins import doc_preview
from convert_search_ai.plugins.doc_preview import page1_previews
from convert_search_ai.plugins.office import OfficePlugin
from convert_search_ai.plugins.pdf import PdfPlugin
from convert_search_ai.plugins.registry import default_registry
from convert_search_ai import tools


def _png_size(data: bytes):
    """(width, height) from a PNG's IHDR chunk."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def _one_page_pdf() -> bytes:
    FPDF = pytest.importorskip("fpdf").FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.cell(0, 10, "Page One")
    return bytes(pdf.output())


# --- graceful degradation + wiring (no external tools needed) ---

def test_page1_previews_empty_input_returns_nothing():
    assert page1_previews(b"") == []


def test_page1_previews_degrades_when_pdftoppm_missing(monkeypatch):
    monkeypatch.setattr(tools, "have", lambda tool: False)
    assert page1_previews(b"%PDF-1.4 fake") == []


def test_pdf_plugin_emits_no_pdf_rendition_when_tool_missing(monkeypatch):
    # PDFs are their own inline preview, so the PDF plugin never emits a "pdf".
    monkeypatch.setattr(tools, "have", lambda tool: False)
    assert PdfPlugin().render(b"%PDF", "application/pdf", "a.pdf") == []


def test_config_sizes_flow_through_registry():
    class Cfg:
        pdf_backends = ["pdftotext"]
        doc_thumbnail_px = 96
        doc_preview_px = 1600

    reg = default_registry(Cfg())
    plugins = {p.name: p for p in reg._plugins}
    assert plugins["pdf"].thumbnail_px == 96 and plugins["pdf"].preview_px == 1600
    assert plugins["office"].thumbnail_px == 96 and plugins["office"].preview_px == 1600


def test_office_render_composes_pdf_plus_previews(monkeypatch):
    # Stub the LibreOffice conversion with a real one-page PDF, so render()'s
    # composition (inline pdf + page-1 previews) is exercised without soffice.
    pytest.importorskip("fpdf")
    if not tools.have("pdftoppm"):
        pytest.skip("pdftoppm not available")
    pdf_bytes = _one_page_pdf()
    plugin = OfficePlugin()
    monkeypatch.setattr(plugin, "_convert", lambda *a, **k: pdf_bytes)
    rends = plugin.render(
        b"docx-bytes",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "report.docx",
    )
    fmts = [r.fmt for r in rends]
    assert fmts == ["pdf", "thumbnail", "preview"]
    assert rends[0].mime == "application/pdf" and rends[0].data == pdf_bytes
    assert all(r.mime == "image/png" for r in rends[1:])


def test_office_render_empty_when_conversion_fails(monkeypatch):
    plugin = OfficePlugin()
    monkeypatch.setattr(plugin, "_convert", lambda *a, **k: None)
    assert plugin.render(b"x", "application/msword", "a.doc") == []


# --- real rendering (needs poppler + fpdf2) ---

@pytest.mark.skipif(not tools.have("pdftoppm"), reason="pdftoppm (poppler) not installed")
def test_pdf_render_produces_icon_and_larger_previews():
    rends = PdfPlugin(thumbnail_px=256, preview_px=1280).render(
        _one_page_pdf(), "application/pdf", "a.pdf")
    by_fmt = {r.fmt: r for r in rends}
    assert set(by_fmt) == {"thumbnail", "preview"}
    for r in rends:
        assert r.ext == "png" and r.mime == "image/png"
    # pdftoppm -scale-to sets the LONGER edge exactly; icon is clearly smaller.
    assert max(_png_size(by_fmt["thumbnail"].data)) == 256
    assert max(_png_size(by_fmt["preview"].data)) == 1280
    assert len(by_fmt["thumbnail"].data) < len(by_fmt["preview"].data)


@pytest.mark.skipif(not tools.have("pdftoppm"), reason="pdftoppm (poppler) not installed")
def test_doc_thumbnail_size_is_configurable():
    rends = PdfPlugin(thumbnail_px=128, preview_px=512).render(
        _one_page_pdf(), "application/pdf", "a.pdf")
    by_fmt = {r.fmt: r for r in rends}
    assert max(_png_size(by_fmt["thumbnail"].data)) == 128
    assert max(_png_size(by_fmt["preview"].data)) == 512
