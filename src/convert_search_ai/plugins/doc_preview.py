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

"""Page-1 preview images for document types.

Renders the first page of a PDF to an icon-sized ``thumbnail`` and a larger
``preview`` PNG using poppler's ``pdftoppm``. Shared by the PDF and Office
plugins so every supported document type gets the same icon + first-page preview
set (and, for Office, alongside the inline ``pdf`` rendition).

Degrades gracefully: returns ``[]`` when ``pdftoppm`` is missing or the PDF can't
be rendered, so the pipeline records partial output rather than failing."""
from __future__ import annotations

import os
from typing import List

from .base import Rendition
from .. import tools

# Default longest-edge sizes (px). Aligned with the image plugin so a document's
# icon/preview match an image's. Overridable via CSAI_DOC_THUMBNAIL_PX / _PREVIEW_PX.
DEFAULT_THUMBNAIL_PX = 256
DEFAULT_PREVIEW_PX = 1280


def page1_previews(pdf: bytes,
                   thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
                   preview_px: int = DEFAULT_PREVIEW_PX) -> List[Rendition]:
    """Icon-sized ``thumbnail`` + larger ``preview`` PNGs of the PDF's first page.

    Empty list if ``pdftoppm`` is unavailable, ``pdf`` is empty, or rendering
    fails. A non-positive size skips that rendition."""
    if not pdf or not tools.have("pdftoppm"):
        return []
    out: List[Rendition] = []
    with tools.workdir() as d:
        src = tools.write_temp(d, "in.pdf", pdf)
        for fmt, box in (("thumbnail", thumbnail_px), ("preview", preview_px)):
            if box <= 0:
                continue
            base = os.path.join(d, fmt)
            # -singlefile renders page 1 only (output is "<base>.png", no page
            # suffix); -scale-to fits the longer edge to `box`, preserving aspect.
            if tools.run(["pdftoppm", "-png", "-singlefile", "-scale-to", str(box), src, base]):
                png = tools.read_if_exists(base + ".png")
                if png:
                    out.append(Rendition(fmt=fmt, ext="png", data=png, mime="image/png"))
    return out
