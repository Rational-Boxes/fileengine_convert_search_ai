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


def _rendition_plugin(fmt, ext, mime, markdown=None):
    class P(ConversionPlugin):
        name = "p"
        def supports(self, m): return True
        def render(self, d, m, n): return [Rendition(fmt, ext, b"DATA", mime)]
        def extract(self, d, m, n): return markdown
    return P()


def test_force_reconverts_already_processed_file():
    mf = FakeMF()
    mf.add_file("f2", "pic.png", content=b"x", version="v3")
    store = FakeStore()
    p = ConversionPipeline(mf=mf, store=store,
                           registry=PluginRegistry([_rendition_plugin("preview", "png", "image/png")]))

    assert p.convert("f2", "default").status == "converted"
    # Without force the same version is an idempotent no-op.
    again = p.convert("f2", "default")
    assert again.status == "skipped" and again.renditions_written == []
    # With force it re-runs and reports the full current set (already present).
    forced = p.convert("f2", "default", force=True)
    assert forced.status == "converted"
    assert forced.renditions_written == ["v3-preview.png"]


def test_force_backfills_renditions_for_previously_indexed_file():
    # A text file indexed before the preview plugin existed: status=indexed for
    # this version, but no rendition children — the on-demand (force) path must
    # run the plugin and write the missing renditions without re-embedding.
    mf = FakeMF()
    mf.add_file("t1", "a.txt", content=b"hello", version="v1")
    store = FakeStore()
    store.upsert("default", "t1", source_version="v1", status="indexed")
    p = ConversionPipeline(mf=mf, store=store,
                           registry=PluginRegistry([_rendition_plugin("pdf", "pdf", "application/pdf",
                                                                       markdown="hello")]))

    # Without force: the bug — skipped, no renditions ever written.
    assert p.convert("t1", "default").status == "skipped"
    assert mf.renditions.get("t1", {}) == {}

    out = p.convert("t1", "default", force=True)
    assert out.status == "indexed"                  # stays indexed, no re-embed
    assert out.renditions_written == ["v1-pdf.pdf"]
    assert "v1-pdf.pdf" in mf.renditions["t1"]


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


def test_new_version_conversion_prunes_old_version_renditions():
    # Converting a new version writes its renditions and wipes the prior
    # version's, so stale previews don't linger for superseded content.
    mf = FakeMF()
    mf.add_file("f2", "pic.png", content=b"v1", version="v1")
    store = FakeStore()
    p = ConversionPipeline(mf=mf, store=store,
                           registry=PluginRegistry([_rendition_plugin("preview", "png", "image/png")]))
    p.convert("f2", "default")
    assert set(mf.renditions["f2"]) == {"v1-preview.png"}

    mf.files["f2"]["version"] = "v2"
    out = p.convert("f2", "default")

    assert out.status == "converted"
    # Only the new version's rendition remains; the old one was pruned.
    assert set(mf.renditions["f2"]) == {"v2-preview.png"}


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
