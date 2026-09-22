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
    assert names == {"pdf", "office", "image", "video", "model3d", "html", "markdown", "source", "text"}


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


# ── dispatch sees the filename, and Markdown is not source code ─────────────

def test_markdown_is_claimed_by_the_markdown_plugin_not_the_source_formatter():
    from convert_search_ai.plugins.registry import default_registry
    r = default_registry(None)
    assert r.for_mime("text/markdown", "notes.md").name == "markdown"
    # The case that shipped broken: libmagic says text/plain, and the source
    # plugin claims text/* wholesale — so without a name-aware claim it wins.
    assert r.for_mime("text/plain", "notes.md").name == "markdown"
    assert r.for_mime("text/plain", "notes.mkd").name == "markdown"
    assert r.for_mime("text/plain", "README.MARKDOWN").name == "markdown"


def test_the_source_formatter_still_owns_everything_else():
    from convert_search_ai.plugins.registry import default_registry
    r = default_registry(None)
    assert r.for_mime("text/plain", "notes.txt").name == "source"
    assert r.for_mime("text/x-python", "app.py").name == "source"
    assert r.for_mime("text/plain", "").name == "source"


def test_a_markdown_name_does_not_override_a_real_format():
    # PDF bytes called notes.md are a PDF: the name only resolves a GENERIC type.
    from convert_search_ai.plugins.registry import default_registry
    r = default_registry(None)
    assert r.for_mime("application/pdf", "notes.md").name == "pdf"
    assert r.for_mime("image/png", "diagram.md").name == "image"


def test_claims_defaults_to_supports_for_plugins_that_do_not_care():
    # A plugin that never heard of `claims` keeps working — the interface has a
    # default, so adding name-awareness could not break an existing converter.
    from convert_search_ai.plugins.base import ConversionPlugin

    class OnlyMime(ConversionPlugin):
        name = "only-mime"

        def supports(self, mime):
            return mime == "application/x-thing"

    p = OnlyMime()
    assert p.claims("application/x-thing") is True
    assert p.claims("application/x-thing", "whatever.thing") is True
    assert p.claims("text/plain", "whatever.thing") is False
