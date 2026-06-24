"""Images → thumbnail + web preview (ImageMagick)."""
from __future__ import annotations

import os
from typing import List

from .base import ConversionPlugin, Rendition
from .. import tools


class ImagePlugin(ConversionPlugin):
    name = "image"

    # (logical fmt, max box). Both emitted as stripped PNG.
    _SIZES = [("thumbnail", 256), ("preview", 1280)]

    def supports(self, mime: str) -> bool:
        return mime.startswith("image/")

    def _binary(self) -> str | None:
        if tools.have("magick"):
            return "magick"
        if tools.have("convert"):
            return "convert"
        return None

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        binary = self._binary()
        if not binary or not data:
            return []
        out: List[Rendition] = []
        with tools.workdir() as d:
            src = tools.write_temp(d, "in", data)
            for fmt, box in self._SIZES:
                dst = os.path.join(d, f"{fmt}.png")
                # -thumbnail keeps aspect ratio within the box; [0] = first frame.
                if tools.run([binary, f"{src}[0]", "-thumbnail", f"{box}x{box}", "-strip", dst]):
                    png = tools.read_if_exists(dst)
                    if png:
                        out.append(Rendition(fmt=fmt, ext="png", data=png, mime="image/png"))
        return out
