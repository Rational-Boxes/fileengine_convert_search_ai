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
    # 3D / AEC binary formats (XEOKIT3D_PLUGIN).
    (0, b"glTF", "model/gltf-binary"),     # GLB (binary glTF)
    (0, b"LASF", "application/vnd.las"),   # LAS/LAZ point cloud (LAZ refined by ext)
    (0, b"ply\n", "model/ply"),
    (0, b"ply\r", "model/ply"),
    (0, b"#VRML", "model/vrml"),           # VRML world (#VRML V2.0 utf8 / V1.0)
]

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
        lower = name.lower()
        for ext, mime in _EXT_3D.items():
            if lower.endswith(ext):
                return mime
        guess, _ = mimetypes.guess_type(name)
        if guess:
            return guess
    return DEFAULT
