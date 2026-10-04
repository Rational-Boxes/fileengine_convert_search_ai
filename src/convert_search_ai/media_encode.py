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

"""Publish-grade media renditions (MEDIA_SHARE.md §4.1, §4.5, §9.4) — MS1.

These are OUTBOUND artifacts: a whole video at 720p and 480p, an audio file as
MP3 and Opus, and a small animated poster for email. They are produced only when
a media share is configured (§4.3), by the media worker — never on ingest.

That is why this is a module of functions and not a ConversionPlugin. Plugins are
dispatched on ingest for every file of their MIME type; registering a publish
encoder there would transcode every uploaded video and audio file, which is
exactly the automatic conversion §4.3 forbids. The ingest-time ``VideoPlugin``
keeps producing ``poster`` + the 10-second ``preview`` and nothing more.

Everything works from a PATH (MS0's ``consumes_path``): the source is never held
in memory, and the outputs are file-backed renditions the writer streams.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Set

from . import tools
from .plugins.base import Rendition

log = logging.getLogger("convert_search_ai.media_encode")


# ── settings ────────────────────────────────────────────────────────────────


@dataclass
class MediaSettings:
    """The encoder parameters (§11). Built from Config by :meth:`from_config`;
    constructed directly by tests with small dimensions."""

    video_height: int = 720          # the HD profile's cap, as a short edge
    video_crf: int = 31
    sd_enabled: bool = True
    sd_height: int = 480
    sd_crf: int = 33
    # Constrained quality: CRF sets the quality, these cap the bitrate (decided
    # 2026-10-03). Uncapped, a real 1080p foliage clip came out at 8.15 Mb/s at
    # 720p; a talking-head intro stays under these anyway.
    video_max_bitrate: str = "2500k"
    sd_max_bitrate: str = "1200k"
    audio_bitrate: str = "128k"      # Opus inside the video
    mp3_quality: int = 0             # LAME -q:a (0 = V0)
    opus_enabled: bool = True
    opus_bitrate: str = "96k"        # the standalone audio-opus rendition
    gif_enabled: bool = True
    gif_seconds: int = 3
    gif_fps: int = 8
    gif_width: int = 280            # half the usual 560 email body width (2026-10-03)
    gif_max_bytes: int = 2 * 1024 * 1024
    threads: int = 0                 # 0 = FFmpeg's choice
    timeout_seconds: int = 21600
    max_output_bytes: int = 0        # 0 = unbounded
    # NOTHING VERY LONG IS SERVED (decided 2026-10-03). FileEngine publishes
    # short clips; a long video belongs on a video platform — PeerTube first, as
    # the open-source option — and is refused here before any CPU or storage is
    # spent on it. 0 = no limit.
    max_duration_seconds: int = 600

    @classmethod
    def from_config(cls, config) -> "MediaSettings":
        g = lambda k, d: getattr(config, k, d)  # noqa: E731
        return cls(
            video_height=g("media_video_height", 720), video_crf=g("media_video_crf", 31),
            sd_enabled=g("media_sd_enabled", True), sd_height=g("media_sd_height", 480),
            sd_crf=g("media_sd_crf", 33), audio_bitrate=g("media_audio_bitrate", "128k"),
            video_max_bitrate=g("media_video_max_bitrate", "2500k"),
            sd_max_bitrate=g("media_sd_max_bitrate", "1200k"),
            mp3_quality=g("media_mp3_quality", 0), opus_enabled=g("media_opus_enabled", True),
            opus_bitrate=g("media_opus_bitrate", "96k"), gif_enabled=g("media_gif_enabled", True),
            gif_seconds=g("media_gif_seconds", 3), gif_fps=g("media_gif_fps", 8),
            gif_width=g("media_gif_width", 280), gif_max_bytes=g("media_gif_max_bytes", 2 * 1024 * 1024),
            threads=g("media_ffmpeg_threads", 0), timeout_seconds=g("media_job_timeout_seconds", 21600),
            max_output_bytes=g("media_max_output_bytes", 0),
            max_duration_seconds=g("media_max_duration_seconds", 600))


# ── the vocabulary ──────────────────────────────────────────────────────────

#: profile -> (rendition fmt, ext, mime) for the PREFERRED target. A fallback
#: target (VP8, H.264) changes ext/mime but never the fmt.
PROFILE_OUTPUT = {
    "video-720p-vp9": ("media", "webm", "video/webm"),
    "video-480p-vp9": ("media_sd", "webm", "video/webm"),
    "audio-mp3": ("audio", "mp3", "audio/mpeg"),
    "audio-opus": ("audio_opus", "webm", "audio/webm"),
    "video-emailposter": ("emailposter", "gif", "image/gif"),
}

VIDEO_PROFILES = ("video-720p-vp9", "video-480p-vp9", "video-emailposter")
AUDIO_PROFILES = ("audio-mp3", "audio-opus")


def profiles_for(mime: str, settings: MediaSettings) -> list:
    """The profiles a publish request queues for a source of ``mime`` (§4.5).
    Eager: both video sizes together (Q7). Skips that depend on the SOURCE (SD
    for an already-small video, Opus without libopus) are decided at encode time
    and recorded as ``skipped``, not failed."""
    if mime.startswith("video/"):
        out = ["video-720p-vp9"]
        if settings.sd_enabled:
            out.append("video-480p-vp9")
        if settings.gif_enabled:
            out.append("video-emailposter")
        return out
    if mime.startswith("audio/"):
        out = ["audio-mp3"]
        if settings.opus_enabled:
            out.append("audio-opus")
        return out
    return []


# Video encode targets, best-first (§4.5). The preview ladder's shape, with
# publish-grade settings: constrained quality, `good` deadline — slow is allowed.
_VIDEO_TARGETS = [
    ("libvpx-vp9", "webm", "video/webm"),
    ("libvpx", "webm", "video/webm"),
    ("libx264", "mp4", "video/mp4"),
    ("libopenh264", "mp4", "video/mp4"),
]


# ── probing ─────────────────────────────────────────────────────────────────


@dataclass
class Probe:
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    vcodec: str = ""
    acodec: str = ""
    format_name: str = ""
    bit_rate: int = 0                 # whole-file average, bits/s; 0 when unknown

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec)

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec)

    @property
    def short_edge(self) -> int:
        return min(self.width, self.height) if self.width and self.height else 0


def probe(path: str) -> Optional[Probe]:
    """ffprobe the file. None when it cannot be read as media at all."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
             "-show_format", path],
            capture_output=True, check=False, timeout=120, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        info = json.loads(out.stdout or "{}")
    except ValueError:
        return None
    p = Probe(format_name=str((info.get("format") or {}).get("format_name") or ""))
    try:
        p.duration_ms = int(float((info.get("format") or {}).get("duration") or 0) * 1000)
    except (TypeError, ValueError):
        p.duration_ms = 0
    try:
        p.bit_rate = int((info.get("format") or {}).get("bit_rate") or 0)
    except (TypeError, ValueError):
        p.bit_rate = 0
    for s in info.get("streams") or []:
        if s.get("codec_type") == "video" and not p.vcodec:
            # An attached cover image (an MP3's artwork) is not a video track.
            if (s.get("disposition") or {}).get("attached_pic"):
                continue
            p.vcodec = str(s.get("codec_name") or "")
            p.width, p.height = int(s.get("width") or 0), int(s.get("height") or 0)
        elif s.get("codec_type") == "audio" and not p.acodec:
            p.acodec = str(s.get("codec_name") or "")
    return p


# ── results ─────────────────────────────────────────────────────────────────


@dataclass
class EncodeResult:
    status: str                          # ok | skipped | failed | cancelled
    rendition: Optional[Rendition] = None
    encoder: str = ""
    detail: str = ""
    output_bytes: int = 0
    duration_ms: int = 0
    width: int = 0
    height: int = 0


# ── the encode ──────────────────────────────────────────────────────────────


def _cap_scale(short_edge_cap: int) -> str:
    """Cap the SHORT edge at ``short_edge_cap`` (so the long edge at 16:9 is
    ``cap * 16 / 9``), never upscale, keep even dimensions, and respect
    orientation (§4.5). `scale=1280:-2` would turn a portrait phone video into a
    1280×2276 file; this keeps it 720×1280."""
    c = int(short_edge_cap)
    return (f"scale='if(gt(iw,ih),-2,min({c},iw))':'if(gt(iw,ih),min({c},ih),-2)'")


def _threads(settings: MediaSettings) -> list:
    return ["-threads", str(settings.threads)] if settings.threads else []


def bitrate_bps(value: str) -> int:
    """'2500k' / '4M' / '1200000' -> bits per second. 0 for anything unparseable."""
    v = (value or "").strip().lower()
    mult = 1
    if v.endswith("k"):
        mult, v = 1000, v[:-1]
    elif v.endswith("m"):
        mult, v = 1000_000, v[:-1]
    try:
        return int(float(v) * mult)
    except ValueError:
        return 0


def _video_cmd(src: str, out: str, encoder: str, ext: str, cap: int, crf: int,
               settings: MediaSettings, encoders: Set[str], max_bitrate: str = "") -> list:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
           "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", _cap_scale(cap),
           "-pix_fmt", "yuv420p", *_threads(settings)]
    rate = max_bitrate or "0"
    if encoder == "libvpx-vp9":
        # -crf with a NON-zero -b:v is libvpx's constrained-quality mode: the CRF
        # decides quality and -b:v is the ceiling. (-b:v 0 would be pure CQ.)
        cmd += ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", rate, "-row-mt", "1",
                "-tile-columns", "2", "-deadline", "good", "-cpu-used", "2", "-g", "240"]
    elif encoder == "libvpx":
        cmd += ["-c:v", "libvpx", "-crf", str(crf - 21 if crf > 21 else 10),
                "-b:v", max_bitrate or "2M", "-deadline", "good", "-cpu-used", "2", "-g", "240"]
    elif encoder == "libx264":
        cmd += ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-g", "240"]
        if max_bitrate:
            cmd += ["-maxrate", max_bitrate, "-bufsize", str(2 * bitrate_bps(max_bitrate))]
    else:  # libopenh264 — bitrate-driven, so the ceiling IS its target
        cmd += ["-c:v", "libopenh264", "-b:v", max_bitrate or "2500k", "-g", "240"]
    if ext == "webm":
        if "libopus" in encoders:
            cmd += ["-c:a", "libopus", "-b:a", settings.audio_bitrate]
        else:
            cmd += ["-c:a", "libvorbis", "-q:a", "6"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    return cmd + [out]


def _conformant(p: Probe, cap: int, max_bitrate: str = "") -> bool:
    """Already what we would produce: VP9 (+ Opus or no audio) in WebM, within
    the size cap AND the bitrate ceiling. Re-encoding it would be a worse copy
    of itself (§4.5) — but copying one above the ceiling would publish exactly
    the file the ceiling exists to prevent, so that one is re-encoded."""
    limit = bitrate_bps(max_bitrate)
    within_rate = not limit or (0 < p.bit_rate <= limit)
    return ("webm" in p.format_name and p.vcodec == "vp9"
            and p.acodec in ("opus", "") and 0 < p.short_edge <= cap and within_rate)


def _run_ffmpeg(cmd: list, duration_ms: int, settings: MediaSettings,
                on_progress: Optional[Callable[[int], None]],
                should_cancel: Optional[Callable[[], bool]]) -> str:
    """Run an FFmpeg command that writes ``-progress pipe:1``. Returns
    'ok' | 'failed' | 'cancelled' | 'timeout'. Reports a monotonic 0..99 while
    running; the caller reports 100 on success."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)
    except OSError as e:
        log.warning("ffmpeg could not start: %s", e)
        return "failed"
    started = time.monotonic()
    last = -1
    try:
        for line in proc.stdout:              # one `key=value` per line
            if should_cancel is not None and should_cancel():
                proc.kill()
                proc.wait()
                return "cancelled"
            if time.monotonic() - started > settings.timeout_seconds:
                proc.kill()
                proc.wait()
                return "timeout"
            key, _, value = line.strip().partition("=")
            # out_time_us is microseconds; out_time_ms is ALSO microseconds
            # (a long-standing FFmpeg naming quirk), so either works.
            if key in ("out_time_us", "out_time_ms") and duration_ms > 0:
                try:
                    us = int(value)
                except ValueError:
                    continue
                pct = max(0, min(99, int(us / 1000 * 100 / duration_ms)))
                if pct > last and on_progress is not None:
                    last = pct
                    on_progress(pct)
        # A cancel requested after the last progress line still wins.
        if should_cancel is not None and should_cancel():
            proc.kill()
            proc.wait()
            return "cancelled"
        return "ok" if proc.wait() == 0 else "failed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _finish(profile: str, out_path: str, ext: str, mime: str, encoder: str,
            settings: MediaSettings, on_progress) -> EncodeResult:
    """Detach a successful output as a file-backed rendition, enforcing the
    output ceiling (§4.7)."""
    fmt = PROFILE_OUTPUT[profile][0]
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if size <= 0:
        return EncodeResult("failed", detail="the encoder produced no output")
    if settings.max_output_bytes and size > settings.max_output_bytes:
        return EncodeResult("failed", detail=(f"output of {size} bytes exceeds the ceiling "
                                              f"of {settings.max_output_bytes}"))
    got = probe(out_path) or Probe()
    detached = tools.detach(out_path)
    if detached is None:
        return EncodeResult("failed", detail="the encoder output could not be kept")
    path, cleanup = detached
    if on_progress is not None:
        on_progress(100)
    return EncodeResult("ok", Rendition.from_path(fmt, ext, path, mime, cleanup=cleanup),
                        encoder=encoder, output_bytes=size, duration_ms=got.duration_ms,
                        width=got.width, height=got.height)


def encode(profile: str, src: str, settings: MediaSettings, *,
           encoders: Optional[Set[str]] = None,
           on_progress: Optional[Callable[[int], None]] = None,
           should_cancel: Optional[Callable[[], bool]] = None) -> EncodeResult:
    """Produce one publish rendition from the source at ``src``.

    Never raises for an encoding problem: the outcome is the result's ``status``
    and a user-safe ``detail``, so the job can record it. A ``skipped`` result is
    a correct outcome (no SD copy of an SD source; no Opus without libopus), not
    a failure. An output exceeding ``max_output_bytes`` is failed and discarded,
    never written."""
    if profile not in PROFILE_OUTPUT:
        raise ValueError(f"unknown media profile {profile!r}")
    if not tools.have("ffmpeg"):
        return EncodeResult("failed", detail="ffmpeg is not installed")
    enc = set(encoders) if encoders is not None else set(tools.ffmpeg_encoders())
    p = probe(src)
    if p is None:
        return EncodeResult("failed", detail="the source could not be read as media")
    too_long = _too_long(p, settings)
    if too_long:
        return EncodeResult("failed", detail=too_long)

    with tools.workdir() as d:
        if profile in ("video-720p-vp9", "video-480p-vp9"):
            return _encode_video(profile, src, d, p, settings, enc, on_progress, should_cancel)
        if profile == "video-emailposter":
            return _encode_gif(src, d, p, settings, on_progress, should_cancel)
        return _encode_audio(profile, src, d, p, settings, enc, on_progress, should_cancel)


def _minutes(ms: int) -> str:
    m = ms / 60000
    return f"{m:.0f} minute{'s' if round(m) != 1 else ''}" if m >= 1 else f"{ms // 1000} seconds"


def _too_long(p: Probe, settings: MediaSettings) -> str:
    """The refusal for a source over the duration limit, or "". Written for the
    person who asked: it says why, and where the video should go instead."""
    limit = int(settings.max_duration_seconds or 0)
    if not limit or p.duration_ms <= limit * 1000:
        return ""
    return (f"This recording is too long to publish here ({_minutes(p.duration_ms)}; "
            f"FileEngine publishes clips of up to {_minutes(limit * 1000)}). For longer "
            f"videos, publish to PeerTube (open source), YouTube or Vimeo and share that "
            f"link instead.")


def _encode_video(profile, src, d, p, settings, enc, on_progress, should_cancel):
    if not p.has_video:
        return EncodeResult("failed", detail="the source has no video track")
    hd = profile == "video-720p-vp9"
    cap = settings.video_height if hd else settings.sd_height
    crf = settings.video_crf if hd else settings.sd_crf
    max_bitrate = settings.video_max_bitrate if hd else settings.sd_max_bitrate
    if not hd and p.short_edge and p.short_edge <= settings.sd_height:
        # A second identical-size file is pure storage (§4.5).
        return EncodeResult("skipped", detail="the source is already standard definition")

    if _conformant(p, cap, max_bitrate):
        out = os.path.join(d, "out.webm")
        status = _run_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
                              "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", out],
                             p.duration_ms, settings, on_progress, should_cancel)
        if status == "ok":
            return _finish(profile, out, "webm", "video/webm", "copy", settings, on_progress)
        if status == "cancelled":
            return EncodeResult("cancelled", detail="cancelled")
        # A remux that fails falls through to a real encode.

    target = next((t for t in _VIDEO_TARGETS if t[0] in enc), None)
    if target is None:
        return EncodeResult("failed", detail="no usable video encoder in this FFmpeg build")
    encoder, ext, mime = target
    out = os.path.join(d, f"out.{ext}")
    status = _run_ffmpeg(_video_cmd(src, out, encoder, ext, cap, crf, settings, enc,
                                    max_bitrate),
                         p.duration_ms, settings, on_progress, should_cancel)
    if status == "cancelled":
        return EncodeResult("cancelled", detail="cancelled")
    if status == "timeout":
        return EncodeResult("failed", detail="the encode exceeded its time limit")
    if status != "ok":
        return EncodeResult("failed", detail=f"the {encoder} encode failed")
    return _finish(profile, out, ext, mime, encoder, settings, on_progress)


def _encode_audio(profile, src, d, p, settings, enc, on_progress, should_cancel):
    if not p.has_audio:
        return EncodeResult("failed", detail="the source has no audio track")
    if profile == "audio-mp3":
        if "libmp3lame" not in enc:
            return EncodeResult("failed", detail="this FFmpeg build has no MP3 encoder")
        out = os.path.join(d, "out.mp3")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", src,
               "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", str(settings.mp3_quality),
               "-write_xing", "1", *_threads(settings), out]
        encoder, ext, mime = "libmp3lame", "mp3", "audio/mpeg"
    else:
        if "libopus" not in enc:
            return EncodeResult("skipped", detail="this FFmpeg build has no Opus encoder")
        out = os.path.join(d, "out.webm")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", src,
               "-vn", "-map", "0:a:0", "-c:a", "libopus", "-b:a", settings.opus_bitrate,
               "-f", "webm", *_threads(settings), out]
        encoder, ext, mime = "libopus", "webm", "audio/webm"
    status = _run_ffmpeg(cmd, p.duration_ms, settings, on_progress, should_cancel)
    if status == "cancelled":
        return EncodeResult("cancelled", detail="cancelled")
    if status == "timeout":
        return EncodeResult("failed", detail="the encode exceeded its time limit")
    if status != "ok":
        return EncodeResult("failed", detail=f"the {encoder} encode failed")
    return _finish(profile, out, ext, mime, encoder, settings, on_progress)


# ── the email poster (§9.4) ─────────────────────────────────────────────────


def _play_button(path: str, size: int) -> None:
    """A dark translucent disc with a white triangle, as a PNG for the overlay.
    Without one, an image of a video reads as a screenshot and is not clicked."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.ellipse((0, 0, size - 1, size - 1), fill=(0, 0, 0, 170))
    s = size
    dr.polygon([(int(s * 0.38), int(s * 0.28)), (int(s * 0.38), int(s * 0.72)),
                (int(s * 0.74), int(s * 0.50))], fill=(255, 255, 255, 255))
    im.save(path)


def _gif_attempts(settings: MediaSettings) -> list:
    """(fps, width, colours, seconds) to try, best-first, until one fits under
    ``gif_max_bytes``. Measured on a real 1080p phone clip (2026-10-03): 560 px at
    8 fps came out at 3.5 MB, over the 2 MiB email budget — a busy scene is the
    normal case, not an edge one, so the poster steps DOWN rather than failing.
    The last rung is a single still frame with the play button: exactly what
    Outlook shows anyway (§9.4), so it is a correct poster, just not animated."""
    w, fps, secs = settings.gif_width, settings.gif_fps, settings.gif_seconds
    return [
        (fps, w, 256, secs),
        (max(4, fps * 3 // 4), w, 128, secs),
        (max(4, fps // 2), max(240, w * 4 // 5), 96, secs),
        (max(3, fps // 2), max(200, w * 2 // 3), 64, max(2, secs * 2 // 3)),
        (0, w, 256, 0),                      # one still frame
    ]


def _encode_gif(src, d, p, settings, on_progress, should_cancel):
    if not p.has_video:
        return EncodeResult("failed", detail="the source has no video track")
    button = os.path.join(d, "play.png")
    try:
        _play_button(button, max(24, settings.gif_width // 5))
    except Exception as e:  # noqa: BLE001 - Pillow missing or broken
        return EncodeResult("failed", detail=f"could not draw the play button ({e})")
    # From early in the clip, but past a black first frame where there is room.
    start = 1 if p.duration_ms > (settings.gif_seconds + 1) * 1000 else 0
    out = os.path.join(d, "out.gif")
    last_size = 0
    for fps, width, colours, secs in _gif_attempts(settings):
        # The overlay is composited onto EVERY frame, starting with the first:
        # Outlook shows only the first frame of a GIF, and the play button is
        # what makes that still read as a video (§9.4).
        if fps:
            head = (f"[0:v]trim=start={start}:duration={secs},setpts=PTS-STARTPTS,"
                    f"fps={fps},scale={width}:-2:flags=lanczos[v];")
        else:
            head = (f"[0:v]trim=start={start}:duration=0.5,setpts=PTS-STARTPTS,"
                    f"scale={width}:-2:flags=lanczos,select=eq(n\\,0)[v];")
        graph = (head + "[v][1:v]overlay=(W-w)/2:(H-h)/2,split[a][b];"
                 f"[a]palettegen=max_colors={colours}:stats_mode=diff[pal];"
                 "[b][pal]paletteuse=dither=bayer:bayer_scale=5")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
               "-i", src, "-i", button, "-filter_complex", graph]
        cmd += ["-loop", "0"] if fps else ["-frames:v", "1"]
        cmd.append(out)
        status = _run_ffmpeg(cmd, max(secs, 1) * 1000, settings, None, should_cancel)
        if status == "cancelled":
            return EncodeResult("cancelled", detail="cancelled")
        if status != "ok":
            return EncodeResult("failed", detail="the email poster encode failed")
        last_size = os.path.getsize(out) if os.path.exists(out) else 0
        if 0 < last_size <= settings.gif_max_bytes:
            return _finish("video-emailposter", out, "gif", "image/gif",
                           "gif" if fps else "gif-still", settings, on_progress)
    return EncodeResult("failed", detail=(f"the email poster is too large ({last_size} bytes, "
                                          f"limit {settings.gif_max_bytes}) even as a still"))
