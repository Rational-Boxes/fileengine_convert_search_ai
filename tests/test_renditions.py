"""Unit tests for rendition naming + idempotent writes (fake ManagedFiles)."""
from convert_search_ai.plugins.base import Rendition
from convert_search_ai.renditions import RenditionWriter, rendition_name
from fakes import FakeMF


def test_rendition_name_keeps_version_and_format():
    assert rendition_name("20260623_021500.482", "pdf", "pdf") == "20260623_021500.482-pdf.pdf"
    # unsafe chars in the version are sanitized
    assert rendition_name("v/1 2", "thumbnail", "png") == "v_1_2-thumbnail.png"


def test_writes_renditions_as_hidden_children():
    mf = FakeMF()
    mf.add_file("file1", "report.pdf", version="v9")
    w = RenditionWriter(mf)
    rends = [Rendition("preview", "png", b"PNG", "image/png"),
             Rendition("pdf", "pdf", b"%PDF", "application/pdf")]

    written = w.write("file1", "v9", rends, "default")

    assert set(written) == {"v9-preview.png", "v9-pdf.pdf"}
    # each was created under the source file's uid and had content put
    assert set(mf.renditions["file1"]) == {"v9-preview.png", "v9-pdf.pdf"}
    assert len(mf.puts) == 2


def test_is_idempotent_across_reruns():
    mf = FakeMF()
    mf.add_file("file1", "report.pdf", version="v9")
    w = RenditionWriter(mf)
    rends = [Rendition("preview", "png", b"PNG", "image/png")]

    first = w.write("file1", "v9", rends, "default")
    second = w.write("file1", "v9", rends, "default")

    assert first == ["v9-preview.png"]
    assert second == []                 # already present -> nothing re-written
    assert len(mf.puts) == 1


def test_new_version_supersedes_with_new_name():
    mf = FakeMF()
    mf.add_file("file1", "report.pdf", version="v9")
    w = RenditionWriter(mf)
    w.write("file1", "v9", [Rendition("preview", "png", b"A", "image/png")], "default")
    w.write("file1", "v10", [Rendition("preview", "png", b"B", "image/png")], "default")
    assert set(mf.renditions["file1"]) == {"v9-preview.png", "v10-preview.png"}
