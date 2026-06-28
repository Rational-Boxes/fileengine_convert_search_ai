# 3D / BIM test fixtures — provenance

Small public sample files used by `tests/test_xeokit3d.py` to exercise MIME
detection and searchable-text extraction. Each is unmodified from its source.

| File | Format | Source | License |
|------|--------|--------|---------|
| `box.glb`, `box.gltf` | glTF 2.0 (binary + embedded) | KhronosGroup/glTF-Sample-Models `2.0/Box` | Public domain / CC0 |
| `boxes.gltf` | glTF 2.0 (embedded) | KhronosGroup/glTF-Sample-Models `2.0/BoxInterleaved` | Public domain / CC0 |
| `ifc4.ifc` | IFC4 (STEP/Part-21) | opensourceBIM/TestFiles `TestData/data/ifc4.ifc` | per upstream repo |
| `city_a.json` | CityJSON 2.0 | cityjson/cjio `tests/data/dummy/dummy.json` | per upstream repo |
| `cube.ply` | PLY (ASCII) | PyMesh/PyMesh `tests/data/cube.ply` | BSD (PyMesh) |
| `cube.stl` | STL (ASCII) | PyMesh/PyMesh `tests/data/cube.stl` | BSD (PyMesh) |
| `points.las` | LAS 1.x point cloud | PDAL/PDAL `test/data/las/simple.las` | per upstream repo |

These are test inputs only; they are not distributed as part of the service.
