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


# ── every fmt any producer emits must round-trip (MEDIA_SHARE.md §4.1) ───────
#
# `metamodel` was written by plugins/xeokit3d.py and absent from _KNOWN_FMTS, so
# parse_rendition_name returned None for it and prune_old_versions skipped every
# superseded copy — forever. For a 5 KB JSON that was untidy; for a 1 GB `media`
# rendition it would be a storage incident. This test reads the producers' source,
# so a new fmt with the same omission fails here rather than on someone's disk.

def _emitted_fmts():
    import pathlib
    import re
    from convert_search_ai import media_encode
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "convert_search_ai"
    found = set()
    pat = re.compile(r"""Rendition(?:\.from_path)?\(\s*(?:fmt\s*=\s*)?["']([A-Za-z0-9_-]+)["']""")
    for f in list((root / "plugins").glob("*.py")) + [root / "pipeline.py"]:
        found |= set(pat.findall(f.read_text()))
    found |= {fmt for fmt, _ext, _mime in media_encode.PROFILE_OUTPUT.values()}
    return found


def test_the_scan_finds_the_known_producers():
    # Guards the guard: a regex that matched nothing would make the next test
    # pass vacuously.
    fmts = _emitted_fmts()
    assert {"poster", "preview", "metamodel", "media", "media_sd", "audio"} <= fmts


def test_every_emitted_fmt_round_trips_through_the_parser():
    from convert_search_ai.renditions import parse_rendition_name, rendition_name
    for fmt in sorted(_emitted_fmts()):
        name = rendition_name("20261003_101010.123", fmt, "bin")
        parsed = parse_rendition_name(name)
        assert parsed is not None and parsed[1] == fmt, (
            f"{fmt!r} is emitted but does not parse as a rendition — add it to "
            f"_KNOWN_FMTS (and never put a '-' in a fmt), or it is never pruned")


def test_the_audience_sidecars_are_never_mistaken_for_renditions():
    # MEDIA_SHARE.md §8.1: a reserved sibling namespace. True today only because
    # no fmt is named after a UUID; pinned so it stays true.
    from convert_search_ai.renditions import parse_rendition_name
    assert parse_rendition_name("audience-3f2a0c1e-9b7d-4c1a-8e2f-0a1b2c3d4e5f.csv") is None
    assert parse_rendition_name("audience.csv") is None


# ── a published rendition outlives a new upload (MEDIA_SHARE.md §6.2 vs §4.7) ──
#
# §6.2: a media link pins the cut that was published when it was minted, and a
# new source version does NOT retarget it. §4.7 said superseded media would be
# pruned automatically — the two cannot both hold, and the prune would win: the
# ingest of a new version deletes the published copy every live link and embed
# is serving. Published renditions are therefore exempt from VERSION pruning;
# their lifetime is the share's, and the orphan reaper (§4.3.1) removes them.

def test_a_new_version_prunes_old_previews_but_keeps_old_published_media():
    from convert_search_ai.renditions import RenditionWriter
    from fakes import FakeEntry, FakeMF
    mf = FakeMF()
    mf.renditions["f"] = {
        "v1-preview.webm": "r1", "v1-poster.png": "r2",
        "v1-media.webm": "r3", "v1-media_sd.webm": "r4", "v1-emailposter.gif": "r5",
        "v1-audio.mp3": "r6", "v1-audio_opus.webm": "r7",
        "v2-preview.webm": "r8",
    }
    removed = RenditionWriter(mf).prune_old_versions("f", "v2", "default")
    assert sorted(removed) == ["v1-poster.png", "v1-preview.webm"]
    left = set(mf.renditions["f"])
    assert {"v1-media.webm", "v1-media_sd.webm", "v1-emailposter.gif",
            "v1-audio.mp3", "v1-audio_opus.webm", "v2-preview.webm"} == left


def test_the_published_fmts_are_all_known_fmts():
    from convert_search_ai.media_encode import PROFILE_OUTPUT
    from convert_search_ai.renditions import PUBLISHED_FMTS, _KNOWN_FMTS
    assert PUBLISHED_FMTS <= _KNOWN_FMTS
    assert {fmt for fmt, _e, _m in PROFILE_OUTPUT.values()} == PUBLISHED_FMTS
