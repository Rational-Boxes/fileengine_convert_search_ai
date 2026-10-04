"""MS1 (MEDIA_SHARE.md §4.1, §4.5, §9.4): the publish-grade media renditions.

Run against the REAL FFmpeg on tiny synthetic clips, so what is asserted is what
gets encoded — not what a mocked command line claims would be. Skipped where
FFmpeg is absent. Dimensions are scaled down through MediaSettings so each
encode takes a moment rather than a minute; the rules under test (portrait
stays portrait, the long edge is what is capped, SD is skipped for a small
source) do not depend on the absolute numbers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from convert_search_ai import media_encode as me

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")


def _make(path, *, w=320, h=180, seconds=2, video=True, audio=True,
          vcodec=None, acodec=None, fmt=None):
    """A synthetic source: test pattern + a tone."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if vcodec:
        cmd += ["-c:v", vcodec]
        if vcodec == "libvpx-vp9":
            cmd += ["-deadline", "realtime", "-cpu-used", "8", "-b:v", "200k"]
    if acodec:
        cmd += ["-c:a", acodec]
    if fmt:
        cmd += ["-f", fmt]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    return str(path)


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", path],
                         capture_output=True, check=True, text=True).stdout
    return json.loads(out)


def _video_stream(info):
    return next(s for s in info["streams"] if s["codec_type"] == "video")


# Small caps so encodes are quick: a "720p" of long edge 320, an "SD" of 214.
SMALL = me.MediaSettings(video_height=180, sd_height=120, gif_width=160,
                         gif_seconds=1, gif_fps=4)


@pytest.fixture
def landscape(tmp_path):
    return _make(tmp_path / "land.mp4", w=640, h=360, vcodec="libopenh264", acodec="aac")


# ── the rendition vocabulary ────────────────────────────────────────────────


def test_every_profile_maps_to_a_rendition_fmt_the_pruner_knows():
    from convert_search_ai.renditions import parse_rendition_name, rendition_name
    for profile, (fmt, ext, _mime) in me.PROFILE_OUTPUT.items():
        name = rendition_name("20261003_101010.123", fmt, ext)
        parsed = parse_rendition_name(name)
        assert parsed is not None and parsed[1] == fmt, (profile, name)


# ── video ───────────────────────────────────────────────────────────────────


def test_720p_is_vp9_opus_webm_capped_on_the_long_edge(landscape, tmp_path):
    r = me.encode("video-720p-vp9", landscape, SMALL)
    assert r.status == "ok", r.detail
    assert r.rendition.fmt == "media" and r.rendition.ext == "webm"
    info = _probe(r.rendition.path)
    v = _video_stream(info)
    assert v["codec_name"] == "vp9"
    assert max(v["width"], v["height"]) == 320 and min(v["width"], v["height"]) == 180
    assert any(s["codec_type"] == "audio" and s["codec_name"] == "opus" for s in info["streams"])
    assert r.encoder == "libvpx-vp9"
    assert r.output_bytes == os.path.getsize(r.rendition.path) > 0
    assert 1500 <= r.duration_ms <= 2500
    r.rendition.release()


def test_a_portrait_source_stays_portrait_and_caps_its_long_edge(tmp_path):
    src = _make(tmp_path / "portrait.mp4", w=360, h=640, vcodec="libopenh264", acodec="aac")
    r = me.encode("video-720p-vp9", src, SMALL)
    assert r.status == "ok", r.detail
    v = _video_stream(_probe(r.rendition.path))
    assert v["height"] > v["width"]                       # still portrait
    assert v["height"] == 320 and v["width"] == 180       # long edge capped, not width
    r.rendition.release()


def test_a_small_source_is_never_upscaled(tmp_path):
    src = _make(tmp_path / "tiny.mp4", w=160, h=90, vcodec="libopenh264", acodec="aac")
    r = me.encode("video-720p-vp9", src, SMALL)
    v = _video_stream(_probe(r.rendition.path))
    assert (v["width"], v["height"]) == (160, 90)
    r.rendition.release()


def test_sd_is_skipped_not_failed_for_a_source_already_that_small(tmp_path):
    # SMALL's SD cap is a short edge of 120; this source's short edge is 90.
    src = _make(tmp_path / "small.mp4", w=160, h=90, vcodec="libopenh264", acodec="aac")
    r = me.encode("video-480p-vp9", src, SMALL)
    assert r.status == "skipped" and r.rendition is None


def test_sd_is_produced_for_a_larger_source(landscape):
    r = me.encode("video-480p-vp9", landscape, SMALL)
    assert r.status == "ok", r.detail
    assert r.rendition.fmt == "media_sd"
    v = _video_stream(_probe(r.rendition.path))
    assert min(v["width"], v["height"]) == 120
    r.rendition.release()


def test_a_conformant_vp9_opus_webm_is_remuxed_not_re_encoded(tmp_path):
    src = _make(tmp_path / "ok.webm", w=320, h=180, vcodec="libvpx-vp9", acodec="libopus")
    r = me.encode("video-720p-vp9", src, SMALL)
    assert r.status == "ok", r.detail
    assert r.encoder == "copy"
    r.rendition.release()


def test_the_ladder_falls_back_to_vp8_without_vp9(landscape, monkeypatch):
    r = me.encode("video-720p-vp9", landscape, SMALL,
                  encoders={"libvpx", "libopus", "libmp3lame", "libopenh264", "aac"})
    assert r.status == "ok", r.detail
    assert r.encoder == "libvpx"
    assert _video_stream(_probe(r.rendition.path))["codec_name"] == "vp8"
    r.rendition.release()


def test_the_mp4_fallback_is_faststart(landscape):
    r = me.encode("video-720p-vp9", landscape, SMALL, encoders={"libopenh264", "aac"})
    assert r.status == "ok", r.detail
    assert r.rendition.ext == "mp4" and r.rendition.mime == "video/mp4"
    with open(r.rendition.path, "rb") as f:
        head = f.read(4096)
    # +faststart puts the index (moov) before the media data (mdat).
    assert head.find(b"moov") != -1 and (head.find(b"mdat") == -1 or head.find(b"moov") < head.find(b"mdat"))
    r.rendition.release()


def test_no_usable_video_encoder_fails_with_a_reason(landscape):
    r = me.encode("video-720p-vp9", landscape, SMALL, encoders={"libopus"})
    assert r.status == "failed" and "encoder" in r.detail


# ── audio ───────────────────────────────────────────────────────────────────


def test_mp3_is_v0_with_a_xing_header_and_the_right_duration(tmp_path):
    src = _make(tmp_path / "tone.wav", video=False, seconds=3)
    r = me.encode("audio-mp3", src, SMALL)
    assert r.status == "ok", r.detail
    assert r.rendition.fmt == "audio" and r.rendition.mime == "audio/mpeg"
    with open(r.rendition.path, "rb") as f:
        head = f.read(4096)
    assert b"Xing" in head or b"Info" in head        # the VBR seek/duration header
    assert 2500 <= r.duration_ms <= 3500
    r.rendition.release()


def test_opus_is_webm_audio(tmp_path):
    src = _make(tmp_path / "tone.wav", video=False, seconds=2)
    r = me.encode("audio-opus", src, SMALL)
    assert r.status == "ok", r.detail
    assert r.rendition.fmt == "audio_opus" and r.rendition.mime == "audio/webm"
    info = _probe(r.rendition.path)
    assert [s["codec_name"] for s in info["streams"]] == ["opus"]
    r.rendition.release()


def test_without_libopus_the_opus_rendition_is_skipped_not_failed(tmp_path):
    src = _make(tmp_path / "tone.wav", video=False, seconds=2)
    r = me.encode("audio-opus", src, SMALL, encoders={"libmp3lame"})
    assert r.status == "skipped"


# ── the email poster (§9.4) ─────────────────────────────────────────────────


def test_the_email_poster_is_a_small_gif_whose_first_frame_carries_the_play_button(landscape):
    from PIL import Image
    r = me.encode("video-emailposter", landscape, SMALL)
    assert r.status == "ok", r.detail
    assert r.rendition.fmt == "emailposter" and r.rendition.mime == "image/gif"
    assert r.output_bytes <= SMALL.gif_max_bytes
    with Image.open(r.rendition.path) as im:
        assert im.format == "GIF"
        assert im.width == SMALL.gif_width
        im.seek(0)                               # Outlook shows ONLY the first frame
        first = im.convert("RGB")
        cx, cy = first.width // 2, first.height // 2
        # The centre of the play button is its white triangle on a dark disc.
        assert first.getpixel((cx, cy))[0] > 200
    r.rendition.release()


def test_an_email_poster_over_the_size_cap_is_refused(landscape):
    tight = me.MediaSettings(video_height=180, sd_height=120, gif_width=160,
                             gif_seconds=1, gif_fps=4, gif_max_bytes=100)
    r = me.encode("video-emailposter", landscape, tight)
    assert r.status == "failed" and "too large" in r.detail and "still" in r.detail


# ── progress and cancellation ───────────────────────────────────────────────


def test_progress_is_reported_and_reaches_the_end(landscape):
    seen = []
    r = me.encode("video-720p-vp9", landscape, SMALL, on_progress=seen.append)
    assert r.status == "ok", r.detail
    assert seen and seen == sorted(seen) and seen[-1] == 100
    r.rendition.release()


def test_cancellation_kills_the_encode_and_leaves_nothing(tmp_path):
    src = _make(tmp_path / "long.mp4", w=640, h=360, seconds=20,
                vcodec="libopenh264", acodec="aac")
    r = me.encode("video-720p-vp9", src, me.MediaSettings(), should_cancel=lambda: True)
    assert r.status == "cancelled" and r.rendition is None


def test_an_output_over_the_ceiling_is_failed_and_discarded(landscape):
    capped = me.MediaSettings(video_height=180, sd_height=120, max_output_bytes=10)
    r = me.encode("video-720p-vp9", landscape, capped)
    assert r.status == "failed" and r.rendition is None and "exceeds" in r.detail


def test_an_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        me.encode("video-4k-av1", "/nonexistent", SMALL)


def test_a_poster_over_budget_steps_down_rather_than_failing(landscape):
    # A budget the first rung cannot meet but a smaller one can: the result is a
    # poster, not a failure — a busy phone clip is the normal case (2026-10-03).
    first = me.encode("video-emailposter", landscape, SMALL)
    assert first.status == "ok"
    budget = first.output_bytes - 1
    first.rendition.release()
    tight = me.MediaSettings(video_height=180, sd_height=120, gif_width=160,
                             gif_seconds=1, gif_fps=4, gif_max_bytes=budget)
    r = me.encode("video-emailposter", landscape, tight)
    assert r.status == "ok", r.detail
    assert r.output_bytes <= budget
    from PIL import Image
    with Image.open(r.rendition.path) as im:
        im.seek(0)
        first_frame = im.convert("RGB")
        assert first_frame.getpixel((first_frame.width // 2, first_frame.height // 2))[0] > 200
    r.rendition.release()


# ── the bitrate ceiling (decided 2026-10-03) ────────────────────────────────
#
# Constant quality with no ceiling let busy footage get large: a real 1080p
# garden clip came out at 8.15 Mb/s at 720p. Constrained quality now caps 720p at
# 2.5 Mb/s and 480p at 1.2 Mb/s — a talking head stays under the cap anyway.

def _captured(monkeypatch):
    seen = []
    real = me._run_ffmpeg

    def spy(cmd, *a, **k):
        seen.append(list(cmd))
        return real(cmd, *a, **k)
    monkeypatch.setattr(me, "_run_ffmpeg", spy)
    return seen


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_hd_vp9_is_capped(landscape, monkeypatch):
    seen = _captured(monkeypatch)
    r = me.encode("video-720p-vp9", landscape, SMALL)
    assert r.status == "ok", r.detail
    (cmd,) = seen
    assert _arg(cmd, "-crf") == "31" and _arg(cmd, "-b:v") == "2500k"
    r.rendition.release()


def test_sd_vp9_is_capped_lower(landscape, monkeypatch):
    seen = _captured(monkeypatch)
    r = me.encode("video-480p-vp9", landscape, SMALL)
    assert r.status == "ok", r.detail
    assert _arg(seen[0], "-b:v") == "1200k"
    r.rendition.release()


def test_the_fallback_encoders_respect_the_cap(landscape, monkeypatch):
    seen = _captured(monkeypatch)
    r = me.encode("video-480p-vp9", landscape, SMALL, encoders={"libopenh264", "aac"})
    assert r.status == "ok", r.detail
    assert _arg(seen[0], "-b:v") == "1200k"
    r.rendition.release()


def test_the_cap_is_configurable():
    from types import SimpleNamespace
    s = me.MediaSettings.from_config(SimpleNamespace(media_video_max_bitrate="4M",
                                                     media_sd_max_bitrate="900k"))
    assert (s.video_max_bitrate, s.sd_max_bitrate) == ("4M", "900k")


def test_a_conformant_source_over_the_cap_is_re_encoded_not_copied(tmp_path):
    # A VP9/Opus WebM that would otherwise be remuxed, but at a bitrate above the
    # ceiling: copying it would publish a file the cap exists to prevent.
    src = tmp_path / "fat.webm"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=25:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "libvpx-vp9", "-deadline",
                    "realtime", "-cpu-used", "8", "-b:v", "6M", "-minrate", "6M", "-maxrate", "6M",
                    "-c:a", "libopus", str(src)], check=True)
    # The ceiling is set far below what the encoder actually produced (~230 kb/s
    # for this pattern), so the source is unambiguously over it.
    tight = me.MediaSettings(video_height=180, sd_height=120, video_max_bitrate="50k")
    r = me.encode("video-720p-vp9", str(src), tight)
    assert r.status == "ok", r.detail
    assert r.encoder != "copy"
    r.rendition.release()


# ── nothing very long is served (decided 2026-10-03) ────────────────────────
#
# FileEngine publishes short clips. A long video belongs on YouTube, Vimeo or
# PeerTube, so it is refused at publish — before any CPU or storage is spent —
# with a message that says where to put it instead.

def test_a_source_over_the_duration_limit_is_refused_before_encoding(landscape, monkeypatch):
    seen = _captured(monkeypatch)
    limited = me.MediaSettings(video_height=180, sd_height=120, max_duration_seconds=1)
    r = me.encode("video-720p-vp9", landscape, limited)          # a 2 s source
    assert r.status == "failed" and r.rendition is None
    assert "YouTube" in r.detail and "PeerTube" in r.detail
    assert seen == []                                            # no encode was started


def test_the_limit_applies_to_audio_too(tmp_path):
    src = _make(tmp_path / "tone.wav", video=False, seconds=3)
    r = me.encode("audio-mp3", src, me.MediaSettings(max_duration_seconds=1))
    assert r.status == "failed" and "too long" in r.detail


def test_zero_means_no_limit(landscape):
    r = me.encode("video-720p-vp9", landscape,
                  me.MediaSettings(video_height=180, sd_height=120, max_duration_seconds=0))
    assert r.status == "ok"
    r.rendition.release()


def test_the_default_limit_is_ten_minutes():
    assert me.MediaSettings().max_duration_seconds == 600
