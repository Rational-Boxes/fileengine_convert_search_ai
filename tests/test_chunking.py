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

"""Unit tests for Markdown chunking."""
from convert_search_ai.chunking import chunk_markdown


def test_empty_input():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []


def test_small_doc_is_one_chunk():
    cs = chunk_markdown("# Title\n\nA short body paragraph.")
    assert len(cs) == 1
    assert cs[0].ordinal == 0
    assert "Title" in cs[0].text and "short body" in cs[0].text


def test_splits_large_doc_with_contiguous_ordinals():
    doc = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(20))
    cs = chunk_markdown(doc, target_chars=400, overlap_chars=50)
    assert len(cs) > 1
    assert [c.ordinal for c in cs] == list(range(len(cs)))
    assert all(len(c.text) < 800 for c in cs)  # roughly bounded by target + a block


def test_oversized_block_kept_whole():
    big = "x" * 5000  # a single block with no blank lines
    cs = chunk_markdown(big, target_chars=1000)
    assert len(cs) == 1 and len(cs[0].text) == 5000
