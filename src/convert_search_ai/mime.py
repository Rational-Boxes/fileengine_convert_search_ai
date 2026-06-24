"""MIME-type detection: content sniffing first, extension fallback.

A small built-in magic-byte table covers the common types with no dependency;
``python-magic`` (libmagic) is used when available for everything else; the file
name's extension is the last resort. Always returns *some* type so a plugin can
decide it is ``unsupported`` rather than crash."""
from __future__ import annotations

import mimetypes

DEFAULT = "application/octet-stream"

# (offset, signature, mime). Ordered; first match wins.
_MAGIC = [
    (0, b"%PDF-", "application/pdf"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"RIFF", "image/webp"),          # refined below if WEBP
    (0, b"\x00\x00\x01\x00", "image/x-icon"),
    (0, b"II*\x00", "image/tiff"),
    (0, b"MM\x00*", "image/tiff"),
    (0, b"\x1a\x45\xdf\xa3", "video/x-matroska"),
    (0, b"OggS", "video/ogg"),
    (0, b"%!PS", "application/postscript"),
]

# Office Open XML / OpenDocument are ZIP containers — disambiguate by member.
_ZIP_SIG = b"PK\x03\x04"
_OOXML = {
    "word/": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xl/": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt/": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _sniff(data: bytes) -> str | None:
    head = data[:64]
    if head[:4] == _ZIP_SIG:
        return _sniff_zip(data)
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for offset, sig, mime in _MAGIC:
        if head[offset:offset + len(sig)] == sig:
            return mime
    # ftyp box near the start => ISO base media (mp4 / mov / m4v)
    if data[4:8] == b"ftyp":
        return "video/mp4"
    return None


def _sniff_zip(data: bytes) -> str:
    try:
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "mimetype" in names:                      # OpenDocument
                mt = zf.read("mimetype").decode("ascii", "ignore").strip()
                if mt:
                    return mt
            for prefix, mime in _OOXML.items():
                if any(n.startswith(prefix) for n in names):
                    return mime
    except Exception:
        pass
    return "application/zip"


def detect(data: bytes, name: str = "") -> str:
    """Best-effort MIME type for ``data`` (with optional file ``name``)."""
    if data:
        sniffed = _sniff(data)
        if sniffed:
            return sniffed
        try:  # python-magic, if installed
            import magic  # type: ignore
            guess = magic.from_buffer(bytes(data[:8192]), mime=True)
            if guess and guess != DEFAULT:
                return guess
        except Exception:
            pass
    if name:
        guess, _ = mimetypes.guess_type(name)
        if guess:
            return guess
    return DEFAULT
