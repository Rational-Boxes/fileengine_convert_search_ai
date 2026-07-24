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

"""Unit tests for rendition naming + idempotent writes (fake ManagedFiles)."""
from convert_search_ai.plugins.base import Rendition
from convert_search_ai.renditions import (
    RenditionWriter, parse_rendition_name, rendition_name,
)
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


def test_parse_rendition_name_roundtrip_and_rejects_others():
    assert parse_rendition_name("v9-preview.png") == ("v9", "preview", "png")
    assert parse_rendition_name("20260623-1-model.xkt") == ("20260623-1", "model", "xkt")
    # not renditions: unknown fmt, no dash, no extension
    assert parse_rendition_name("report.pdf") is None
    assert parse_rendition_name("v9-notes.txt") is None
    assert parse_rendition_name("plain") is None


def test_prune_removes_all_old_version_formats_keeps_current():
    mf = FakeMF()
    mf.add_file("file1", "report.pdf", version="v10")
    w = RenditionWriter(mf)
    # An old version's full rendition set across formats…
    for r in (("preview", "png"), ("thumbnail", "png"), ("pdf", "pdf"), ("model", "xkt")):
        w.write("file1", "v9", [Rendition(r[0], r[1], b"old", "x")], "default")
    # …plus the current version's renditions.
    w.write("file1", "v10", [Rendition("preview", "png", b"new", "image/png"),
                             Rendition("pdf", "pdf", b"new", "application/pdf")], "default")

    removed = w.prune_old_versions("file1", "v10", "default")

    assert set(removed) == {"v9-preview.png", "v9-thumbnail.png", "v9-pdf.pdf", "v9-model.xkt"}
    assert set(mf.renditions["file1"]) == {"v10-preview.png", "v10-pdf.pdf"}


def test_prune_leaves_non_rendition_children_untouched():
    mf = FakeMF()
    mf.add_file("file1", "report.pdf", version="v2")
    w = RenditionWriter(mf)
    w.write("file1", "v1", [Rendition("preview", "png", b"old", "image/png")], "default")
    w.write("file1", "v2", [Rendition("preview", "png", b"new", "image/png")], "default")
    # A hidden child that is not one of our renditions must never be pruned.
    mf.renditions["file1"]["notes.txt"] = "child-xyz"

    removed = w.prune_old_versions("file1", "v2", "default")

    assert removed == ["v1-preview.png"]
    assert "notes.txt" in mf.renditions["file1"]
    assert "v2-preview.png" in mf.renditions["file1"]


def test_prune_is_best_effort_on_delete_failure():
    mf = FakeMF()
    mf.add_file("file1", "report.pdf", version="v2")
    w = RenditionWriter(mf)
    w.write("file1", "v1", [Rendition("preview", "png", b"old", "image/png")], "default")
    w.write("file1", "v2", [Rendition("preview", "png", b"new", "image/png")], "default")

    def boom(uid, tenant=None, **kw):
        raise RuntimeError("core read-only")
    mf.remove = boom

    # Must not raise — cleanup failures are logged, not fatal to the conversion.
    assert w.prune_old_versions("file1", "v2", "default") == []
