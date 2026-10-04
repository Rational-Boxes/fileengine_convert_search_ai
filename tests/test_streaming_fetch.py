"""MS0 (MEDIA_SHARE.md §4.2a): the source is streamed, never held whole, for a
plugin that can work from a path.

The pipeline used to do `blob = mf.get(...)` then `data = blob.read()` for every
file, so a 4 GB video was read into the worker's memory before the video plugin
wrote it straight back to a temp file for FFmpeg — and `bounds_own_memory = True`
exempted video from the sweep's size limit, so nothing stood in the way.

Rules pinned here:
  * a `consumes_path` plugin gets a PATH to the streamed source, with the exact
    bytes, and the source is never read whole by the pipeline;
  * every other plugin behaves exactly as before — it still receives the whole
    content as bytes, and the MIME is still sniffed from all of it;
  * the temp file is gone after every exit, including a plugin that raises.
"""
import os

from convert_search_ai.pipeline import ConversionPipeline
from convert_search_ai.plugins.base import ConversionPlugin, Rendition
from convert_search_ai.plugins.registry import PluginRegistry
from fakes import FakeMF, FakeStore

# The first bytes of a real WebM (EBML header) — enough for libmagic to say video.
WEBM = bytes.fromhex("1a45dfa39f4286810142f7810142f2810442f381084282847765626d")
VIDEO = WEBM + b"\x00" * 5000


class PathPlugin(ConversionPlugin):
    name = "pathplug"
    consumes_path = True
    bounds_own_memory = True

    def __init__(self, raise_in_render=False):
        self.seen_path = None
        self.seen_bytes = None
        self.raise_in_render = raise_in_render

    def supports(self, mime):
        return mime.startswith("video/")

    def render(self, data, mime, name):
        raise AssertionError("a path plugin must not be handed the bytes")

    def render_from_path(self, path, mime, name):
        self.seen_path = path
        with open(path, "rb") as f:
            self.seen_bytes = f.read()
        if self.raise_in_render:
            raise RuntimeError("encoder exploded")
        return [Rendition("poster", "png", b"PNG", "image/png")]


class BytesPlugin(ConversionPlugin):
    name = "bytesplug"

    def __init__(self):
        self.seen = None

    def supports(self, mime):
        return True

    def render(self, data, mime, name):
        self.seen = (data, mime)
        return [Rendition("thumbnail", "png", b"PNG", "image/png")]


def _pipeline(mf, plugins):
    return ConversionPipeline(mf=mf, store=FakeStore(), registry=PluginRegistry(plugins))


def test_a_path_plugin_gets_the_exact_bytes_by_path_and_the_source_is_streamed():
    mf = FakeMF()
    mf.add_file("v1", "clip.webm", content=VIDEO, version="ver1")
    plug = PathPlugin()
    out = _pipeline(mf, [plug, BytesPlugin()]).convert("v1", "default")
    assert out.status == "converted", out
    assert plug.seen_bytes == VIDEO
    assert mf.streams == ["v1"]
    assert out.renditions_written == ["ver1-poster.png"]


def test_the_temp_source_is_removed_after_conversion():
    mf = FakeMF()
    mf.add_file("v1", "clip.webm", content=VIDEO, version="ver1")
    plug = PathPlugin()
    _pipeline(mf, [plug]).convert("v1", "default")
    assert plug.seen_path and not os.path.exists(plug.seen_path)


def test_the_temp_source_is_removed_when_the_plugin_raises():
    mf = FakeMF()
    mf.add_file("v1", "clip.webm", content=VIDEO, version="ver1")
    plug = PathPlugin(raise_in_render=True)
    out = _pipeline(mf, [plug]).convert("v1", "default")
    # Fail-soft, exactly as a bytes plugin that raises: nothing written.
    assert out.status == "converted" and out.renditions_written == []
    assert plug.seen_path and not os.path.exists(plug.seen_path)


def test_a_bytes_plugin_still_gets_the_whole_content_and_full_content_mime():
    mf = FakeMF()
    body = b"hello world, plain text, " * 400
    mf.add_file("t1", "notes.txt", content=body, version="ver1")
    plug = BytesPlugin()
    out = _pipeline(mf, [PathPlugin(), plug]).convert("t1", "default")
    assert out.status == "converted"
    data, mime = plug.seen
    assert data == body
    assert mime.startswith("text/")


def test_a_missing_file_is_still_reported_missing():
    mf = FakeMF()
    mf.add_file("gone", "clip.webm", content=VIDEO, version="ver1")
    del mf.files["gone"]["content"]          # stat works, content vanished
    mf.files["gone"]["content"] = None

    class _Vanishing(FakeMF):
        pass

    # Simulate the content disappearing between stat and read.
    def boom(uid, version="", tenant=None, **kw):
        from fileengine.exceptions import NotFoundError
        raise NotFoundError("gone", operation="get_stream", uid=uid)
        yield b""  # pragma: no cover - makes this a generator, like the client
    mf.get_stream = boom
    out = _pipeline(mf, [PathPlugin()]).convert("gone", "default")
    assert out.status == "missing"


def test_video_plugin_render_and_render_from_path_issue_identical_commands(monkeypatch, tmp_path):
    """No new behaviour (MS0): the poster + preview a video produces must not
    change. Pinned at the command line, where the behaviour is decided."""
    from convert_search_ai import tools
    from convert_search_ai.plugins.video import VideoPlugin

    calls = []
    monkeypatch.setattr(tools, "have", lambda t: True)
    monkeypatch.setattr(tools, "ffmpeg_encoders", lambda: {"libvpx-vp9"})
    monkeypatch.setattr(tools, "run", lambda cmd, **kw: calls.append(list(cmd)) or False)

    VideoPlugin().render(VIDEO, "video/webm", "clip.webm")
    by_bytes = calls[:]
    calls.clear()
    src = tmp_path / "in"
    src.write_bytes(VIDEO)
    VideoPlugin().render_from_path(str(src), "video/webm", "clip.webm")
    by_path = calls[:]

    def normalise(cmds):
        # The input path differs (a temp copy vs the caller's file); nothing else may.
        out = []
        for c in cmds:
            i = c.index("-i")
            out.append([a if not (a.endswith(".png") or a.endswith(".webm") or a.endswith(".mp4"))
                        else os.path.basename(a) for a in c[:i + 1]] + ["<src>"] +
                       [a if not (a.endswith(".png") or a.endswith(".webm") or a.endswith(".mp4"))
                        else os.path.basename(a) for a in c[i + 2:]])
        return out

    assert len(by_bytes) == 2 and normalise(by_bytes) == normalise(by_path)


def test_the_video_plugin_declares_it_consumes_a_path():
    from convert_search_ai.plugins.video import VideoPlugin
    assert VideoPlugin.consumes_path is True
