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

"""Table-fidelity test for PDF extraction — runs when an advanced backend is
installed (pdfplumber) plus fpdf2 to synthesize a table PDF; skips otherwise.

Proves the critical requirement: a PDF table survives conversion as a real
GitHub-flavored Markdown table (not flattened text)."""
import pytest

pytest.importorskip("pdfplumber")
fpdf = pytest.importorskip("fpdf")

from convert_search_ai.plugins.pdf import PdfPlugin
from convert_search_ai.plugins.pdf_backends import extract_markdown


def _pdf_with_table() -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, "Quarterly Report", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    rows = [["Region", "Q1", "Q2"], ["North", "100", "120"], ["South", "90", "85"]]
    with pdf.table() as table:
        for r in rows:
            row = table.row()
            for cell in r:
                row.cell(cell)
    return bytes(pdf.output())


def test_table_survives_as_gfm_markdown():
    md = extract_markdown(_pdf_with_table(), ["pdfplumber"])
    assert md
    assert "| Region | Q1 | Q2 |" in md
    assert "| --- | --- | --- |" in md          # a real GFM table, not flattened text
    for value in ("North", "South", "100", "120", "90", "85"):
        assert value in md
    assert "Quarterly Report" in md              # surrounding prose retained too


def test_pdf_plugin_extract_uses_backends():
    md = PdfPlugin(backends=["pdfplumber"]).extract(_pdf_with_table(), "application/pdf", "q.pdf")
    assert md and "| Region | Q1 | Q2 |" in md
