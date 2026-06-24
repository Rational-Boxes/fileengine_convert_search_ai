"""Unit tests for the plugin framework — registry dispatch + text plugin."""
from convert_search_ai.plugins.base import ConversionPlugin, Rendition
from convert_search_ai.plugins.registry import PluginRegistry, default_registry
from convert_search_ai.plugins.text import TextMarkdownPlugin


def test_text_plugin_extracts_content_no_renditions():
    p = TextMarkdownPlugin()
    assert p.supports("text/plain")
    assert p.supports("text/markdown")
    assert not p.supports("application/pdf")
    assert p.extract(b"# Title\n\nbody", "text/markdown", "a.md") == "# Title\n\nbody"
    assert p.render(b"x", "text/plain", "a.txt") == []


def test_registry_dispatch_order_and_unsupported():
    class FakePdf(ConversionPlugin):
        name = "fakepdf"
        def supports(self, mime): return mime == "application/pdf"
        def render(self, data, mime, name): return [Rendition("preview", "png", b"PNG", "image/png")]
        def extract(self, data, mime, name): return "pdf text"

    reg = PluginRegistry([FakePdf(), TextMarkdownPlugin()])

    pdf = reg.convert(b"%PDF", "application/pdf", "a.pdf")
    assert pdf.supported and pdf.markdown == "pdf text"
    assert [r.fmt for r in pdf.renditions] == ["preview"]

    unknown = reg.convert(b"\x00", "application/x-thing", "a.bin")
    assert unknown.supported is False
    assert unknown.renditions == [] and unknown.markdown is None


def test_plugin_exception_is_fail_soft():
    class Boom(ConversionPlugin):
        name = "boom"
        def supports(self, mime): return True
        def render(self, data, mime, name): raise RuntimeError("nope")
        def extract(self, data, mime, name): raise RuntimeError("nope")

    reg = PluginRegistry([Boom()])
    res = reg.convert(b"x", "anything", "f")
    assert res.supported is True          # a plugin matched...
    assert res.renditions == [] and res.markdown is None  # ...but produced nothing


def test_default_registry_has_the_expected_plugins():
    names = {p.name for p in default_registry()._plugins}
    assert names == {"pdf", "office", "image", "video", "text"}
