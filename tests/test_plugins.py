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
    assert names == {"pdf", "office", "image", "video", "source", "text"}


def test_source_preview_precedes_text_catch_all():
    # The source/text preview plugin must win over the plain-text plugin for
    # text/* (it adds renditions), so it is registered first.
    plugins = default_registry()._plugins
    order = [p.name for p in plugins]
    assert order.index("source") < order.index("text")
    assert PluginRegistry(plugins).for_mime("text/x-python").name == "source"


# --- video preview encoder selection (open WebM preferred) -------------------
from convert_search_ai.plugins.video import VideoPlugin
from convert_search_ai import tools as _tools


def _stub_ffmpeg(monkeypatch, encoders):
    """Make VideoPlugin think ffmpeg exists, every run() succeeds, and outputs
    are non-empty — without invoking any real tool."""
    monkeypatch.setattr(_tools, "have", lambda t: True)
    monkeypatch.setattr(_tools, "ffmpeg_encoders", lambda: frozenset(encoders))
    monkeypatch.setattr(_tools, "read_if_exists", lambda p: b"BYTES")
    calls = []
    monkeypatch.setattr(_tools, "run", lambda cmd, timeout=120, input_bytes=None: (calls.append(cmd), True)[1])
    return calls


def test_video_preview_prefers_open_webm_vp9(monkeypatch):
    calls = _stub_ffmpeg(monkeypatch, {"libvpx-vp9", "libopenh264"})
    rends = VideoPlugin().render(b"video-bytes", "video/mp4", "clip.mp4")
    by = {(r.fmt, r.ext, r.mime) for r in rends}
    assert ("poster", "png", "image/png") in by
    assert ("preview", "webm", "video/webm") in by  # open WebM/VP9, not H.264
    preview_cmd = next(c for c in calls if any(str(x).endswith("preview.webm") for x in c))
    assert "libvpx-vp9" in preview_cmd


def test_video_preview_falls_back_to_h264_when_no_vpx(monkeypatch):
    _stub_ffmpeg(monkeypatch, {"libopenh264"})
    rends = VideoPlugin().render(b"v", "video/mp4", "clip.mp4")
    assert any(r.fmt == "preview" and r.ext == "mp4" and r.mime == "video/mp4" for r in rends)


def test_video_emits_poster_only_when_no_usable_encoder(monkeypatch):
    _stub_ffmpeg(monkeypatch, set())  # no H.264/VPx encoders at all
    rends = VideoPlugin().render(b"v", "video/mp4", "clip.mp4")
    fmts = {r.fmt for r in rends}
    assert fmts == {"poster"}  # still get the poster, just no clip
