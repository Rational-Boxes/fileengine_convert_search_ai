# convert_search_ai — 3D / BIM conversion + viewing with xeokit

Status: **Design.** Not yet implemented.

Companion docs: [`SPECIFICATION.md`](./SPECIFICATION.md),
[`DEVELOPMENT_PLAN.md`](./DEVELOPMENT_PLAN.md),
[`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md). This plan reuses the conversion
plugin framework (M1) and the search/RAG pipeline (M2/M3) unchanged — it adds one
new `ConversionPlugin`, a small MIME-detection addition, and a frontend viewer
component.

## 1. Goal

Let FileEngine store, view, and **search** 3D CAD/BIM models — especially **IFC**
— in the browser, using the [xeokit](https://xeokit.io/) toolkit.

Two distinct deliverables, both built on the existing rendition mechanism:

1. **Conversion (this service).** A `Xeokit3DPlugin` converts open 3D/AEC formats
   into xeokit's native **XKT** model, stored as a hidden-child rendition of the
   source file — exactly like the `pdf`/`preview`/`thumbnail` renditions today.
2. **Viewing (frontend).** A new viewer component loads that `.xkt` rendition into
   a [xeokit-sdk](https://github.com/xeokit/xeokit-sdk) `Viewer` +
   `XKTLoaderPlugin`, inline in the existing preview surface.

Plus a cross-cutting requirement:

3. **Search.** All **human-readable strings** in the source model (element names,
   descriptions, property sets, classifications, spatial structure, materials,
   layer/group names, glTF/CityJSON attributes, …) are extracted to Markdown and
   fed into the **same** FTS + pgvector indexing path as every other document, so
   models are findable by content and usable as RAG context — gated by the user's
   FileEngine read permission like everything else.

### Out of scope (initial)

- The xeokit **BCF markup / measurement / feedback / annotation** tooling
  (`BCFViewpointsPlugin`, issue tracking, redlining). The first cut is a
  **read-only inline viewer**. A follow-on service can add collaborative markup.
- Server-side photorealistic rendering. Thumbnails are addressed in §6.4.
- Generic mechanical CAD kernels (STEP `.stp`/`.step`, IGES, Parasolid, native
  SolidWorks/CATIA). xeokit-convert does not ingest these; see §11.

## 2. Decisions (resolved)

| # | Decision | Choice |
|---|----------|--------|
| D1 | IFC engine | A **pluggable backend chain** (mirrors `pdf_backends`). The **native xeokit/web-ifc path** — bundled in convert2xkt, needs only Node, **no extra install** — is the **always-available fallback** for geometry *and* metadata. **IfcOpenShell** is an **optional** higher-fidelity backend (it can be complicated to install, so it must never be required). **CxConverter** is an optional proprietary backend. Selection is config-driven and degrades automatically to whatever is installed. |
| D2 | Format scope | **Full xeokit-convert range**: IFC, glTF/GLB, CityJSON, LAS/LAZ, STL, PLY — **extended** via an OpenCASCADE backend to true-CAD/mesh formats convert2xkt can't read: STEP, IGES, BREP, OBJ, VRML (§6.2, §11). |
| D3 | SDK / model format | **xeokit-sdk v2 + XKT** (the stable, production viewer/format today). The next-gen V3 SDK / XGF format is alpha and deferred. |
| D4 | Licensing | **Accept AGPL-3.0** and stay open-source. The viewer and converter are AGPLv3; CSAI is GPL-3.0-or-later and the frontend is GPL-3.0. See §9. |

## 3. Background: xeokit

- **xeokit-sdk** — a pure-WebGL, double-precision 3D viewer SDK for AEC/BIM. The
  stable line (v2) loads models from the **XKT** format via `XKTLoaderPlugin` and
  renders large federated models (real-world coordinates, full precision) in the
  browser. Licensed **AGPL-3.0** (commercial licensing available from Creoox).
- **XKT** — xeokit's native binary format: a compact, web-optimized payload that
  bundles compressed geometry **and** a semantic metadata tree (object ids, IFC
  types, property sets). A ~49 MB IFC compresses to ~1.5 MB XKT loading in a few
  seconds. This is what we generate and store as a rendition.
- **convert2xkt** (`@xeokit/xeokit-convert`) — a **Node.js ≥18 CLI** that converts
  IFC, glTF/GLB, CityJSON, LAS/LAZ, STL, and PLY into XKT. Licensed AGPL-3.0. Its
  bundled IFC path uses **`web-ifc`** (a WASM IFC parser) and needs **only Node —
  no native libraries to install**. This is our **baseline / fallback** engine: it
  produces both XKT geometry and an embedded metadata tree directly from `.ifc`.
  Its IFC support is officially **alpha**, so fidelity on complex models may trail
  the options below.
- **IfcOpenShell** — mature open-source IFC toolkit, but **optional** here because
  it can be **awkward to install** (native build / platform wheels). When present,
  it is the **preferred** backend: `ifcConvert` produces higher-fidelity GLB
  geometry, and the Python `ifcopenshell` package gives full programmatic access to
  the model (entities, attributes, property sets, spatial tree, materials,
  classifications) for a **richer** XKT metadata JSON **and** richer searchable
  text (§5, §6.2). The system must run fully **without** it.
- **CxConverter** (`ifc2gltfcxconverter`) — Creoox's **proprietary** IFC→GLB tool,
  xeokit's recommended production path for best fidelity/perf. Optional; operator-
  supplied (§9). Output GLB then feeds convert2xkt.
- **Next-gen xeokit/sdk (V3)** — a TypeScript redesign (scene-graph + data-graph
  split, XGF format). **Alpha**; not targeted here (D3).

## 4. Where this fits in CSAI

The conversion framework already does exactly what we need (see
[`plugins/base.py`](../src/convert_search_ai/plugins/base.py),
[`plugins/registry.py`](../src/convert_search_ai/plugins/registry.py),
[`renditions.py`](../src/convert_search_ai/renditions.py),
[`ingest.py`](../src/convert_search_ai/ingest.py)):

```
core file.changed event
   → ingest worker fetches bytes
   → mime.detect(bytes, name)                      ← §6.1 adds 3D MIME sniffing
   → PluginRegistry.for_mime(mime)  → Xeokit3DPlugin
   → plugin.render(...)  → [Rendition("model","xkt",…)]   (v1: no server raster)   ← §6
   → plugin.extract(...) → Markdown of human-readable strings                      ← §5
   → RenditionWriter.write(...)  hidden child "<version>-model.xkt"                 ← §7
   → markdown → chunking → embeddings → pgvector + Postgres FTS                   ← reused
```

A `ConversionPlugin` already returns **both** renditions (presentation copies) and
`markdown` (indexed text). The 3D plugin is "just another plugin": no changes to
the ingest worker, reconcile sweep, indexing, search, permission gating, or RAG.

What is genuinely new:

- A `Xeokit3DPlugin` with pluggable geometry backends (§6).
- A small MIME-detection addition for 3D/AEC formats (§6.1).
- A new rendition **fmt** vocabulary entry `model` (§7), and the frontend changes
  to recognize and render it (§8).
- New container tooling: Node + convert2xkt (required); IfcOpenShell (optional,
  §9).

## 5. Searchable text extraction (requirement #3)

Goal: index **every human-readable string** in the model so it participates in
FTS + fuzzy + vector search like any document. `extract()` returns Markdown; the
existing chunker/indexer does the rest. The extraction is **independent of the
geometry conversion** — even if geometry conversion fails or is disabled, text
extraction should still run (fail-soft, per the plugin contract).

### 5.1 IFC — two extractors (native fallback + optional IfcOpenShell)

Because IfcOpenShell is optional (D1), IFC text extraction has **two
implementations** selected the same way as the geometry backend; text indexing
**always works**, even on a stripped-down deployment:

**(a) Native STEP/Part-21 extractor — no dependencies (the fallback).** An IFC
file is an ASCII STEP physical file (`ISO-10303-21;`). A small built-in
tokenizer/parser (stdlib only) walks the `DATA` section and pulls:

- The `HEADER` (`FILE_DESCRIPTION`, `FILE_NAME`, `FILE_SCHEMA` → schema version,
  authoring application).
- Every entity's **type name** (e.g. `IFCWALL`, `IFCDOOR`) and its **quoted string
  attributes** (the single-quoted STEP strings — names, descriptions, tags,
  `IFCPROPERTYSINGLEVALUE` name/value, `IFCMATERIAL` names, classification
  references). STEP string escaping (`''`, `\X2\…\X0\` unicode) is decoded.

This yields all the human-readable strings for FTS/vector search **without** a full
semantic graph. It cannot cheaply reconstruct the spatial tree or resolve every
relationship, so the output is grouped by entity type rather than by storey — but
every searchable string is captured. This path can also feed a **minimal** XKT
metadata JSON (ids, types, names) when the native geometry path is used.

**(b) IfcOpenShell extractor — optional, richer.** When `ifcopenshell` is
importable, a single `ifcopenshell.open(path)` pass yields a fully structured,
relationship-resolved Markdown document:

- **Project header** — schema, authoring application, `IfcProject` name, units.
- **Spatial structure** — the `IfcSite → IfcBuilding → IfcBuildingStorey →
  IfcSpace` tree as nested headings (storey/space names and long names).
- **Elements** — per `IfcElement`: `Name`, `Description`, `ObjectType`, `Tag`,
  predefined type, and containing storey; grouped by IFC class.
- **Property sets & quantities**, **materials**, **classifications** (Uniclass/
  OmniClass codes + names), **type objects** — fully resolved.

When IfcOpenShell is present this same parse **also** produces the richer
convert2xkt metadata JSON (§6.2) — one parse, two consumers. Example (richer)
output:

```markdown
# Model: Office Tower (IFC4)
Authoring tool: Revit 2024 · Units: millimetre

## Site: Main Campus
### Building: Tower A
#### Storey: Level 03 (+9000mm)
- **IfcDoor** “D-0312 Fire Door” (Tag 90213) — Pset_DoorCommon.FireRating=EI30,
  IsExternal=false; Classification: Uniclass Pr_30_59_24 “Doors”
- **IfcWall** “Curtain Wall W-12” — Pset_WallCommon.LoadBearing=false; Material: Glass/Aluminium
```

Selection follows the same `auto` rule as geometry (§6.2/§10): prefer IfcOpenShell
if importable, else the native extractor. Both are fail-soft.

### 5.2 Other formats

- **glTF/GLB** — node names, mesh/material names, `KHR_materials_*` names, and
  `extras`/`asset.extras` string fields; scene/camera names.
- **CityJSON** — `CityObject` ids, `type`, and all string-valued `attributes`
  (addresses, names, function codes).
- **LAS/LAZ** — header system/software id, point-format/CRS description; little
  free text (point clouds are mostly numeric). Index the header metadata only.
- **STL** — the 80-byte ASCII header comment / solid name (ASCII STL).
- **PLY** — `comment` / `obj_info` header lines and named element/property labels.
- **STEP** — generic Part-21, so the same dependency-free STEP scanner used for IFC
  (§5.1a) applies directly: header (author/organization/originating system) plus
  every entity's quoted strings (product names, descriptions, units), grouped by
  type.
- **IGES** — the Start section free text and the Hollerith strings of the Global
  section (authoring system, file name, units).
- **OBJ** — object/group names (`o`/`g`), material references (`usemtl`/`mtllib`),
  and `#` comments.
- **VRML** — node `DEF` names and quoted strings (WorldInfo title/info, URLs).
- **BREP** — pure geometry; no human-readable text (contributes nothing to FTS).

When a format carries no meaningful text, `extract()` returns `None` (the file is
still viewable; it just contributes nothing to the text index). Guard output size
with the existing extraction caps (truncate very large property dumps).

## 6. Conversion design — `Xeokit3DPlugin`

New file `src/convert_search_ai/plugins/xeokit3d.py`, registered in
`default_registry()` ahead of the text catch-all. Implements the standard
contract: `supports`, `render`, `extract`. Side-effect-free; degrades to
`[]`/`None` when its external tools are missing. **Node + convert2xkt** is the only
tool needed for geometry (IFC via bundled web-ifc); IfcOpenShell/CxConverter, when
present, are *preferred* but never required. A deployment without Node simply
produces no model rendition (text extraction still runs, §5) rather than failing
the file.

### 6.1 MIME detection (`mime.py` additions)

3D/AEC formats need sniffing help. Add to the magic table / `detect()`:

| Format | Detection | MIME used internally |
|--------|-----------|----------------------|
| IFC (STEP/Part 21) | text begins `ISO-10303-21;` and contains `FILE_SCHEMA(('IFC…'))` | `application/x-ifc` |
| IFC-XML | XML root with IFC namespace | `application/x-ifc+xml` |
| IFC-ZIP | ZIP containing a single `.ifc` member | `application/x-ifc-zip` |
| glTF (JSON) | JSON with top-level `"asset":{"version"`…} or `.gltf` | `model/gltf+json` |
| GLB (binary) | magic `glTF` (`0x46546C67`) at offset 0 | `model/gltf-binary` |
| CityJSON | JSON with `"type":"CityJSON"` | `application/city+json` |
| LAS | magic `LASF` at offset 0 | `application/vnd.las` |
| LAZ | LAZ-compressed LAS (`LASF` + compression vlr) / `.laz` | `application/vnd.laz` |
| STL | ASCII `solid ` prefix, or 84-byte binary header heuristic + `.stl` | `model/stl` |
| PLY | magic `ply\n` | `model/ply` |
| STEP | Part-21 (`ISO-10303-21;`) whose `FILE_SCHEMA` is **not** IFC, or `.step`/`.stp` | `model/step` |
| IGES | section letter `S` in column 73 + 7-digit sequence, or `.iges`/`.igs` | `model/iges` |
| BREP | text begins `DBRep_DrawableShape` / `CASCADE Topology`, or `.brep` | `model/x-brep` |
| OBJ | `.obj` (Wavefront has no reliable magic) | `model/obj` |
| VRML | magic `#VRML`, or `.wrl`/`.vrml` | `model/vrml` |

Extension fallback (`.ifc`, `.ifczip`, `.glb`, `.gltf`, `.json`/`.city.json`,
`.las`, `.laz`, `.stl`, `.ply`) covers the rest. Distinguishing plain glTF JSON
and CityJSON from arbitrary JSON requires a content peek (keys above), so do it in
`_sniff` before the generic JSON/zip handling.

### 6.2 Geometry → XKT (pluggable backends)

Mirror `pdf_backends`: an ordered, config-driven chain of geometry backends. Each
backend knows which MIMEs it can convert and returns XKT bytes (or `None`).

```
class XktBackend(Protocol):
    name: str
    def supports(self, mime: str) -> bool: ...
    def to_xkt(self, data: bytes, mime: str, name: str) -> Optional[bytes]: ...
```

**IFC backend chain (config-driven, `auto` by default — first that works wins):**

1. **CxConverter** (if `CSAI_3D_CXCONVERTER` set): `ifc2gltfcxconverter in.ifc
   out/` → GLB(+metadata JSON) → `convert2xkt`. Best fidelity; proprietary.
2. **IfcOpenShell** (if importable / `ifcConvert` present): `ifcConvert in.ifc
   out.glb` → GLB, plus the §5.1(b) parse → rich **metadata JSON**, then
   `convert2xkt -s out.glb -m meta.json -o out.xkt`. Higher fidelity than web-ifc.
3. **Native web-ifc fallback (always available, Node only):** `convert2xkt -s
   in.ifc -o out.xkt` — convert2xkt ingests `.ifc` directly via its bundled
   web-ifc and embeds a metadata tree. No native libraries; works everywhere Node
   does. This guarantees IFC viewing even when IfcOpenShell can't be installed.

`auto` walks the list top-down and uses the first backend whose tools are present;
an explicit `CSAI_3D_IFC_BACKEND` value pins one. Same plugin, swapped backend —
no other code changes.

**Other formats → XKT:** `convert2xkt -s <in> -o out.xkt` directly (glTF/GLB,
CityJSON, LAS/LAZ, STL, PLY are first-class convert2xkt inputs). Metadata from
§5.2 is passed via `-m` where the format carries it (glTF/CityJSON). These need
**only** convert2xkt — never IfcOpenShell.

**CAD formats → glTF → XKT (OpenCASCADE backend):** STEP (`.step`/`.stp`), IGES
(`.iges`/`.igs`), OpenCASCADE BREP (`.brep`), Wavefront OBJ and VRML are **not**
convert2xkt inputs, so they are routed through **OpenCASCADE's `DRAWEXE`** (the
OCCT "DRAW" Tcl CLI) first. A generated batch script reads the file into an XDE
document, **tessellates** exact BRep geometry (`incmesh`, relative deflection so
one setting fits any model scale — STEP/IGES/BREP only; OBJ/VRML already carry
meshes), and writes a binary glTF (`WriteGltf`); that glTF then feeds the same
`convert2xkt` hop above. `DRAWEXE` runs headless via the existing `tools` helpers
(`workdir`/`write_temp`/`run`/`read_if_exists`) with all paths confined to a
private temp dir. Required system package: `opencascade-draw` (+ `-modeling`,
`-ocaf`, `-visualization`). Optional/auto-detected exactly like the IFC backends —
when `DRAWEXE` is missing the file is text-indexed only.

convert2xkt is invoked as a subprocess via the existing
[`tools`](../src/convert_search_ai/tools.py) helpers (`tools.workdir()`,
`tools.write_temp`, `tools.run(timeout=…)`, `tools.read_if_exists`) — the same
pattern `office.py` uses for LibreOffice. All work happens in a temp dir; nothing
touches the source bytes.

### 6.3 `render()` output

```python
def render(self, data, mime, name) -> List[Rendition]:
    xkt = self._backend_for(mime).to_xkt(data, mime, name)
    out = []
    if xkt:
        out.append(Rendition("model", "xkt", xkt, "application/octet-stream"))
        out += self._thumbnails(...)   # §6.4, optional
    return out
```

The XKT rendition is served by the bridge as opaque bytes; the frontend loads it
into `XKTLoaderPlugin` from an `ArrayBuffer` (§8). MIME is
`application/octet-stream` (a custom `application/vnd.xeokit.xkt` is optional and
purely cosmetic).

### 6.4 Thumbnails / still previews

XKT renders in a WebGL browser context; there is no cheap server-side raster.
Static previews **are** possible (drive xeokit in **headless Chrome** and snapshot
a canvas frame), but spinning up a headless browser + WebGL per model is **heavy**
— far more than the poppler/LibreOffice subprocesses the other plugins use.

- **Initial version:** **no static raster** (the headless-Chrome overhead is not
  worth it for v1). Instead the **frontend** owns this entirely: it shows
  **format-specific icons** (IFC, glTF, CityJSON, point cloud, mesh, …) and a
  "preview-link" graphic for 3D files, and "open" launches the live viewer. No
  headless GPU/browser in the ingest worker, and no server-side icon assets — see
  §8.
- **Future:** rather than burden the CSAI ingest worker with headless Chrome,
  isolate the compute-heavy raster step in a **bespoke 3D preview service** (§13)
  — a dedicated, separately-scaled service that renders XKT → `thumbnail`/`preview`
  PNGs out-of-band and writes them back as the usual rendition children.

The plugin is structured so emitting `thumbnail`/`preview` images later (whether
in-process or via that service) requires **no contract change** — they are just
additional `Rendition`s under the existing fmt vocabulary.

## 7. Rendition storage & naming

Reuse [`renditions.py`](../src/convert_search_ai/renditions.py) unchanged. The new
logical fmt is **`model`** with ext **`xkt`**, so the hidden child is:

```
<source-version>-model.xkt
```

This is idempotent (written at most once per source version) and superseded by new
versions, exactly like existing renditions. No new RPCs; it's a normal hidden
child of the source file.

> The frontend's rendition vocabulary is a **fixed allowlist** (`KNOWN` in
> `frontend/src/services/renditions.ts`). Adding `model` there (§8) is required or
> the child is ignored by the UI.

## 8. Frontend viewer integration

Repo: `frontend/` (Vue 3 + TS). Plugs into the existing preview surface
(`DocumentPreview.vue` / `PreviewView.vue` / `renditions.ts`).

1. **Rendition vocabulary** — add `'model'` to `RenditionFmt` / `KNOWN` in
   `src/services/renditions.ts`, with ext `xkt`. It is **not** an image, so the
   `thumbnailImage`/`previewImage` helpers must ignore it for still tiles.
2. **Icons & preview-link graphic (frontend-owned, v1).** Since the backend emits
   no raster for 3D files (§6.4), the frontend supplies the visuals: a
   **format-specific icon** per source type (IFC / glTF / CityJSON / point cloud /
   mesh — keyed off the source MIME or extension) for file tiles, and a
   **"preview-link" graphic** (a clickable 3D-preview affordance) that opens the
   live viewer. These are static frontend assets; no server-side icon generation.
   When a `model` rendition is absent (e.g. conversion disabled/failed), still
   show the format icon but no preview affordance.
3. **Viewer component** — new `Model3DViewer.vue` (and/or a `ModelViewerOverlay`
   like `PdfPreviewOverlay.vue`):
   - Lazy-load `@xeokit/xeokit-sdk` (dynamic `import()`) so the (large, AGPL)
     viewer is only pulled when a 3D file is opened — keeps the main bundle lean.
   - Create `Viewer({ canvasId })`, add `XKTLoaderPlugin`, and
     `load({ id, xkt: arrayBuffer })`.
   - Fetch the `.xkt` bytes via the existing authed download
     (`renditionObjectUrl`/`downloadFile` → `ArrayBuffer`); the bytes are
     permission-gated by the bridge like any rendition.
   - Add a `TreeViewPlugin`/`NavCubePlugin` (optional) for object tree + IFC type
     navigation — the metadata is already embedded in the XKT.
4. **Routing/UX** — open a 3D file → 3D viewer overlay (same affordance as PDF).
   Dispose the `Viewer` and revoke object URLs on close (WebGL context cleanup).
5. **Dependency** — add `@xeokit/xeokit-sdk` to `frontend/package.json`. AGPL — see
   §9.

## 9. Runtime dependencies & licensing (AGPL-3.0)

### Runtime dependencies

| Dependency | Where | Required? |
|------------|-------|-----------|
| **Node.js ≥18 + `@xeokit/xeokit-convert` (convert2xkt)** | CSAI image | **Required** for any geometry→XKT (incl. native IFC via web-ifc). |
| **IfcOpenShell** (`ifcopenshell` + `ifcConvert`) | CSAI image (optional layer) | Optional — auto-detected; enriches IFC fidelity + metadata + text. |
| **OpenCASCADE** (`DRAWEXE`, pkg `opencascade-draw`) | CSAI image (optional layer) | Optional — auto-detected; enables STEP/IGES/BREP/OBJ/VRML → glTF → XKT. LGPL-2.1 (with OCCT exception). |
| **CxConverter** (`ifc2gltfcxconverter`) | operator-supplied | Optional — proprietary; not bundled/distributed. |
| **`@xeokit/xeokit-sdk`** | frontend bundle | Required for the viewer (lazy-loaded). |

The CSAI image gains a Node build/runtime stage for convert2xkt. IfcOpenShell is a
**separate, optional** image layer so a minimal build (Node + convert2xkt only)
still ships working 3D conversion + search via the native web-ifc fallback (§6.2).

### Licensing

Decision D4: **accept AGPL, stay open-source.** Implications to honor:

- **convert2xkt** runs **server-side** in CSAI and is GPL-compatible with the
  GPL-3.0-or-later service. No proprietary linkage. (The optional **CxConverter**
  backend is proprietary; it stays an **optional, separately-obtained** binary that
  is *not* bundled or distributed with this repo — operator supplies it.)
- **xeokit-sdk** ships to the browser. Under **AGPLv3 §13**, users interacting with
  the app over a network must be offered the **corresponding source** of the
  AGPL-covered viewer (including any local modifications). Action items:
  - Keep the bundled xeokit unmodified, or publish any fork.
  - Provide a visible **"Source"/license** link in the frontend (and in
    `docker_unified`) pointing to the xeokit source + our repos.
  - Record xeokit's AGPL in the frontend's license/attribution notices.
- Isolate xeokit behind the `Model3DViewer` component and the `XktBackend`
  boundary so a future swap to a commercially-licensed xeokit (if ever needed)
  touches one component + one backend.

## 10. Configuration

New `CSAI_*` knobs in [`config.py`](../src/convert_search_ai/config.py) (env-driven,
same pattern as existing options):

| Env var | Default | Meaning |
|---------|---------|---------|
| `CSAI_3D_ENABLED` | `true` | Master switch for the 3D plugin. |
| `CSAI_3D_IFC_BACKEND` | `auto` | `auto` (auto-detect: CxConverter → IfcOpenShell → native web-ifc, first present wins) \| `cxconverter` \| `ifcopenshell` \| `webifc` \| comma-list to pin an explicit order. |
| `CSAI_3D_CONVERT2XKT` | `convert2xkt` | Path/command for convert2xkt (Node CLI; the only required geometry tool). |
| `CSAI_3D_IFCCONVERT` | `ifcConvert` | Path for IfcOpenShell's `ifcConvert` (used only if auto-detected/installed). |
| `CSAI_3D_CXCONVERTER` | _(unset)_ | Path for the proprietary `ifc2gltfcxconverter` (its presence enables that backend). |
| `CSAI_3D_MAX_INPUT_MB` | `512` | Reject/skip source files larger than this (resource guard). |
| `CSAI_3D_TIMEOUT_S` | `600` | Per-conversion subprocess timeout. |
| `CSAI_3D_EXTRACT_ONLY` | `false` | Index text but skip geometry→XKT (e.g. nodes without Node). |
| `CSAI_3D_DRAWEXE` | `DRAWEXE` | Path/command for the OpenCASCADE DRAW CLI (the CAD→glTF backend for STEP/IGES/BREP/OBJ/VRML; auto-detected, optional). |
| `CSAI_3D_CAD_DEFLECTION` | `0.001` | Relative linear tessellation deflection (fraction of bounding box) for exact CAD geometry. |
| `CSAI_3D_CAD_ANGLE` | `20` | Angular tessellation deflection in degrees for exact CAD geometry. |

**Auto-detection.** With the `auto` default the plugin **probes for IfcOpenShell**
(can `import ifcopenshell` / is `ifcConvert` on PATH?) and CxConverter
(`CSAI_3D_CXCONVERTER` set & executable?) **at startup**, picks the best available
IFC backend, and **gracefully falls back** to the bundled native web-ifc path when
neither is present. The result is logged once at startup (e.g. *"3D: IFC backend =
ifcopenshell"* or *"… = webifc (IfcOpenShell not found)"*). The same auto rule
governs IFC **text** extraction (§5.1). When even convert2xkt/Node is missing, the
plugin logs once and degrades to text-only, consistent with the fail-soft
contract.

## 11. Resource & safety considerations

- **Big models.** IFC→GLB→XKT is CPU/RAM heavy. Enforce `CSAI_3D_MAX_INPUT_MB` and
  `CSAI_3D_TIMEOUT_S`; run in the ingest worker (already off the request path);
  consider a concurrency cap so one giant model can't starve the worker pool.
- **Untrusted input.** IfcOpenShell/convert2xkt parse untrusted bytes — run with
  the worker's existing sandboxing, no shell-string interpolation (`tools.run`
  takes an argv list), all temp files in a `workdir()` that is cleaned up.
- **Fail-soft.** Geometry failure must not drop text extraction, and vice versa;
  the registry already isolates `render` and `extract` exceptions per file.
- **Idempotency / reconcile.** Inherited from the rendition writer — a re-run finds
  `<version>-model.xkt` present and writes nothing.
- **True-CAD via OpenCASCADE.** STEP/IGES/BREP (plus OBJ/VRML, which convert2xkt
  also can't ingest) are converted through the **OpenCASCADE `DRAWEXE`** backend
  (§6.2): read → tessellate exact BRep geometry → glTF, then the standard
  convert2xkt → XKT hop. When `DRAWEXE` is absent these formats are text-indexed
  only, like every other geometry path. Genuinely proprietary native CAD
  (SolidWorks/CATIA/Parasolid) is still out of scope.

## 12. Implementation phases

- **P1 — text indexing (highest value, lowest risk).** MIME detection (§6.1) +
  `Xeokit3DPlugin.extract()`: the **native STEP extractor** for IFC (§5.1a, zero
  deps) plus the other formats (§5.2), and the optional IfcOpenShell enrichment
  (§5.1b) behind the auto-detect probe. Models become **searchable** and usable as
  RAG context immediately, with no Node/WebGL dependency. Unit tests with small
  fixture models.
- **P2 — XKT conversion.** Add **Node + convert2xkt** to the image (required, §9);
  implement the auto-detecting `XktBackend` chain and `render()` (§6.2–6.3); store
  the `model` rendition (§7). IfcOpenShell is added as an **optional** image layer,
  not a hard dependency — the native web-ifc path must pass the tests on its own.
  `@live` test: ingest an IFC → assert `<v>-model.xkt` child exists and is a valid
  XKT (run with **and** without IfcOpenShell present).
- **P3 — frontend viewer.** `Model3DViewer.vue`, rendition-vocabulary + the
  format-specific icons / preview-link graphic (§8), lazy xeokit load, AGPL
  source/attribution notices (§8, §9). Wire into `docker_unified` (Node build stage
  for convert2xkt; IfcOpenShell as an optional CSAI image layer).
- **P4 — polish (deferred).** CxConverter backend validation, object-tree/nav UI,
  point-cloud tuning. Static raster thumbnails are **not** in scope here — they are
  deferred to a possible bespoke 3D preview service (§6.4, §13) so the
  headless-Chrome compute stays out of the CSAI ingest worker.

## 13. Open questions / future

- **Thumbnails / static previews:** v1 ships frontend-owned, format-specific icons
  + a preview-link graphic (no server raster — the headless-Chrome cost is too
  high, §6.4, §8). A later **bespoke 3D preview service**
  could isolate this compute-heavy rendering: a dedicated, independently-scaled
  worker that drives headless xeokit to produce `thumbnail`/`preview` PNGs
  out-of-band and writes them back as rendition children — keeping the heavy
  GPU/browser dependency out of the main CSAI ingest worker (and reusable for any
  future server-side 3D rendering, e.g. fixed-view snapshots for RAG citations).
- **Federated/multi-file BIM:** IFC projects are sometimes split across files.
  v1 treats each file independently; federation (loading several XKTs into one
  scene) is a future viewer feature.
- **CxConverter procurement:** if higher IFC fidelity is needed, obtain the Creoox
  tool and enable the backend (§6.2, §10) — no code change beyond the path.
- **Markup/BCF service:** the originally-noted collaborative markup/feedback tools
  remain a separate, later service (§1 out-of-scope).
- **V3 SDK / XGF:** revisit when xeokit V3 leaves alpha (D3).

## 14. References

- xeokit — <https://xeokit.io/>
- xeokit-sdk (v2, AGPL) — <https://github.com/xeokit/xeokit-sdk>
- xeokit-convert / convert2xkt — <https://github.com/xeokit/xeokit-convert>
- Converting IFC to XKT (CxConverter) — <https://xeokit.io/blog/converting-ifc-to-xkt-using-ifc2gltfcxconverter/>
- Converting models with convert2xkt — <https://xeokit.io/blog/converting-models-to-xkt-with-convert2xkt/>
- xeokit AGPL terms — <https://xeokit.io/docs/terms/affero-gpl-agpl/>
- Next-gen xeokit/sdk (V3, alpha) — <https://github.com/xeokit/sdk>
- IfcOpenShell — <https://ifcopenshell.org/>
- CSAI plugin contract — [`plugins/base.py`](../src/convert_search_ai/plugins/base.py)
- CSAI rendition writer — [`renditions.py`](../src/convert_search_ai/renditions.py)
- FileEngine renditions — `file_engine_core/design_documents/file_renditions.md`
</content>
