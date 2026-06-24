"""Unit tests for the conversion pipeline (fake MF + store + registry)."""
from convert_search_ai.pipeline import ConversionPipeline
from convert_search_ai.plugins.base import ConversionPlugin, Rendition
from convert_search_ai.plugins.registry import PluginRegistry
from fakes import FakeMF, FakeStore


def test_converts_text_file_and_extracts_markdown():
    mf = FakeMF()
    mf.add_file("f1", "notes.txt", content=b"hello world", version="v1")
    store = FakeStore()
    p = ConversionPipeline(mf=mf, store=store)  # default registry (text plugin)

    out = p.convert("f1", "default")

    assert out.status == "converted"
    assert out.has_markdown is True
    assert store.docs[("default", "f1")].status == "converted"
    assert store.docs[("default", "f1")].source_version == "v1"


def test_writes_renditions_from_plugin():
    class FakeImg(ConversionPlugin):
        name = "fakeimg"
        def supports(self, mime): return True
        def render(self, data, mime, name): return [Rendition("thumbnail", "png", b"PNG", "image/png")]
        def extract(self, data, mime, name): return None

    mf = FakeMF()
    mf.add_file("f2", "pic.png", content=b"\x89PNG\r\n\x1a\n", version="v3")
    store = FakeStore()
    p = ConversionPipeline(mf=mf, store=store, registry=PluginRegistry([FakeImg()]))

    out = p.convert("f2", "default")

    assert out.status == "converted"
    assert out.renditions_written == ["v3-thumbnail.png"]
    assert mf.renditions["f2"] == {"v3-thumbnail.png": list(mf.renditions["f2"].values())[0]}


def test_idempotent_skip_when_up_to_date():
    mf = FakeMF()
    mf.add_file("f1", "notes.txt", content=b"hi", version="v1")
    store = FakeStore()
    p = ConversionPipeline(mf=mf, store=store)

    assert p.convert("f1", "default").status == "converted"
    again = p.convert("f1", "default")
    assert again.status == "skipped"
    assert again.detail == "up-to-date"


def test_reconverts_on_new_version():
    mf = FakeMF()
    mf.add_file("f1", "notes.txt", content=b"v1", version="v1")
    store = FakeStore()
    p = ConversionPipeline(mf=mf, store=store)
    p.convert("f1", "default")

    mf.files["f1"]["version"] = "v2"      # a new version arrives
    out = p.convert("f1", "default")
    assert out.status == "converted"
    assert store.docs[("default", "f1")].source_version == "v2"


def test_unsupported_mime_is_recorded_not_failed():
    mf = FakeMF()
    mf.add_file("f3", "blob.bin", content=b"\x01\x02\x03nope", version="v1")
    store = FakeStore()
    p = ConversionPipeline(mf=mf, store=store)
    out = p.convert("f3", "default")
    assert out.status == "unsupported"
    assert store.docs[("default", "f3")].status == "unsupported"


def test_directory_is_skipped():
    mf = FakeMF()
    mf.add_file("d1", "folder", is_dir=True)
    p = ConversionPipeline(mf=mf, store=FakeStore())
    assert p.convert("d1", "default").status == "skipped"


def test_missing_file():
    p = ConversionPipeline(mf=FakeMF(), store=FakeStore())
    assert p.convert("nope", "default").status == "missing"
