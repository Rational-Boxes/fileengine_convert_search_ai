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

"""Thin, safe wrappers around the external conversion tools.

LibreOffice / ImageMagick / FFmpeg / poppler are invoked via subprocess on temp
files. Everything degrades gracefully: if a tool is missing or fails, the caller
gets ``None``/``[]`` and the pipeline records the file as partially/unsupported
rather than crashing."""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile

# Default per-conversion wall-clock cap (seconds); overridable per call.
DEFAULT_TIMEOUT = 120


def have(tool: str) -> bool:
    """Is an executable available on PATH?"""
    return shutil.which(tool) is not None


@functools.lru_cache(maxsize=1)
def ffmpeg_encoders() -> frozenset[str]:
    """Encoder names this FFmpeg build supports (e.g. 'libx264', 'libopenh264').

    Distro FFmpeg builds vary in which H.264 encoder is compiled in (Fedora's
    ffmpeg-free ships libopenh264, not libx264), so callers pick from what's
    actually available. Result is cached for the process."""
    out = run_capture(["ffmpeg", "-hide_banner", "-encoders"])
    if not out:
        return frozenset()
    names: set[str] = set()
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split()
        # Encoder rows start with a 6-char flag field (e.g. "V....D"); the second
        # token is the encoder name.
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT, input_bytes: bytes | None = None) -> bool:
    """Run ``cmd``; return True on exit 0. Never raises for tool/exec errors."""
    try:
        proc = subprocess.run(
            cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_capture(cmd: list[str], timeout: int = DEFAULT_TIMEOUT, input_bytes: bytes | None = None) -> bytes | None:
    """Run ``cmd``, return stdout bytes on success, else None."""
    try:
        proc = subprocess.run(
            cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class workdir:
    """Context manager yielding a temp dir that is always cleaned up."""

    def __enter__(self) -> str:
        self._dir = tempfile.mkdtemp(prefix="csai_")
        return self._dir

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)


def write_temp(directory: str, name: str, data: bytes) -> str:
    """Write ``data`` to ``directory/name`` and return the path."""
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def read_if_exists(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None
