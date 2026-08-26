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

"""Markdown chunking for embedding + retrieval.

Packs Markdown blocks (paragraphs, lists, GFM tables — split on blank lines) into
chunks of a target size with a small overlap so context isn't lost at chunk
boundaries. A block is kept whole where it fits, so a table or list is not cut
mid-structure.

WHERE IT DOES NOT FIT, IT IS SPLIT. Keeping an oversized block whole used to be
unconditional, which made chunk size unbounded — one 2467-char block came out of
a 1200-char target. Embedding models have a hard input limit
(BAAI/bge-base-en-v1.5 is 512 tokens), and exceeding it fails the request for the
WHOLE document: the indexer catches it, leaves the row at "converted", and
because "converted" counts as already-done nothing ever retries it. Losing a
table's structure costs some retrieval quality in one chunk; refusing to split
cost the entire document's searchability, silently. So blocks are split on line
boundaries where possible, and hard-cut only when a single line is itself too
long."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

_BLANKS = re.compile(r"\n\s*\n")

#: Length of the "\n\n" joined between a carried-over tail and the next block.
_SEP_LEN = 2


@dataclass
class Chunk:
    ordinal: int
    text: str


def _split_oversized(block: str, limit: int) -> List[str]:
    """Break one block into pieces of at most ``limit`` chars.

    Prefers line boundaries — a Markdown table row or list item stays intact, so
    the damage from splitting is a seam between rows rather than a torn row. A
    single line longer than the limit (a run-on paragraph, a base64 blob) is
    hard-cut, because there is no better boundary available and the alternative
    is a chunk the embedder will reject."""
    if len(block) <= limit:
        return [block]
    pieces: List[str] = []
    buf = ""
    for line in block.split("\n"):
        while len(line) > limit:            # a single unsplittable line
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = (buf + "\n" + line) if buf else line
        if buf and len(candidate) > limit:
            pieces.append(buf)
            buf = line
        else:
            buf = candidate
    if buf.strip():
        pieces.append(buf)
    return [p for p in pieces if p.strip()]


def chunk_markdown(md: str, *, target_chars: int = 1200, overlap_chars: int = 150) -> List[Chunk]:
    text = (md or "").strip()
    if not text:
        return []
    blocks = [b.strip() for b in _BLANKS.split(text) if b.strip()]
    # Cap every block BEFORE packing, so no chunk can exceed target_chars however
    # the blocks combine. The budget subtracts the overlap: a chunk carries the
    # previous one's tail as a prefix, so splitting at the full target would let
    # tail + block land at target + overlap and put the cap back over the limit.
    block_limit = max(1, target_chars - overlap_chars - _SEP_LEN)
    blocks = [p for b in blocks for p in _split_oversized(b, block_limit)]

    chunks: List[str] = []
    buf = ""
    for b in blocks:
        candidate = (buf + "\n\n" + b) if buf else b
        if buf and len(candidate) > target_chars:
            chunks.append(buf)
            # Snap the carried-over tail to a line boundary. A raw [-overlap:]
            # slice starts mid-row ("25 | b25 |"), so the next chunk opens with a
            # fragment that embeds as noise. The whole row is still intact in the
            # chunk we just emitted, so nothing is lost by starting the overlap
            # at the next line instead.
            tail = buf[-overlap_chars:] if overlap_chars > 0 else ""
            if tail and "\n" in tail:
                tail = tail.split("\n", 1)[1]
            buf = (tail + "\n\n" + b).strip() if tail else b
        else:
            buf = candidate
    if buf.strip():
        chunks.append(buf.strip())

    return [Chunk(i, c) for i, c in enumerate(chunks)]
