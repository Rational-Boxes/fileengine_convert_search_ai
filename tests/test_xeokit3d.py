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
