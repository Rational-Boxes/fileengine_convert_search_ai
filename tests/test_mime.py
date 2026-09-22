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

"""Unit tests for MIME detection — content sniffing + extension fallback."""
import io
import zipfile

from convert_search_ai.mime import detect, DEFAULT


def test_pdf_by_magic():
    assert detect(b"%PDF-1.7\n...") == "application/pdf"


def test_png_and_jpeg_by_magic():
    assert detect(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert detect(b"\xff\xd8\xff\xe0JFIF") == "image/jpeg"


def test_mp4_ftyp_box():
    assert detect(b"\x00\x00\x00\x18ftypmp42....") == "video/mp4"


def test_docx_is_disambiguated_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("word/document.xml", "<w/>")
    mime = detect(buf.getvalue(), "report.docx")
    assert mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_plain_zip_without_office_members():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.txt", "hi")
    assert detect(buf.getvalue(), "bundle.zip") == "application/zip"


def test_extension_fallback_for_text():
    # No magic signature -> fall back to the file name's extension.
    assert detect(b"just some words", "notes.txt") == "text/plain"


def test_unknown_is_default():
    assert detect(b"\x01\x02\x03nothing-here") == DEFAULT


# ── a generic verdict is a floor, not an answer ─────────────────────────────
#
# libmagic (python-magic) is installed in the container and NOT in a plain dev
# checkout, which is why this only ever misbehaved in production: it answers
# `text/plain` for Markdown, OBJ and YAML alike, and `detect` used to return that
# and never look at the name. Markdown then went to the source-code formatter
# instead of the document renderer. These tests stub the module so the behaviour
# is pinned either way round.

class _FakeMagic:
    """Stands in for python-magic, answering whatever libmagic really answers."""

    def __init__(self, verdict):
        self.verdict = verdict

    def from_buffer(self, data, mime=False):  # noqa: ARG002 — signature parity
        return self.verdict


def _with_magic(monkeypatch, verdict):
    import sys
    monkeypatch.setitem(sys.modules, "magic", _FakeMagic(verdict))


import pytest  # noqa: E402


@pytest.mark.parametrize("name", [
    "notes.md", "notes.markdown", "notes.mdown", "notes.mkd", "notes.mdwn", "README.MD",
])
def test_markdown_survives_a_text_plain_verdict(monkeypatch, name):
    _with_magic(monkeypatch, "text/plain")
    assert detect(b"# Title\n\nbody **bold**\n", name) == "text/markdown"


def test_obj_survives_a_text_plain_verdict(monkeypatch):
    # OBJ has no signature to sniff, so before the refinement a .obj model was
    # plain text and went to the source formatter — the 3D chain never saw it.
    _with_magic(monkeypatch, "text/plain")
    assert detect(b"# Blender v2.8\nv 0 0 0\nf 1 2 3\n", "model.obj") == "model/obj"


def test_a_plain_text_file_stays_plain_text(monkeypatch):
    _with_magic(monkeypatch, "text/plain")
    assert detect(b"just some words", "notes.txt") == "text/plain"


def test_source_keeps_the_specific_verdict_libmagic_gives_it(monkeypatch):
    # A specific verdict is never second-guessed, even when mimetypes would say
    # something else for the extension.
    _with_magic(monkeypatch, "text/x-script.python")
    assert detect(b"import os\n", "script.py") == "text/x-script.python"


def test_a_name_cannot_promote_text_to_a_binary_format(monkeypatch):
    # The refinement is confined to text conventions and the curated 3D map. A
    # .pdf name over plain-text bytes must NOT become application/pdf.
    _with_magic(monkeypatch, "text/plain")
    assert detect(b"not really a pdf", "invoice.pdf") == "text/plain"


def test_content_still_beats_the_name_for_a_real_format():
    # No stub: the built-in sniffer sees %PDF- and wins over the .md extension.
    # This is the property that stops a name talking the service into a format.
    assert detect(b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n", "trap.md") == "application/pdf"


def test_the_sniffer_still_wins_over_libmagic_and_the_name(monkeypatch):
    _with_magic(monkeypatch, "text/plain")
    ifc = b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC4'));\nDATA;\n"
    assert detect(ifc, "Project.ifc") == "application/x-ifc"
