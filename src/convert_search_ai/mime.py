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

"""MIME-type detection: content sniffing first, extension fallback.

A small built-in magic-byte table covers the common types with no dependency;
``python-magic`` (libmagic) is used when available for everything else; the file
name's extension is the last resort. Always returns *some* type so a plugin can
decide it is ``unsupported`` rather than crash."""
from __future__ import annotations

import mimetypes
from typing import Optional

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
    # 3D / AEC binary formats (XEOKIT3D_PLUGIN).
    (0, b"glTF", "model/gltf-binary"),     # GLB (binary glTF)
    (0, b"LASF", "application/vnd.las"),   # LAS/LAZ point cloud (LAZ refined by ext)
    (0, b"ply\n", "model/ply"),
    (0, b"ply\r", "model/ply"),
    (0, b"#VRML", "model/vrml"),           # VRML world (#VRML V2.0 utf8 / V1.0)
]

# A GENERIC verdict is a floor, not an answer.
#
# Sniffing identifies a FORMAT; several of the types this service converts are a
# CONVENTION over plain text, and no amount of looking at the bytes will
# distinguish them. libmagic answers `text/plain` for Markdown, IFC/STEP, ASCII
# STL, OBJ and YAML alike, and `application/json` for both CityJSON and
# glTF-JSON. Returning that verdict and never consulting the name is what sent
# every .md file to the source-code formatter instead of the document renderer —
# and every .ifc to it as well, instead of the 3D pipeline.
#
# So for these verdicts only, the name gets to refine the answer. A SPECIFIC
# verdict is never overridden: bytes that sniff as a PDF stay a PDF whatever the
# file is called, which is the property that stops a name from talking this
# service into treating one format as another.
GENERIC_TYPES = frozenset({
    "text/plain",
    "application/json",
    "application/octet-stream",   # == DEFAULT, spelled out for grep-ability
})
# Public: the reconcile sweep needs the same rule to find files an earlier,
# name-blind detection mistyped.
_GENERIC_TYPES = GENERIC_TYPES          # kept for callers using the old name

# Text conventions libmagic cannot see and `mimetypes` may not know. Markdown's
# many spellings are all in use in the wild; .yaml/.yml are here because YAML is
# structured data a converter may want to treat as such rather than as source.
_EXT_TEXT = {
    ".markdown": "text/markdown",
    ".mdown": "text/markdown",
    ".mkdn": "text/markdown",
    ".mdwn": "text/markdown",
    ".mkd": "text/markdown",
    ".md": "text/markdown",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}

# Extension map for 3D/AEC types many of which libmagic/mimetypes don't know.
_EXT_3D = {
    ".ifcxml": "application/x-ifc+xml",
    ".ifczip": "application/x-ifc-zip",
    ".ifc": "application/x-ifc",
    ".gltf": "model/gltf+json",
    ".glb": "model/gltf-binary",
    ".city.json": "application/city+json",
    ".laz": "application/vnd.laz",
    ".las": "application/vnd.las",
    ".stl": "model/stl",
    ".ply": "model/ply",
    # CAD formats reachable through the OpenCASCADE (DRAWEXE) → glTF → XKT chain.
    ".step": "model/step",
    ".stp": "model/step",
    ".iges": "model/iges",
    ".igs": "model/iges",
    ".brep": "model/x-brep",
    ".obj": "model/obj",
    ".wrl": "model/vrml",
    ".vrml": "model/vrml",
}

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
    lowered = head.lstrip().lower()
    if lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html"):
        return "text/html"
    return _sniff_text_3d(data, head)


def _sniff_text_3d(data: bytes, head: bytes) -> str | None:
    """Content sniffing for text-based 3D/AEC + CAD formats: IFC/STEP (Part-21),
    IGES, OpenCASCADE BREP, glTF/CityJSON (JSON), and ASCII STL — none of which
    have a fixed binary magic."""
    stripped = head.lstrip()
    # OpenCASCADE BREP shape dump (native or DRAW-saved).
    if stripped.startswith(b"DBRep_DrawableShape") or stripped.startswith(b"CASCADE Topology"):
        return "model/x-brep"
    # IGES: 80-column fixed records; the section letter sits in column 73 and the
    # Start section ("S") is first, followed by a 7-digit sequence number.
    if data[72:73] == b"S" and data[73:80].isdigit():
        return "model/iges"
    # IFC is a STEP/Part-21 physical file; an IFC FILE_SCHEMA marks it as IFC,
    # otherwise it is generic CAD STEP (AP203/AP214/AP242, …).
    if stripped.startswith(b"ISO-10303-21"):
        window = data[:4096]
        if b"FILE_SCHEMA" in window and b"IFC" in window:
            return "application/x-ifc"
        return "model/step"
    # JSON: glTF and CityJSON share the .json/JSON shape — peek at marker keys.
    if stripped[:1] == b"{":
        window = data[:4096].decode("utf-8", "replace")
        if '"CityJSON"' in window:
            return "application/city+json"
        if '"asset"' in window and '"version"' in window:
            return "model/gltf+json"
    # ASCII STL: "solid <name>" followed by facet records (binary STL has no magic).
    if stripped.startswith(b"solid ") and b"facet" in data[:512]:
        return "model/stl"
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


def _by_name(name: str) -> Optional[str]:
    """The type ``name``'s extension implies, or None.

    The curated maps come first because they hold what `mimetypes` gets wrong or
    has never heard of (.ifc, .mkd, .city.json). Longest-suffix wins within each
    map — ".city.json" must beat ".json", and ".ifcxml" must beat neither .ifc nor
    .xml by accident — so they are scanned by descending extension length rather
    than dict order."""
    if not name:
        return None
    lower = name.lower()
    for table in (_EXT_3D, _EXT_TEXT):
        for ext in sorted(table, key=len, reverse=True):
            if lower.endswith(ext):
                return table[ext]
    guess, _ = mimetypes.guess_type(name)
    return guess or None


def _refine(guess: str, name: str) -> str:
    """Let the NAME resolve a generic verdict; never override a specific one.

    The asymmetry is the security property. Refining `text/plain` costs nothing —
    the bytes said "this is text" and the name says which KIND of text, which is
    a claim about convention, not content. Refining a specific verdict would let
    a file called `invoice.pdf` be treated as a PDF because of its name, which is
    exactly the confusion an attacker wants.

    A refinement is also only accepted when it stays in the same neighbourhood:
    text conventions, the curated 3D/AEC map, and text/* from `mimetypes`. A name
    cannot promote plain text to `application/pdf` or `image/png`."""
    if guess not in GENERIC_TYPES:
        return guess
    refined = _by_name(name)
    if not refined or refined == guess:
        return guess
    if refined in _EXT_3D.values() or refined in _EXT_TEXT.values():
        return refined
    if refined.startswith("text/"):
        return refined
    # Anything else (a binary type claimed purely by extension) is ignored: the
    # bytes are the authority on whether this is a document, an image or an
    # archive, and they already answered.
    return guess


def detect(data: bytes, name: str = "") -> str:
    """Best-effort MIME type for ``data`` (with optional file ``name``).

    Content first, then the name — but a generic content verdict (see
    :data:`GENERIC_TYPES`) is refined by the name rather than returned as-is,
    because "it is text" is not a format."""
    if data:
        sniffed = _sniff(data)
        if sniffed:
            return sniffed
        try:  # python-magic, if installed
            import magic  # type: ignore
            guess = magic.from_buffer(bytes(data[:8192]), mime=True)
            if guess and guess != DEFAULT:
                return _refine(guess, name)
        except Exception:
            pass
    named = _by_name(name)
    if named:
        return named
    return DEFAULT
