"""Unit tests for the xeokit 3D/BIM plugin (XEOKIT3D_PLUGIN design doc).

Covers: 3D/AEC MIME detection, native (dependency-free) IFC text extraction,
per-format searchable-text extraction, geometry→XKT backend selection + render
(external tools stubbed, like the video plugin tests), and registry wiring.

Real sample fixtures live in ``tests/fixtures/3d/`` (fetched from public sources).
"""
from pathlib import Path

import pytest

from convert_search_ai import tools as _tools
from convert_search_ai.mime import detect

FIXTURES = Path(__file__).parent / "fixtures" / "3d"


def fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --------------------------------------------------------------------------- #
# MIME detection
# --------------------------------------------------------------------------- #

def test_mime_ifc_by_content():
    # STEP/Part-21 header + an IFC FILE_SCHEMA -> application/x-ifc.
    assert detect(fx("ifc4.ifc"), "ifc4.ifc") == "application/x-ifc"
    # Even without the name, content sniffing should classify it.
    assert detect(fx("ifc4.ifc")) == "application/x-ifc"


def test_mime_glb_binary_by_magic():
    assert detect(fx("box.glb")) == "model/gltf-binary"


def test_mime_gltf_json_by_content():
    assert detect(fx("box.gltf"), "box.gltf") == "model/gltf+json"


def test_mime_cityjson_by_content():
    # CityJSON is JSON; the "type":"CityJSON" marker disambiguates it.
    assert detect(fx("city_a.json"), "city_a.json") == "application/city+json"


def test_mime_las_by_magic():
    assert detect(fx("points.las")) == "application/vnd.las"


def test_mime_stl_ascii_and_ply():
    assert detect(fx("cube.stl"), "cube.stl") == "model/stl"
    assert detect(fx("cube.ply")) == "model/ply"


def test_mime_extension_fallback_for_binary_3d():
    # A nameless blob with no signature still resolves by extension.
    assert detect(b"\x00\x01\x02not-a-known-magic", "thing.gltf") == "model/gltf+json"
    assert detect(b"\x00\x01\x02not-a-known-magic", "thing.laz") == "application/vnd.laz"


# --------------------------------------------------------------------------- #
# Native (no-dependency) IFC text extraction
# --------------------------------------------------------------------------- #

def test_native_ifc_extractor_pulls_strings_and_schema():
    from convert_search_ai.plugins.xeokit3d import ifc_text_native

    md = ifc_text_native(fx("ifc4.ifc"))
    assert md
    # schema version surfaces (file declares IFC4RC4)
    assert "IFC4" in md
    # human-readable names from the model are present for search
    assert "Default Project" in md
    assert "Default Building" in md
    assert "Description of Default Building" in md
    # entity type names are indexed too (capitalized form)
    assert "IfcBuilding" in md or "IFCBUILDING" in md


def test_native_ifc_extractor_decodes_step_escapes():
    from convert_search_ai.plugins.xeokit3d import ifc_text_native

    # STEP string with a doubled-quote escape ('') -> single quote.
    sample = (
        "ISO-10303-21;\n"
        "HEADER;\nFILE_SCHEMA(('IFC4'));\nENDSEC;\n"
        "DATA;\n#1=IFCPROJECT('guid',$,'O''Brien Tower',$,$,$,$,$,$);\nENDSEC;\n"
        "END-ISO-10303-21;\n"
    ).encode()
    md = ifc_text_native(sample)
    assert "O'Brien Tower" in md


def test_native_ifc_extractor_handles_garbage():
    from convert_search_ai.plugins.xeokit3d import ifc_text_native

    assert ifc_text_native(b"") is None
    assert ifc_text_native(b"not an ifc file at all") is None


# --------------------------------------------------------------------------- #
# Plugin: supports + per-format extraction
# --------------------------------------------------------------------------- #

def _plugin():
    from convert_search_ai.plugins.xeokit3d import Xeokit3DPlugin
    return Xeokit3DPlugin()


def test_plugin_supports_the_3d_mime_family():
    p = _plugin()
    for mime in (
        "application/x-ifc", "model/gltf-binary", "model/gltf+json",
        "application/city+json", "application/vnd.las", "application/vnd.laz",
        "model/stl", "model/ply",
    ):
        assert p.supports(mime), mime
    assert not p.supports("application/pdf")
    assert not p.supports("text/plain")


def test_extract_ifc():
    assert "Default Project" in _plugin().extract(fx("ifc4.ifc"), "application/x-ifc", "ifc4.ifc")


def test_extract_gltf_json_names_and_generator():
    md = _plugin().extract(fx("box.gltf"), "model/gltf+json", "box.gltf")
    assert "COLLADA2GLTF" in md       # asset.generator
    assert "Red" in md                # material name
    assert "Mesh" in md               # mesh name


def test_extract_glb_binary():
    md = _plugin().extract(fx("box.glb"), "model/gltf-binary", "box.glb")
    assert "COLLADA2GLTF" in md


def test_extract_cityjson_attributes():
    md = _plugin().extract(fx("city_a.json"), "application/city+json", "city_a.json")
    assert "Building" in md
    assert "Elvis Presley" in md      # string attribute value
    assert "gable" in md


def test_extract_stl_and_ply_headers():
    stl = _plugin().extract(fx("cube.stl"), "model/stl", "cube.stl")
    assert "PyMesh" in stl
    ply = _plugin().extract(fx("cube.ply"), "model/ply", "cube.ply")
    assert "Blender" in ply           # from the PLY comment line


def test_extract_las_header_strings():
    md = _plugin().extract(fx("points.las"), "application/vnd.las", "points.las")
    assert "TerraScan" in md          # LAS system identifier


def test_extract_is_fail_soft_on_empty():
    p = _plugin()
    assert p.extract(b"", "application/x-ifc", "x.ifc") in (None, "")


# --------------------------------------------------------------------------- #
# Geometry -> XKT: backend selection + render (external tools stubbed)
# --------------------------------------------------------------------------- #

def _stub_tools(monkeypatch, available, xkt=b"XKT\x00bytes"):
    """Pretend the named tools exist; every run() succeeds; outputs are non-empty.
    ``available`` is the set of tool *commands* considered present on PATH."""
    calls = []
    monkeypatch.setattr(_tools, "have", lambda t: t in available)
    monkeypatch.setattr(_tools, "run",
                        lambda cmd, timeout=120, input_bytes=None: (calls.append(cmd), True)[1])
    monkeypatch.setattr(_tools, "read_if_exists", lambda p: xkt if str(p).endswith(".xkt") else b"GLB")
    return calls


def test_render_produces_model_xkt_rendition(monkeypatch):
    calls = _stub_tools(monkeypatch, {"convert2xkt"})
    rends = _plugin().render(fx("box.glb"), "model/gltf-binary", "box.glb")
    assert len(rends) == 1
    r = rends[0]
    assert (r.fmt, r.ext, r.mime) == ("model", "xkt", "application/octet-stream")
    assert r.data == b"XKT\x00bytes"
    # convert2xkt was invoked
    assert any("convert2xkt" in c[0] for c in calls)


def test_render_returns_nothing_without_convert2xkt(monkeypatch):
    _stub_tools(monkeypatch, set())  # no tools at all
    assert _plugin().render(fx("box.glb"), "model/gltf-binary", "box.glb") == []


# --------------------------------------------------------------------------- #
# CAD via OpenCASCADE (STEP/IGES/BREP/OBJ/VRML) → DRAWEXE → glTF → XKT
# --------------------------------------------------------------------------- #

CAD_CASES = [
    ("box.step", "model/step"),
    ("box.iges", "model/iges"),
    ("box.brep", "model/x-brep"),
    ("box.obj", "model/obj"),
    ("box.wrl", "model/vrml"),
]


@pytest.mark.parametrize("fname,mime", CAD_CASES)
def test_mime_cad_by_content(fname, mime):
    # Detected from content alone (OBJ has no magic — it resolves by extension).
    assert detect(fx(fname), fname) == mime
    if mime != "model/obj":
        assert detect(fx(fname)) == mime


def test_step_is_distinguished_from_ifc():
    # An IFC file is also Part-21 but must stay application/x-ifc, not model/step.
    assert detect(fx("ifc4.ifc")) == "application/x-ifc"
    assert detect(fx("box.step")) == "model/step"


def test_plugin_supports_cad_mimes():
    p = _plugin()
    for _, mime in CAD_CASES:
        assert p.supports(mime), mime


def test_extract_step_text_pulls_schema_and_strings():
    from convert_search_ai.plugins.xeokit3d import extract_step_text
    md = extract_step_text(fx("box.step"))
    assert md and "STEP CAD model" in md
    assert "AUTOMOTIVE_DESIGN" in md          # AP214 schema surfaces
    assert "Open CASCADE" in md               # authoring system / header strings
    assert extract_step_text(b"not a step file") is None


def test_extract_iges_obj_vrml_text():
    from convert_search_ai.plugins.xeokit3d import (
        extract_iges_text, extract_obj_text, extract_vrml_text)
    assert "IGES CAD model" in extract_iges_text(fx("box.iges"))
    obj = extract_obj_text(fx("box.obj"))
    assert "OBJ mesh" in obj and "SOLID" in obj   # object name from the OBJ
    assert "VRML model" in extract_vrml_text(fx("box.wrl"))


def test_extract_brep_has_no_text():
    # BREP is pure geometry — the plugin contributes nothing to the text index.
    assert _plugin().extract(fx("box.brep"), "model/x-brep", "box.brep") is None


@pytest.mark.parametrize("fname,mime", CAD_CASES)
def test_render_cad_chains_drawexe_then_convert2xkt(monkeypatch, fname, mime):
    calls = _stub_tools(monkeypatch, {"convert2xkt", "DRAWEXE"})
    rends = _plugin().render(fx(fname), mime, fname)
    assert len(rends) == 1
    assert rends[0].fmt == "model" and rends[0].ext == "xkt"
    # OpenCASCADE produces the glTF, then convert2xkt produces the XKT.
    assert any("DRAWEXE" in c[0] for c in calls)
    assert any("convert2xkt" in c[0] for c in calls)


def test_render_cad_needs_drawexe(monkeypatch):
    # convert2xkt alone can't ingest CAD: without DRAWEXE there is no geometry.
    _stub_tools(monkeypatch, {"convert2xkt"})
    assert _plugin().render(fx("box.step"), "model/step", "box.step") == []


def test_occt_script_recenters_geometry_by_default():
    from convert_search_ai.plugins.xeokit3d import _OCCT_SPECS
    p = _plugin()
    assert p.cad_recenter is True
    script = p._occt_script(_OCCT_SPECS["model/step"], "/w/in.step", "/w/out.glb")
    # Bakes the recentre translation into vertices + mesh (a plain location is
    # stripped by WriteGltf), keyed off the model's bounding box.
    assert "bounding _s" in script
    assert "ttranslate _s" in script and "-copy -copymesh" in script


def test_occt_script_recenter_can_be_disabled(monkeypatch):
    from convert_search_ai.config import Config
    from convert_search_ai.plugins.xeokit3d import Xeokit3DPlugin, _OCCT_SPECS
    monkeypatch.setenv("CSAI_3D_CAD_RECENTER", "false")
    p = Xeokit3DPlugin(Config())
    assert p.cad_recenter is False
    # STEP reads into a document; with recentre off that document is exported
    # as-is (assembly structure preserved), with no translation baked in.
    script = p._occt_script(_OCCT_SPECS["model/step"], "/w/in.step", "/w/out.glb")
    assert "ttranslate" not in script
    assert "WriteGltf _doc" in script


@pytest.mark.live
def test_occt_recenters_far_from_origin_model():
    """Real OpenCASCADE conversion of a part defined far from the world origin:
    the produced glTF must be recentred near the origin so the xeokit camera can
    frame it. Skipped when DRAWEXE is not installed."""
    import json
    import struct
    if not _tools.have(_plugin().drawexe):
        pytest.skip("DRAWEXE (OpenCASCADE) not installed")
    glb = _plugin()._occt_to_glb(fx("box_far.step"), "model/step")
    assert glb and glb[:4] == b"glTF"
    jlen = struct.unpack("<I", glb[12:16])[0]
    doc = json.loads(glb[20:20 + jlen])
    pos = [a for a in doc["accessors"] if a.get("min") and len(a["min"]) == 3]
    gmin = [min(c) for c in zip(*[a["min"] for a in pos])]
    gmax = [max(c) for c in zip(*[a["max"] for a in pos])]
    center = [(a + b) / 2 for a, b in zip(gmin, gmax)]
    # Source part sits ~120 m from the origin; after recentring it must be ~0.
    assert all(abs(c) < 1.0 for c in center), f"not recentred: {center}"


@pytest.mark.live
@pytest.mark.parametrize("fname,mime", CAD_CASES)
def test_occt_produces_real_gltf_geometry(fname, mime):
    """Real OpenCASCADE (DRAWEXE) conversion: CAD source → a valid binary glTF
    carrying actual mesh geometry. Skipped when DRAWEXE is not installed."""
    if not _tools.have(_plugin().drawexe):
        pytest.skip("DRAWEXE (OpenCASCADE) not installed")
    glb = _plugin()._occt_to_glb(fx(fname), mime)
    assert glb and glb[:4] == b"glTF"          # GLB magic
    import json
    import struct
    jlen = struct.unpack("<I", glb[12:16])[0]
    doc = json.loads(glb[20:20 + jlen])
    assert doc.get("meshes"), f"{fname}: glTF has no mesh geometry"


def test_ifc_backend_auto_prefers_ifcopenshell_when_present(monkeypatch):
    from convert_search_ai.plugins.xeokit3d import Xeokit3DPlugin
    calls = _stub_tools(monkeypatch, {"convert2xkt", "ifcConvert"})
    Xeokit3DPlugin().render(fx("ifc4.ifc"), "application/x-ifc", "ifc4.ifc")
    # ifcConvert (IfcOpenShell) used to make GLB before convert2xkt
    assert any("ifcConvert" in c[0] for c in calls)


def test_ifc_backend_auto_falls_back_to_webifc(monkeypatch):
    from convert_search_ai.plugins.xeokit3d import Xeokit3DPlugin
    calls = _stub_tools(monkeypatch, {"convert2xkt"})  # no ifcConvert
    rends = Xeokit3DPlugin().render(fx("ifc4.ifc"), "application/x-ifc", "ifc4.ifc")
    assert len(rends) == 1                       # still converts via web-ifc
    assert not any("ifcConvert" in c[0] for c in calls)
    # convert2xkt consumed the .ifc directly
    assert any(any(str(x).endswith(".ifc") for x in c) for c in calls)


def test_extract_only_skips_geometry(monkeypatch):
    from convert_search_ai.config import Config
    from convert_search_ai.plugins.xeokit3d import Xeokit3DPlugin
    _stub_tools(monkeypatch, {"convert2xkt"})
    monkeypatch.setenv("CSAI_3D_EXTRACT_ONLY", "true")
    plugin = Xeokit3DPlugin(Config())
    assert plugin.render(fx("box.glb"), "model/gltf-binary", "box.glb") == []


def test_render_disabled_by_config(monkeypatch):
    from convert_search_ai.config import Config
    from convert_search_ai.plugins.xeokit3d import Xeokit3DPlugin
    _stub_tools(monkeypatch, {"convert2xkt"})
    monkeypatch.setenv("CSAI_3D_ENABLED", "false")
    plugin = Xeokit3DPlugin(Config())
    assert plugin.render(fx("box.glb"), "model/gltf-binary", "box.glb") == []


# --------------------------------------------------------------------------- #
# Config knobs
# --------------------------------------------------------------------------- #

def test_config_defaults():
    from convert_search_ai.config import Config
    c = Config()
    assert c.threed_enabled is True
    assert c.threed_ifc_backend == "auto"
    assert c.threed_convert2xkt == "convert2xkt"
    assert c.threed_ifcconvert == "ifcConvert"
    assert c.threed_max_input_mb == 512
    assert c.threed_timeout_s == 600
    assert c.threed_extract_only is False
    assert c.threed_drawexe == "DRAWEXE"
    assert c.threed_cad_deflection == "0.001"
    assert c.threed_cad_angle == "20"
    assert c.threed_cad_recenter is True


# --------------------------------------------------------------------------- #
# Registry wiring
# --------------------------------------------------------------------------- #

def test_default_registry_includes_model3d_before_text():
    from convert_search_ai.plugins.registry import default_registry
    order = [p.name for p in default_registry()._plugins]
    assert "model3d" in order
    assert order.index("model3d") < order.index("text")


def test_registry_routes_3d_mime_to_model3d():
    from convert_search_ai.plugins.registry import default_registry
    assert default_registry().for_mime("application/x-ifc").name == "model3d"


# --------------------------------------------------------------------------- #
# IfcOpenShell backend (Python lib for text, IfcConvert for geometry)
#
# The `ifc4.ifc` fixture declares schema IFC4RC4, which the installed
# IfcOpenShell rejects — so it silently falls back to the native scanner and the
# ifcopenshell code path is never exercised by the tests above. `pillar_ifc4.ifc`
# declares a released IFC4 schema, so these tests drive the real ifcopenshell
# path end-to-end (skipped when the library / IfcConvert are absent).
# --------------------------------------------------------------------------- #

def _ifcopenshell_available() -> bool:
    try:
        import ifcopenshell  # noqa: F401
        return True
    except Exception:
        return False


def _ifcconvert_bin():
    import shutil
    # IfcOpenShell ships the CLI as `IfcConvert`; the plugin's default config
    # name is `ifcConvert` — accept either so the test finds a real install.
    return shutil.which("IfcConvert") or shutil.which("ifcConvert")


@pytest.mark.skipif(not _ifcopenshell_available(), reason="ifcopenshell not installed")
def test_ifcopenshell_text_branch_used_for_released_schema():
    from convert_search_ai.plugins.xeokit3d import (
        _ifc_text_ifcopenshell, extract_ifc_text)
    data = fx("pillar_ifc4.ifc")
    md = _ifc_text_ifcopenshell(data)
    assert md, "ifcopenshell branch produced no text for an IFC4 file"
    assert "schema IFC4" in md
    assert "BIMExample" in md          # IfcProject.Name, resolved by ifcopenshell
    # extract_ifc_text must prefer the (richer, attribute-resolved) ifcopenshell
    # result over the native STEP scanner whenever the library can parse the file.
    assert extract_ifc_text(data) == md


@pytest.mark.skipif(
    not (_ifcopenshell_available() and _ifcconvert_bin() and _tools.have("convert2xkt")),
    reason="IfcOpenShell (IfcConvert) + convert2xkt required")
def test_ifc_render_via_ifcopenshell_backend(monkeypatch):
    """Geometry render forced onto the IfcOpenShell backend: IFC → GLB (IfcConvert)
    → XKT (convert2xkt), rather than xeokit's native web-ifc path."""
    import struct
    from convert_search_ai.config import Config
    from convert_search_ai.plugins.xeokit3d import Xeokit3DPlugin
    monkeypatch.setenv("CSAI_3D_IFC_BACKEND", "ifcopenshell")
    monkeypatch.setenv("CSAI_3D_IFCCONVERT", _ifcconvert_bin())
    calls = []
    _orig = _tools.run
    monkeypatch.setattr(_tools, "run",
                        lambda cmd, **kw: (calls.append(cmd[0]), _orig(cmd, **kw))[1])
    rends = Xeokit3DPlugin(Config()).render(
        fx("pillar_ifc4.ifc"), "application/x-ifc", "pillar_ifc4.ifc")
    assert len(rends) == 1
    r = rends[0]
    assert (r.fmt, r.ext, r.mime) == ("model", "xkt", "application/octet-stream")
    assert struct.unpack("<I", r.data[:4])[0] == 12   # xeokit XKT v12 header
    # geometry came through IfcOpenShell's IfcConvert (GLB), then convert2xkt —
    # NOT convert2xkt consuming the .ifc directly (that would be web-ifc/xeokit).
    assert any("IfcConvert" in c or c.endswith("ifcConvert") for c in calls)
    assert any("convert2xkt" in c for c in calls)
