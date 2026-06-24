"""Thin, safe wrappers around the external conversion tools.

LibreOffice / ImageMagick / FFmpeg / poppler are invoked via subprocess on temp
files. Everything degrades gracefully: if a tool is missing or fails, the caller
gets ``None``/``[]`` and the pipeline records the file as partially/unsupported
rather than crashing."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

# Default per-conversion wall-clock cap (seconds); overridable per call.
DEFAULT_TIMEOUT = 120


def have(tool: str) -> bool:
    """Is an executable available on PATH?"""
    return shutil.which(tool) is not None


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
