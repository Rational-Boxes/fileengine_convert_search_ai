"""3D / BIM / CAD conversion + searchable-text extraction (XEOKIT3D_PLUGIN doc).

For open 3D/AEC + CAD formats (IFC, glTF/GLB, CityJSON, LAS/LAZ, STL, PLY, plus
STEP, IGES, BREP, OBJ and VRML) this plugin:

- **extracts** every human-readable string (names, descriptions, property sets,
  attributes, header metadata) as Markdown for FTS + vector search — with **no
  hard dependency** (IFC/STEP use a built-in STEP/Part-21 scanner; richer IFC
  output via the optional ``ifcopenshell``);
- **renders** an XKT model rendition via xeokit's ``convert2xkt`` (Node). For IFC
  the geometry backend is auto-detected and degrades gracefully:
  CxConverter → IfcOpenShell (``ifcConvert``) → native ``web-ifc`` (convert2xkt).
  True-CAD/mesh formats that ``convert2xkt`` cannot ingest (STEP, IGES, BREP, OBJ,
  VRML) are routed through **OpenCASCADE** (``DRAWEXE``): read → tessellate →
  glTF, then chained through the same ``convert2xkt`` → XKT final hop.

Everything is fail-soft: missing tools/libraries yield ``[]``/``None`` rather than
errors, exactly like the other plugins.
"""
from __future__ import annotations

import json
import os
import re
from typing import List, NamedTuple, Optional

from .base import ConversionPlugin, Rendition
from .. import tools

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_MAX_STRINGS = 20000  # safety cap on extracted strings per file


def _as_float(val, default: float) -> float:
    """Coerce a config value to float (defends DRAWEXE script interpolation against
    a malformed env override); falls back to ``default``."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _dedupe(seq) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for s in seq:
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= _MAX_STRINGS:
                break
    return out


# --------------------------------------------------------------------------- #
# IFC (STEP / Part-21) — native, dependency-free text extraction
# --------------------------------------------------------------------------- #

_SCHEMA_RE = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", re.IGNORECASE)
_ENTITY_RE = re.compile(r"\s*#\d+\s*=\s*([A-Za-z0-9_]+)\s*\((.*)\)\s*$", re.DOTALL)
_X2_RE = re.compile(r"\\X2\\([0-9A-Fa-f]+)\\X0\\")
_X1_RE = re.compile(r"\\X\\([0-9A-Fa-f]{2})")


def _decode_step_unicode(s: str) -> str:
    """Decode STEP string unicode escapes (``\\X2\\HHHH..\\X0\\`` UTF-16BE and
    ``\\X\\HH`` latin-1). Plain text passes through untouched."""
    if "\\X" not in s:
        return s
    s = _X2_RE.sub(lambda m: _safe_hex(m.group(1), "utf-16-be"), s)
    s = _X1_RE.sub(lambda m: _safe_hex(m.group(1), "latin-1"), s)
    return s


def _safe_hex(hexs: str, enc: str) -> str:
    try:
        return bytes.fromhex(hexs).decode(enc, "replace")
    except Exception:
        return ""


def _quoted_strings(s: str) -> List[str]:
    """All single-quoted STEP strings in ``s`` (doubled '' -> ' unescaped)."""
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "'":
            j = i + 1
            buf: List[str] = []
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":  # escaped quote
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            out.append(_decode_step_unicode("".join(buf)))
            i = j + 1
        else:
            i += 1
    return out


def _split_statements(text: str) -> List[str]:
    """Split a STEP file into ``;``-terminated statements, respecting quoted
    strings and skipping ``/* … */`` comments."""
    out: List[str] = []
    buf: List[str] = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            buf.append(c)
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
        elif c == "'":
            in_str = True
            buf.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":  # STEP comment
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif c == ";":
            out.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    if buf:
        out.append("".join(buf))
    return out


def _pretty_ifc_type(typ: str) -> str:
    """``IFCBUILDINGSTOREY`` -> ``IfcBuildingstorey`` (readable, search-friendly)."""
    u = typ.upper()
    if u.startswith("IFC"):
        return "Ifc" + u[3:].capitalize()
    return typ


def ifc_text_native(data: bytes) -> Optional[str]:
    """Extract human-readable strings from an IFC (STEP) file with no dependency.

    Groups quoted strings by entity type so element names/descriptions, property
    values, materials and classifications are all indexed. Returns ``None`` for
    non-IFC input."""
    if not data:
        return None
    text = data.decode("utf-8", "replace")
    if not text.lstrip().startswith("ISO-10303-21"):
        return None

    schema = ""
    m = _SCHEMA_RE.search(text)
    if m:
        schema = m.group(1)

    groups: "dict[str, List[str]]" = {}
    header: List[str] = []
    for st in _split_statements(text):
        em = _ENTITY_RE.match(st)
        if em:
            typ = em.group(1).upper()
            strings = _quoted_strings(em.group(2))
            if strings:
                groups.setdefault(typ, []).extend(strings)
        else:
            up = st.lstrip().upper()
            if up.startswith("FILE_NAME") or up.startswith("FILE_DESCRIPTION"):
                header.extend(_quoted_strings(st))

    if not groups and not schema:
        return None

    lines: List[str] = [f"# IFC model" + (f" (schema {schema})" if schema else "")]
    header = _dedupe(header)
    if header:
        lines.append("")
        lines.append("## Document")
        lines += [f"- {h}" for h in header]
    for typ, strings in groups.items():
        vals = _dedupe(strings)
        if not vals:
            continue
        lines.append("")
        lines.append(f"## {_pretty_ifc_type(typ)}")
        lines += [f"- {v}" for v in vals]
    return "\n".join(lines)


def _ifc_text_ifcopenshell(data: bytes) -> Optional[str]:
    """Richer IFC text via the optional ``ifcopenshell`` library (resolved
    per-instance: type, Name, Description, ObjectType, Tag). ``None`` if the
    library is absent or parsing fails."""
    try:
        import ifcopenshell  # type: ignore
    except Exception:
        return None
    try:
        with tools.workdir() as d:
            path = tools.write_temp(d, "in.ifc", data)
            model = ifcopenshell.open(path)
            lines = [f"# IFC model (schema {model.schema})"]
            by_type: "dict[str, List[str]]" = {}
            for el in model.by_type("IfcRoot"):
                bits = []
                for attr in ("Name", "Description", "ObjectType", "Tag"):
                    val = getattr(el, attr, None)
                    if isinstance(val, str) and val.strip():
                        bits.append(val.strip())
                if bits:
                    by_type.setdefault(el.is_a(), []).append(" — ".join(_dedupe(bits)))
            for typ, rows in by_type.items():
                lines.append("")
                lines.append(f"## {typ}")
                lines += [f"- {r}" for r in _dedupe(rows)]
            return "\n".join(lines) if len(lines) > 1 else None
    except Exception:
        return None


def extract_ifc_text(data: bytes) -> Optional[str]:
    """Best available IFC text: ifcopenshell if importable, else the native scan."""
    md = _ifc_text_ifcopenshell(data)
    if md and md.strip():
        return md
    return ifc_text_native(data)


# --------------------------------------------------------------------------- #
# STEP (ISO-10303-21) — generic CAD, reuses the IFC/Part-21 scanner
# --------------------------------------------------------------------------- #

def _pretty_step_type(typ: str) -> str:
    """``PRODUCT_DEFINITION`` -> ``Product definition`` (readable, search-friendly)."""
    return typ.replace("_", " ").strip().capitalize() or typ


def extract_step_text(data: bytes) -> Optional[str]:
    """Human-readable strings from a generic STEP (AP203/AP214/AP242) file.

    STEP is the same Part-21 physical file as IFC, so the dependency-free Part-21
    scanner (``_split_statements``/``_quoted_strings``) applies directly: it pulls
    the header (author, organization, originating system) and every entity's quoted
    strings — product names, descriptions, person/organization names, units — for
    FTS + vector search. Returns ``None`` for non-STEP input."""
    if not data:
        return None
    text = data.decode("utf-8", "replace")
    if not text.lstrip().startswith("ISO-10303-21"):
        return None

    schema = ""
    m = _SCHEMA_RE.search(text)
    if m:
        schema = m.group(1)

    groups: "dict[str, List[str]]" = {}
    header: List[str] = []
    for st in _split_statements(text):
        em = _ENTITY_RE.match(st)
        if em:
            typ = em.group(1).upper()
            strings = _quoted_strings(em.group(2))
            if strings:
                groups.setdefault(typ, []).extend(strings)
        else:
            up = st.lstrip().upper()
            if up.startswith("FILE_NAME") or up.startswith("FILE_DESCRIPTION"):
                header.extend(_quoted_strings(st))

    if not groups and not schema:
        return None

    lines: List[str] = ["# STEP CAD model" + (f" (schema {schema})" if schema else "")]
    header = _dedupe(header)
    if header:
        lines += ["", "## Document"] + [f"- {h}" for h in header]
    for typ, strings in groups.items():
        vals = _dedupe(strings)
        if not vals:
            continue
        lines += ["", f"## {_pretty_step_type(typ)}"] + [f"- {v}" for v in vals]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# IGES (fixed 80-column records; Start + Global sections carry the text)
# --------------------------------------------------------------------------- #

_HOLLERITH_RE = re.compile(r"(\d+)H")


def _hollerith_strings(s: str) -> List[str]:
    """Hollerith-encoded strings (``<count>H<chars>``) used in the IGES Global
    section, e.g. ``31HOpen CASCADE IGES processor 7.9``."""
    out: List[str] = []
    for m in _HOLLERITH_RE.finditer(s):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        start = m.end()
        out.append(s[start:start + n])
    return out


def extract_iges_text(data: bytes) -> Optional[str]:
    """Free text from an IGES file: the Start section (cols 1–72 of ``S`` records)
    and the Hollerith strings of the Global section (``G`` records). ``None`` for
    non-IGES input."""
    if not data:
        return None
    start: List[str] = []
    glob: List[str] = []
    for ln in data.decode("latin-1", "replace").splitlines():
        if len(ln) < 73:
            continue
        sec = ln[72]
        if sec == "S":
            body = ln[:72].strip()
            if body:
                start.append(body)
        elif sec == "G":
            glob.append(ln[:72])
    if not start and not glob:
        return None
    out: List[str] = ["# IGES CAD model"]
    start = _dedupe(start)
    if start:
        out += ["", "## Start section"] + [f"- {s}" for s in start]
    holl = [h for h in _hollerith_strings("".join(glob))
            if h.strip() and not h.replace(".", "").replace("-", "").isdigit()]
    holl = _dedupe(holl)
    if holl:
        out += ["", "## Global"] + [f"- {h}" for h in holl]
    return "\n".join(out) if len(out) > 1 else None


# --------------------------------------------------------------------------- #
# OBJ (Wavefront) / VRML — names + comments
# --------------------------------------------------------------------------- #

def extract_obj_text(data: bytes) -> Optional[str]:
    """Object/group/material names and comments from a Wavefront OBJ file."""
    if not data:
        return None
    names: List[str] = []
    comments: List[str] = []
    for ln in data.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        if ln.startswith("#"):
            c = ln[1:].strip()
            if c:
                comments.append(c)
        elif ln[:2] in ("o ", "g "):
            names.append(ln[2:].strip())
        elif ln.startswith(("usemtl", "mtllib")):
            parts = ln.split(None, 1)
            if len(parts) == 2:
                names.append(parts[1].strip())
    names = _dedupe(names)
    comments = _dedupe(comments)
    if not names and not comments:
        return None
    out: List[str] = ["# OBJ mesh"]
    if comments:
        out += ["", "## Comments"] + [f"- {c}" for c in comments]
    if names:
        out += ["", "## Names"] + [f"- {n}" for n in names]
    return "\n".join(out)


_VRML_DEF_RE = re.compile(r"\bDEF\s+([A-Za-z0-9_]+)")
_VRML_STR_RE = re.compile(r'"([^"]*)"')


def extract_vrml_text(data: bytes) -> Optional[str]:
    """Node ``DEF`` names and quoted strings (WorldInfo title/info, URLs) from a
    VRML/X3D world. ``None`` when nothing readable is present."""
    if not data:
        return None
    text = data.decode("utf-8", "replace")
    defs = _dedupe(_VRML_DEF_RE.findall(text))
    strs = _dedupe([s for s in _VRML_STR_RE.findall(text) if s.strip()])
    if not defs and not strs:
        return None
    out: List[str] = ["# VRML model"]
    if defs:
        out += ["", "## Names"] + [f"- {d}" for d in defs]
    if strs:
        out += ["", "## Strings"] + [f"- {s}" for s in strs]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# glTF / GLB
# --------------------------------------------------------------------------- #

def _gltf_json(data: bytes, mime: str) -> Optional[dict]:
    if data[:4] == b"glTF":  # GLB: 12-byte header, then a JSON chunk
        if len(data) < 20:
            return None
        clen = int.from_bytes(data[12:16], "little")
        if data[16:20] != b"JSON":
            return None
        chunk = data[20:20 + clen]
        return json.loads(chunk.decode("utf-8", "replace"))
    return json.loads(data.decode("utf-8", "replace"))


_GLTF_NAMED = (
    "scenes", "nodes", "meshes", "materials", "cameras",
    "animations", "images", "skins", "textures",
)


def extract_gltf_text(data: bytes, mime: str) -> Optional[str]:
    obj = _gltf_json(data, mime)
    if not isinstance(obj, dict):
        return None
    strings: List[str] = []
    asset = obj.get("asset") or {}
    for k in ("generator", "copyright"):
        if isinstance(asset.get(k), str):
            strings.append(asset[k])
    names: List[str] = []
    for coll in _GLTF_NAMED:
        for item in obj.get(coll) or []:
            if isinstance(item, dict):
                if isinstance(item.get("name"), str):
                    names.append(item["name"])
                extras = item.get("extras")
                if isinstance(extras, dict):
                    names += [v for v in extras.values() if isinstance(v, str)]
    lines = ["# glTF model"]
    if isinstance(asset.get("generator"), str):
        lines.append(f"Generator: {asset['generator']}")
    if isinstance(asset.get("copyright"), str):
        lines.append(f"Copyright: {asset['copyright']}")
    names = _dedupe(names)
    if names:
        lines.append("")
        lines.append("## Names")
        lines += [f"- {n}" for n in names]
    return "\n".join(lines) if (names or len(lines) > 1) else None


# --------------------------------------------------------------------------- #
# CityJSON
# --------------------------------------------------------------------------- #

def extract_cityjson_text(data: bytes) -> Optional[str]:
    obj = json.loads(data.decode("utf-8", "replace"))
    if not isinstance(obj, dict):
        return None
    lines = ["# CityJSON model"]
    meta = obj.get("metadata") or {}
    for k in ("title", "referenceSystem"):
        if isinstance(meta.get(k), str):
            lines.append(f"{k}: {meta[k]}")
    rows: List[str] = []
    for oid, co in (obj.get("CityObjects") or {}).items():
        if not isinstance(co, dict):
            continue
        parts = [str(oid)]
        if isinstance(co.get("type"), str):
            parts.append(co["type"])
        for k, v in (co.get("attributes") or {}).items():
            if isinstance(v, str):
                parts.append(f"{k}: {v}")
        rows.append(" — ".join(parts))
    rows = _dedupe(rows)
    if rows:
        lines.append("")
        lines.append("## City objects")
        lines += [f"- {r}" for r in rows]
    return "\n".join(lines) if rows or len(lines) > 1 else None


# --------------------------------------------------------------------------- #
# LAS / LAZ (point cloud header)
# --------------------------------------------------------------------------- #

def extract_las_text(data: bytes) -> Optional[str]:
    if data[:4] != b"LASF" or len(data) < 96:
        return None
    vmaj, vmin = data[24], data[25]
    sysid = data[26:58].split(b"\x00")[0].decode("latin-1", "replace").strip()
    soft = data[58:90].split(b"\x00")[0].decode("latin-1", "replace").strip()
    lines = [f"# Point cloud (LAS {vmaj}.{vmin})"]
    if sysid:
        lines.append(f"System: {sysid}")
    if soft:
        lines.append(f"Software: {soft}")
    return "\n".join(lines) if len(lines) > 1 else None


# --------------------------------------------------------------------------- #
# STL / PLY (mesh headers)
# --------------------------------------------------------------------------- #

def extract_stl_text(data: bytes) -> Optional[str]:
    head = data[:512].decode("latin-1", "replace")
    if head.lstrip().lower().startswith("solid"):
        first = head.splitlines()[0].strip()
        name = first[len("solid"):].strip()
        return f"# STL mesh\nName: {name}" if name else None
    # Binary STL: an 80-byte header sometimes carries a comment/name.
    hdr = data[:80].decode("latin-1", "replace").replace("\x00", " ").strip()
    return f"# STL mesh\n{hdr}" if hdr else None


def extract_ply_text(data: bytes) -> Optional[str]:
    head = data[:8192].decode("latin-1", "replace")
    if not head.lstrip().lower().startswith("ply"):
        return None
    keep: List[str] = []
    for line in head.splitlines():
        line = line.strip()
        if line == "end_header":
            break
        if line.startswith(("comment", "obj_info", "element")):
            keep.append(line)
    keep = _dedupe(keep)
    return "# PLY mesh\n" + "\n".join(keep) if keep else None


# --------------------------------------------------------------------------- #
# Geometry backends (-> XKT) and the plugin
# --------------------------------------------------------------------------- #

# Formats convert2xkt ingests directly (no intermediate step).
_EXT_BY_MIME = {
    "application/x-ifc": "ifc",
    "model/gltf+json": "gltf",
    "model/gltf-binary": "glb",
    "application/city+json": "json",
    "application/vnd.las": "las",
    "application/vnd.laz": "laz",
    "model/stl": "stl",
    "model/ply": "ply",
}


class _OcctSpec(NamedTuple):
    """How OpenCASCADE (DRAWEXE) reads a CAD/mesh format on the way to glTF."""
    ext: str        # temp-file extension DRAWEXE expects
    read: str       # DRAW read command
    into_doc: bool  # True: read straight into an XDE document; False: read a shape
    mesh: bool      # True: exact BRep geometry needing tessellation before export


# CAD/mesh formats convert2xkt cannot ingest, reachable via DRAWEXE → glTF.
_OCCT_SPECS = {
    "model/step":   _OcctSpec("step", "ReadStep", True, True),
    "model/iges":   _OcctSpec("iges", "ReadIges", True, True),
    "model/x-brep": _OcctSpec("brep", "readbrep", False, True),
    "model/obj":    _OcctSpec("obj", "ReadObj", True, False),
    "model/vrml":   _OcctSpec("wrl", "ReadVrml", True, False),
}


class Xeokit3DPlugin(ConversionPlugin):
    name = "model3d"

    _MIMES = (
        frozenset(_EXT_BY_MIME)
        | frozenset(_OCCT_SPECS)
        | {"application/x-ifc+xml", "application/x-ifc-zip"}
    )

    def __init__(self, config=None):
        self.enabled = getattr(config, "threed_enabled", True)
        self.ifc_backend = getattr(config, "threed_ifc_backend", "auto")
        self.convert2xkt = getattr(config, "threed_convert2xkt", "convert2xkt")
        self.ifcconvert = getattr(config, "threed_ifcconvert", "ifcConvert")
        self.cxconverter = getattr(config, "threed_cxconverter", "") or ""
        self.max_input_mb = getattr(config, "threed_max_input_mb", 512)
        self.timeout_s = getattr(config, "threed_timeout_s", 600)
        self.extract_only = getattr(config, "threed_extract_only", False)
        self.drawexe = getattr(config, "threed_drawexe", "DRAWEXE")
        self.cad_deflection = _as_float(getattr(config, "threed_cad_deflection", "0.001"), 0.001)
        self.cad_angle = _as_float(getattr(config, "threed_cad_angle", "20"), 20.0)
        self.cad_recenter = getattr(config, "threed_cad_recenter", True)

    def supports(self, mime: str) -> bool:
        return mime in self._MIMES

    # --- searchable text -------------------------------------------------- #

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        try:
            if mime == "application/x-ifc":
                return extract_ifc_text(data)
            if mime in ("model/gltf+json", "model/gltf-binary"):
                return extract_gltf_text(data, mime)
            if mime == "application/city+json":
                return extract_cityjson_text(data)
            if mime in ("application/vnd.las", "application/vnd.laz"):
                return extract_las_text(data)
            if mime == "model/stl":
                return extract_stl_text(data)
            if mime == "model/ply":
                return extract_ply_text(data)
            if mime == "model/step":
                return extract_step_text(data)
            if mime == "model/iges":
                return extract_iges_text(data)
            if mime == "model/obj":
                return extract_obj_text(data)
            if mime == "model/vrml":
                return extract_vrml_text(data)
            # model/x-brep is pure geometry — no human-readable text to index.
        except Exception:
            return None
        return None

    # --- geometry -> XKT rendition --------------------------------------- #

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        if not self.enabled or self.extract_only or not data:
            return []
        if len(data) > self.max_input_mb * 1024 * 1024:
            return []
        if not tools.have(self.convert2xkt):
            return []
        xkt = self._to_xkt(data, mime)
        if not xkt:
            return []
        return [Rendition("model", "xkt", xkt, "application/octet-stream")]

    def _to_xkt(self, data: bytes, mime: str) -> Optional[bytes]:
        if mime == "application/x-ifc":
            for backend in self._ifc_backends():
                xkt = backend(data)
                if xkt:
                    return xkt
            return None
        if mime in _OCCT_SPECS:
            return self._occt_to_xkt(data, mime)
        return self._convert2xkt_direct(data, _EXT_BY_MIME.get(mime, "bin"))

    def _ifc_backend_order(self) -> List[str]:
        order = (self.ifc_backend or "auto").strip().lower()
        if order == "auto":
            names: List[str] = []
            if self.cxconverter and tools.have(self.cxconverter):
                names.append("cxconverter")
            if tools.have(self.ifcconvert):
                names.append("ifcopenshell")
            names.append("webifc")
            return names
        return [s.strip() for s in order.split(",") if s.strip()]

    def _ifc_backends(self):
        impls = {
            "cxconverter": self._ifc_via_cxconverter,
            "ifcopenshell": self._ifc_via_ifcconvert,
            "webifc": self._ifc_via_webifc,
        }
        return [impls[n] for n in self._ifc_backend_order() if n in impls]

    def _ifc_via_webifc(self, data: bytes) -> Optional[bytes]:
        # convert2xkt ingests .ifc directly via its bundled web-ifc.
        return self._convert2xkt_direct(data, "ifc")

    def _ifc_via_ifcconvert(self, data: bytes) -> Optional[bytes]:
        if not tools.have(self.ifcconvert):
            return None
        return self._ifc_via_glb(data, self.ifcconvert)

    def _ifc_via_cxconverter(self, data: bytes) -> Optional[bytes]:
        if not (self.cxconverter and tools.have(self.cxconverter)):
            return None
        return self._ifc_via_glb(data, self.cxconverter)

    def _ifc_via_glb(self, data: bytes, ifc_tool: str) -> Optional[bytes]:
        with tools.workdir() as d:
            src = tools.write_temp(d, "in.ifc", data)
            glb = os.path.join(d, "out.glb")
            if not tools.run([ifc_tool, src, glb], timeout=self.timeout_s):
                return None
            return self._convert2xkt_at(d, glb)

    def _convert2xkt_direct(self, data: bytes, ext: str) -> Optional[bytes]:
        with tools.workdir() as d:
            src = tools.write_temp(d, f"in.{ext}", data)
            return self._convert2xkt_at(d, src)

    def _convert2xkt_at(self, d: str, src: str) -> Optional[bytes]:
        out = os.path.join(d, "out.xkt")
        if not tools.run([self.convert2xkt, "-s", src, "-o", out], timeout=self.timeout_s):
            return None
        return tools.read_if_exists(out)

    # --- OpenCASCADE CAD/mesh backend (STEP/IGES/BREP/OBJ/VRML → glTF) ---- #

    def _occt_to_xkt(self, data: bytes, mime: str) -> Optional[bytes]:
        """Convert a CAD/mesh format convert2xkt can't read into XKT via a glTF
        produced by OpenCASCADE (DRAWEXE), then the standard convert2xkt hop."""
        glb = self._occt_to_glb(data, mime)
        if not glb:
            return None
        with tools.workdir() as d:
            src = tools.write_temp(d, "occt.glb", glb)
            return self._convert2xkt_at(d, src)

    def _occt_to_glb(self, data: bytes, mime: str) -> Optional[bytes]:
        """Read ``data`` with DRAWEXE, tessellate exact geometry where needed, and
        write a binary glTF. ``None`` if DRAWEXE is absent or produces no output."""
        spec = _OCCT_SPECS.get(mime)
        if not spec or not tools.have(self.drawexe):
            return None
        with tools.workdir() as d:
            src = tools.write_temp(d, f"in.{spec.ext}", data)
            glb = os.path.join(d, "out.glb")
            script = tools.write_temp(
                d, "convert.tcl", self._occt_script(spec, src, glb).encode("utf-8")
            )
            # DRAWEXE can exit 0 even after a Tcl-level error, so success is judged
            # by the presence of a non-empty glTF rather than the return code.
            tools.run([self.drawexe, "-b", "-f", script], timeout=self.timeout_s)
            return tools.read_if_exists(glb)

    def _occt_script(self, spec: "_OcctSpec", src: str, glb: str) -> str:
        """A batch DRAW (Tcl) script: read → (tessellate) → (recenter) → WriteGltf.

        Recentering bakes a translation that moves the model's bounding-box centre
        to the world origin (``ttranslate -copy -copymesh`` rewrites the actual
        vertex + triangle coordinates — a plain location is stripped by WriteGltf).
        STEP/IGES parts often sit far from the origin, which leaves the xeokit
        camera framing empty space; this gives a sane default view. Recentering
        re-wraps the single shape in a fresh document, so when it is disabled the
        original read document is exported as-is (preserving assembly structure).
        Paths are in our private workdir and brace-quoted so Tcl does no
        substitution."""
        lines = ["pload MODELING XDE OCAF"]
        if spec.into_doc:
            lines.append(f"{spec.read} _doc {{{src}}}")
            lines.append("XGetOneShape _s _doc")
        else:  # shape-level read (BREP)
            lines.append(f"{spec.read} {{{src}}} _s")
        if spec.mesh:  # exact BRep geometry needs tessellation before export
            lines.append(f"incmesh _s {self.cad_deflection} -relative -a {self.cad_angle}")
        out_doc = "_doc" if spec.into_doc else None
        if self.cad_recenter:
            lines += [
                "set _bb [bounding _s]",
                "set _tx [expr {([lindex $_bb 0]+[lindex $_bb 3])/-2.0}]",
                "set _ty [expr {([lindex $_bb 1]+[lindex $_bb 4])/-2.0}]",
                "set _tz [expr {([lindex $_bb 2]+[lindex $_bb 5])/-2.0}]",
                "ttranslate _s $_tx $_ty $_tz -copy -copymesh",
            ]
            out_doc = None  # the centred shape must be re-wrapped in a fresh doc
        if out_doc is None:
            lines += ["XNewDoc _out", "XAddShape _out _s"]
            out_doc = "_out"
        lines += [f"WriteGltf {out_doc} {{{glb}}}", "exit"]
        return "\n".join(lines) + "\n"
