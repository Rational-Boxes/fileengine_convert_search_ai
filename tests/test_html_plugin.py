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

"""Unit tests for the HTML → PDF plugin (full-document conversion + text)."""
import pytest

from convert_search_ai import tools as _tools
from convert_search_ai.mime import detect
from convert_search_ai.plugins import html as htmlmod
from convert_search_ai.plugins.html import HtmlPlugin, html_to_text

SAMPLE = (
    b"<!doctype html><html><head><title>Quarterly Report</title>"
    b"<style>body{color:#000}</style></head><body>"
    b"<h1>Revenue</h1><p>Up &amp; to the right.</p>"
    b"<script>var x=1;</script><table><tr><td>Q3</td><td>$1.2M</td></tr></table>"
    b"</body></html>"
)


# --------------------------------------------------------------------------- #
# MIME detection + plugin support
# --------------------------------------------------------------------------- #

def test_mime_html_by_content_and_name():
    assert detect(SAMPLE) == "text/html"
    assert detect(b"<html><body>hi</body></html>") == "text/html"
    assert detect(b"x", "report.html") == "text/html"


def test_supports():
    p = HtmlPlugin()
    assert p.supports("text/html")
    assert p.supports("application/xhtml+xml")
    assert not p.supports("text/plain")
    assert not p.supports("application/pdf")


# --------------------------------------------------------------------------- #
# Text extraction (no external tools)
# --------------------------------------------------------------------------- #

def test_html_to_text_strips_tags_drops_script_and_keeps_title():
    md = html_to_text(SAMPLE)
    assert "Quarterly Report" in md          # from <title>
    assert "Revenue" in md and "Up & to the right." in md  # entities decoded
    assert "var x" not in md                 # <script> body dropped
    assert "<" not in md and ">" not in md   # tags gone


def test_html_to_text_empty():
    assert html_to_text(b"") is None
    assert html_to_text(b"<html><body></body></html>") is None


# --------------------------------------------------------------------------- #
# Render: backend selection (external tools stubbed)
# --------------------------------------------------------------------------- #

def _stub(monkeypatch, available, pdf=b"%PDF-1.4 fake"):
    calls = []
    monkeypatch.setattr(_tools, "have", lambda t: t in available)
    monkeypatch.setattr(_tools, "run",
                        lambda cmd, timeout=120, input_bytes=None: (calls.append(cmd), True)[1])
    monkeypatch.setattr(_tools, "read_if_exists", lambda p: pdf if str(p).endswith(".pdf") else None)
    # Don't shell out to poppler for previews in these unit tests.
    monkeypatch.setattr(htmlmod, "page1_previews", lambda *a, **k: [])
    return calls


def test_render_prefers_chromium(monkeypatch):
    calls = _stub(monkeypatch, {"chromium-browser"})
    rends = HtmlPlugin().render(SAMPLE, "text/html", "r.html")
    assert len(rends) == 1
    assert (rends[0].fmt, rends[0].ext, rends[0].mime) == ("pdf", "pdf", "application/pdf")
    assert any("chromium-browser" in c[0] for c in calls)


def test_render_falls_back_to_libreoffice(monkeypatch):
    calls = _stub(monkeypatch, {"soffice"})       # no chromium
    rends = HtmlPlugin().render(SAMPLE, "text/html", "r.html")
    assert len(rends) == 1
    assert any("soffice" in c[0] for c in calls)
    assert not any("chromium" in c[0] for c in calls)


def test_render_nothing_without_any_engine(monkeypatch):
    _stub(monkeypatch, set())
    assert HtmlPlugin().render(SAMPLE, "text/html", "r.html") == []


def test_render_includes_page_previews(monkeypatch):
    from convert_search_ai.plugins.base import Rendition
    _stub(monkeypatch, {"chromium-browser"})
    monkeypatch.setattr(htmlmod, "page1_previews",
                        lambda *a, **k: [Rendition("thumbnail", "png", b"\x89PNG", "image/png")])
    rends = HtmlPlugin().render(SAMPLE, "text/html", "r.html")
    assert [r.fmt for r in rends] == ["pdf", "thumbnail"]


# --------------------------------------------------------------------------- #
# Registry wiring
# --------------------------------------------------------------------------- #

def test_registry_routes_html_to_html_plugin_before_text():
    from convert_search_ai.plugins.registry import default_registry
    reg = default_registry()
    assert reg.for_mime("text/html").name == "html"
    order = [p.name for p in reg._plugins]
    assert order.index("html") < order.index("source")  # before source/text catch-alls
    assert order.index("html") < order.index("text")


# --------------------------------------------------------------------------- #
# Live (real Chromium/LibreOffice) — produces a real PDF
# --------------------------------------------------------------------------- #

@pytest.mark.live
def test_live_html_to_pdf():
    p = HtmlPlugin()
    if not (_tools.have(p.chromium) or _tools.have("soffice") or _tools.have("libreoffice")):
        pytest.skip("no HTML→PDF engine installed")
    pdf = p._to_pdf(SAMPLE)
    assert pdf and pdf[:4] == b"%PDF"
