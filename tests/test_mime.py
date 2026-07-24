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
