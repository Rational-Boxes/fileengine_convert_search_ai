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


def test_an_oversized_block_is_split_not_kept_whole():
    """This test used to assert the opposite — one 5000-char chunk from a
    1000-char target — and that expectation is what reached production.

    Keeping a block whole protects a table from being cut mid-structure, which is
    a real benefit, but it left chunk size unbounded. Embedding models have a hard
    input limit (bge-base-en-v1.5: 512 tokens) and exceeding it fails the request
    for the ENTIRE document. Cutting a table costs one seam; refusing to cut cost
    22 documents their searchability, silently."""
    # Distinctive lines, so "nothing was dropped" is checkable. A plain run of
    # 5000 x's cannot distinguish loss from the deliberate overlap duplication.
    lines = ["line-%04d" % i for i in range(500)]
    big = "\n".join(lines)
    cs = chunk_markdown(big, target_chars=1000)
    assert len(cs) > 1
    assert max(len(c.text) for c in cs) <= 1000
    joined = "\n".join(c.text for c in cs)
    assert all(ln in joined for ln in lines), "a line was lost in the split"


def test_no_chunk_can_exceed_the_target_however_blocks_combine():
    """The cap has to survive the overlap: a chunk carries the previous one's
    tail as a prefix, so splitting blocks at the full target would let
    tail + block land at target + overlap."""
    md = "\n\n".join(["y" * 900, "z" * 4000, "short", "w" * 1500])
    cs = chunk_markdown(md, target_chars=1000, overlap_chars=150)
    assert max(len(c.text) for c in cs) <= 1000


def test_a_table_is_split_on_row_boundaries():
    """Splitting is unavoidable; tearing a row in half is not."""
    rows = ["| a%d | b%d |" % (i, i) for i in range(300)]
    cs = chunk_markdown("\n".join(rows), target_chars=400)
    assert max(len(c.text) for c in cs) <= 400
    for c in cs:
        for line in c.text.splitlines():
            if not line.strip():
                continue                       # blank separator between packed blocks
            assert line in rows, "a table row was cut mid-line: %r" % line
