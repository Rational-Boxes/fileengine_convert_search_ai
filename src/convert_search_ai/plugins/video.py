"""Video → poster thumbnail + short web-optimized preview clip (FFmpeg)."""
from __future__ import annotations

import os
from typing import List

from .base import ConversionPlugin, Rendition
from .. import tools


class VideoPlugin(ConversionPlugin):
    name = "video"

    def supports(self, mime: str) -> bool:
        return mime.startswith("video/")

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        if not tools.have("ffmpeg") or not data:
            return []
        out: List[Rendition] = []
        with tools.workdir() as d:
            src = tools.write_temp(d, "in", data)

            # Poster: one frame ~1s in, scaled to 640px wide.
            poster = os.path.join(d, "poster.png")
            if tools.run(["ffmpeg", "-y", "-ss", "1", "-i", src, "-frames:v", "1",
                          "-vf", "scale=640:-1", poster]):
                png = tools.read_if_exists(poster)
                if png:
                    out.append(Rendition("poster", "png", png, "image/png"))

            # Preview: first 10s, H.264, faststart for progressive web playback.
            preview = os.path.join(d, "preview.mp4")
            if tools.run(["ffmpeg", "-y", "-i", src, "-t", "10", "-vf", "scale=640:-2",
                          "-c:v", "libx264", "-preset", "veryfast", "-an",
                          "-movflags", "+faststart", preview], timeout=180):
                mp4 = tools.read_if_exists(preview)
                if mp4:
                    out.append(Rendition("preview", "mp4", mp4, "video/mp4"))
        return out
