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

"""Video → poster thumbnail + short web-optimized preview clip (FFmpeg)."""
from __future__ import annotations

import os
from typing import List

from .base import ConversionPlugin, Rendition
from .. import tools


# Preview encode targets, best-first. Prefer fully-open WebM (VP9, then VP8);
# fall back to H.264/MP4 only if this FFmpeg build has no VPx encoder.
#   (encoder, container ext, mime, encoder-specific extra args)
_PREVIEW_TARGETS = [
    ("libvpx-vp9", "webm", "video/webm", ["-deadline", "realtime", "-cpu-used", "8", "-b:v", "1M"]),
    ("libvpx", "webm", "video/webm", ["-deadline", "realtime", "-cpu-used", "8", "-b:v", "1M"]),
    ("libx264", "mp4", "video/mp4", ["-preset", "veryfast"]),
    ("libopenh264", "mp4", "video/mp4", []),
]


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

            # Preview: a short, scaled-down (640px wide), web-streamable clip of
            # the first 10s — silent (-an). Encoded with the first available
            # target (open WebM/VP9 preferred); see _PREVIEW_TARGETS.
            enc_set = tools.ffmpeg_encoders()
            target = next((t for t in _PREVIEW_TARGETS if t[0] in enc_set), None)
            if target:
                encoder, ext, mime, extra = target
                preview = os.path.join(d, f"preview.{ext}")
                cmd = ["ffmpeg", "-y", "-i", src, "-t", "10", "-vf", "scale=640:-2",
                       "-c:v", encoder, *extra, "-pix_fmt", "yuv420p", "-an"]
                if ext == "mp4":
                    cmd += ["-movflags", "+faststart"]  # progressive playback (WebM is already streamable)
                cmd.append(preview)
                if tools.run(cmd, timeout=180):
                    clip = tools.read_if_exists(preview)
                    if clip:
                        out.append(Rendition("preview", ext, clip, mime))
        return out
