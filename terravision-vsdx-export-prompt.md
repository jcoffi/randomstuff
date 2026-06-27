# Implementation prompt — native VSDX (Microsoft Visio) export for terravision

Hand this to a coding agent working in a fork of `patrickchugh/terravision`.

---

# ROLE
You are a **senior Python engineer contributing a production feature to the open-source project `patrickchugh/terravision`** (you are working in a fork with full repo access). You write clean, tested, idiomatic Python, you read and reuse existing code before writing new code, and you never break existing behavior. You are meticulous about binary file formats.

# MISSION
Add a **native Microsoft Visio `.vsdx` export** to terravision's `draw` command:

```
terravision draw --source <folder|git_url|combined.json> --format vsdx --outfile org.vsdx
```

must produce **ONE professional Microsoft Visio diagram** with official AWS/GCP/Azure icons that **opens and is editable in real Microsoft Visio**. It must reuse terravision's **existing** resource-type→icon mapping and the **existing** graph layout/clustering (the same ones the normal `draw` uses) so icon coverage is identical and there are **no missing icons**. It must also work in the org-wide flow where many repos/accounts are exported via `graphdata` to JSON, merged into one `tfdata` JSON, and rendered — so it must handle large graphs and nested grouping/clusters.

# GROUND RULES (read this twice)
1. **READ AND REUSE BEFORE YOU WRITE.** The single most important rule. Do **NOT** invent a new type→icon mapping or a new layout algorithm. Find the exact functions terravision already uses to (a) resolve a node's icon and (b) compute positions/clusters, and call those. Re-implementing mapping is the #1 way this task fails (missing/incorrect icons).
2. **Mirror the existing non-raster emitter.** `modules/drawio_emitter.py` already emits an editable (draw.io / mxgraph) diagram from `tfdata`. Your new `modules/vsdx_emitter.py` must mirror its interface and how it sources icons/positions/clusters.
3. **Do not break** existing `png/svg/pdf/jpg/drawio/visualise` outputs.
4. The hand-built "minimal" VSDX trap is real (see Format Spec). **LibreOffice/libvisio opening the file is necessary but NOT sufficient** — real Microsoft Visio is the bar.

# STEP 0 — ORIENTATION (do this first, before any code)
Use **DeepWiki** (`deepwiki.com/patrickchugh/terravision`) and direct file reading to locate the exact entry points. Specifically read and take notes on:
- `modules/drawio_emitter.py` — the interface to mirror (signature, how it consumes `tfdata` + outfile, how it picks an icon per node, how it reads positions/clusters).
- `modules/drawing.py` — the native renderer; find the **icon-resolution path** (type → `resource_classes/{aws,azure,gcp}/*` → `resource_images/{aws,azure,gcp}/*.png`) and the **layout/clustering** computation.
- `modules/graphmaker.py` — how clusters/containers (account, VPC, subnet, etc.) and node positions are built.
- The `click`-based `draw` CLI command — where `--format` choices (`png/svg/pdf/jpg/drawio`) and `--outfile` are defined and how `drawio` is dispatched to its emitter.
- `modules/config/cloud_config_aws.py` and `modules/config/drawio_shape_map_aws.py` — existing mapping config; understand it, reuse it, don't fork it.

**Deliverable of Step 0 (put in PR description):** a short "How terravision resolves icons and layout today, and the exact function(s)/path I will call" note. Do not proceed to coding until you can name those functions.

# DATA MODEL (what you receive)
The central structure is `tfdata` (a dict). Relevant keys:
- `graphdict`: `node_address -> [connected_node_addresses]` (the edges).
- `meta_data`: per-node attributes, including the resolved cloud service/type used for icon selection.
- `node_list`: nodes.
- `annotations`: labels/annotations.
- Clustering/containers and node positions are computed by `graphmaker.py` / `drawing.py` — **consume what they produce**; convert those coordinates to Visio inches.

Icons are bundled PNGs in `resource_images/{aws,azure,gcp}/*.png` (~533 AWS, 808 Azure, 48 GCP). The type→icon resolution already exists — reuse it.

# ───────────────────────────────────────────────
# VSDX FORMAT SPEC + HARD-WON PITFALLS (verbatim — these encode expensive lessons; keep intact)
# ───────────────────────────────────────────────
- `.vsdx` is an OPC/OOXML ZIP package (ISO/IEC 29500-2). Authoritative refs to cite in code comments: MS-VSDX [https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-vsdx/5d9a5a4b-c3d1-4d7b-902f-354f25fe66f4], MS-VSDX Appendix A full XSD, and "Introduction to the Visio file format (.vsdx)".
- CRITICAL: A hand-built MINIMAL package opens in LibreOffice/libvisio but REAL Microsoft Visio REJECTS it ("unexpected end of file"). Real Visio requires the FULL part set. A genuine blank Visio file contains these 16-17 parts (use this as the required skeleton):
  `[Content_Types].xml`, `_rels/.rels`, `docProps/core.xml`, `docProps/app.xml`, `docProps/custom.xml`, `docProps/thumbnail.emf`, `visio/document.xml` (+ `visio/_rels/document.xml.rels`), `visio/windows.xml`, `visio/masters/masters.xml` + `visio/masters/master1.xml` (+ `visio/masters/_rels/masters.xml.rels`), `visio/pages/pages.xml` (+ `visio/pages/_rels/pages.xml.rels`), `visio/pages/page1.xml` (+ `visio/pages/_rels/page1.xml.rels`), plus `visio/media/imageN.png`.
- RECOMMENDED IMPLEMENTATION STRATEGY: bundle a known-good real Visio skeleton (the non-page parts: `document.xml` WITH its StyleSheets, `windows.xml`, `masters/*`, `docProps/*`, `[Content_Types].xml`, all `_rels`) as package data, and PROGRAMMATICALLY generate only `visio/pages/page1.xml` (the shapes), the page `.rels`, the `visio/media/*` parts, and patch `[Content_Types].xml`/page size. (A real skeleton can be obtained from the PyPI `vsdx` package's bundled template, or by saving a blank file from Visio. Generating every part from scratch is allowed only if validated to open in real Visio.)
- Content types needed: `Default Extension="png" ContentType="image/png"`, `Default Extension="emf" ContentType="image/x-emf"`, plus overrides: `/visio/document.xml`=`application/vnd.ms-visio.drawing.main+xml`, pages=`application/vnd.ms-visio.pages+xml`, page=`application/vnd.ms-visio.page+xml`, masters=`application/vnd.ms-visio.masters+xml`, master=`application/vnd.ms-visio.master+xml`, windows=`application/vnd.ms-visio.windows+xml`, and standard OOXML core/app/custom property types.
- Namespaces: main=`http://schemas.microsoft.com/office/visio/2012/main`; rel attr ns `r`=`http://schemas.openxmlformats.org/officeDocument/2006/relationships`; package-rels ns=`http://schemas.openxmlformats.org/package/2006/relationships`. Relationship Types: document=`http://schemas.microsoft.com/visio/2010/relationships/document`; pages/page/master=`http://schemas.microsoft.com/visio/2010/relationships/{pages,page,master}`; image=`http://schemas.openxmlformats.org/officeDocument/2006/relationships/image`.
- EMBEDDED ICON SHAPE (the proven pattern): a `<Shape ... Type='Foreign'>` with cells `PinX,PinY` (shape CENTER), `Width,Height`, `LocPinX,LocPinY` (F='Width*0.5'/'Height*0.5'), `ImgOffsetX,ImgOffsetY`, `ImgWidth,ImgHeight`, then `<ForeignData ForeignType='Bitmap' CompressionType='PNG'><Rel r:id='rIdN'/></ForeignData>`. The PNG is stored as `visio/media/imageN.png` and referenced by a relationship (type image) in `visio/pages/_rels/page1.xml.rels`. DEDUPE identical icons to ONE media part referenced by many shapes (multiple shapes may share the same `r:id`).
- Coordinates: inches, origin BOTTOM-LEFT, Y increases upward. ShapeSheet cells are `<Cell N='..' V='..' [F='formula']/>`. Rectangles/containers use `<Section N='Geometry' IX='0'>` with `<Row T='RelMoveTo'/'RelLineTo'>` rows; labels via `<Text>..</Text>`. Shapes reference style indices `LineStyle/FillStyle/TextStyle` that must exist in `document.xml` StyleSheets (carry over from the skeleton). Set the page size in `pages.xml` `PageSheet` (`PageWidth`/`PageHeight`) large enough for the org.
- The legacy single-file `.vdx` (DatadiagramML 2003) is NOT acceptable — modern Visio is too finicky with it; use modern `.vsdx`.
- Connections: render `graphdict` edges as Visio dynamic connectors between shapes. Clusters/groups (account, VPC, subnet, etc., that terravision already computes) should be Visio container/group shapes with labels.
# ───────────────────────────────────────────────

# FEATURE REQUIREMENTS
1. New `modules/vsdx_emitter.py` that **mirrors `drawio_emitter.py`**'s interface (consume the `tfdata` dict + `outfile`, write the `.vsdx`). Wire it into the `draw` format dispatch in the **same place `drawio` is routed**; add `vsdx` to the `--format` `click.Choice` and to `--help`.
2. **Self-contained `.vsdx`**: icons embedded in `visio/media/*` and **deduped**, using official AWS/GCP/Azure icons resolved via terravision's **existing** icon resolution (no new mapping).
3. **Reuse terravision's computed layout/positions and clustering** so the Visio structure matches the native diagram; convert layout coordinates to Visio inches (origin bottom-left, Y up).
4. Handle the merged/org-wide JSON source (`--source combined.json`) and **large graphs with nested containers** (account → VPC → subnet, etc.) as Visio container/group shapes with labels; render `graphdict` edges as connectors.
5. Must **NOT** break existing `png/svg/pdf/jpg/drawio/visualise` outputs.

# STEP-BY-STEP PLAN (follow in order; think through each before coding)
1. **Orientation (Step 0 above):** name the exact icon-resolution function/path and the layout/cluster source used by `drawing.py`/`drawio_emitter.py`. Write them in the PR notes.
2. **Acquire a real skeleton:** bundle a known-good blank-Visio part set as package data (e.g. from PyPI `vsdx`'s template or a blank file saved by Visio). Store under something like `modules/vsdx_template/` and load it at runtime. Keep `document.xml` StyleSheets, `windows.xml`, `masters/*`, `docProps/*`, `[Content_Types].xml`, and all `_rels` intact.
3. **Define the emitter interface:** create `modules/vsdx_emitter.py` exposing the same shape of entry function as `drawio_emitter.py` (e.g. `def render(tfdata, outfile): ...`). Reuse the same calls `drawio_emitter.py` uses to get, per node: resolved icon PNG path, position, size, label, and cluster membership.
4. **Build the icon media set with dedupe:** collect the set of unique icon PNG paths actually used; map each unique PNG → one `visio/media/imageN.png` and one image relationship `rIdN`. Multiple shapes reuse the same `r:id`.
5. **Generate `visio/pages/page1.xml`:**
   - For each node: emit a `<Shape Type='Foreign'>` per the EMBEDDED ICON SHAPE pattern (PinX/PinY at center, Width/Height, LocPinX/LocPinY formulas, ImgOffset/ImgWidth/ImgHeight, `<ForeignData ... CompressionType='PNG'><Rel r:id='rIdN'/></ForeignData>`), converting terravision coordinates to inches.
   - For each cluster/container: emit a labeled rectangle/group shape (`Geometry` section with `RelMoveTo`/`RelLineTo` rows, `<Text>` label) sized/positioned from terravision's cluster bounds. Preserve nesting.
   - For each `graphdict` edge: emit a dynamic connector between the two shapes.
   - Reference only `LineStyle/FillStyle/TextStyle` indices that exist in the skeleton's `document.xml` StyleSheets.
6. **Generate page `.rels` (`visio/pages/_rels/page1.xml.rels`):** one image relationship per unique media part (correct image relationship Type).
7. **Patch package metadata:** add/ensure `[Content_Types].xml` Defaults for `png` and `emf` and all required Overrides; set `PageWidth`/`PageHeight` in `pages.xml` `PageSheet` large enough to fit the whole org graph (derive from layout bounds).
8. **Zip the OPC package** with the full required part list (the "16-17 parts" above) — nothing missing.
9. **CLI wiring:** add `vsdx` to `--format` choices/help and dispatch to `vsdx_emitter` alongside `drawio`.
10. **Tests + validation** (see Acceptance). Iterate until the file opens in **real Visio** (manually verified at least once) and passes the automated gates.

# ACCEPTANCE CRITERIA (hard, binary)
- **PRIMARY (must hold):** the output `.vsdx` **opens in actual Microsoft Visio with all icons visible and shapes editable.** Explicitly note in the PR: *LibreOffice/libvisio rendering is necessary but NOT sufficient — a file can render in LibreOffice yet fail in Visio when parts are missing.* Manually verify in Visio at least once and state that you did.
- **Automated, in CI/tests:**
  - `libreoffice --headless --convert-to pdf out.vsdx` succeeds and the rendered PDF/output **contains the embedded images** (assert icons are present, not blank boxes).
  - Assert the package **contains the full required part list** (the 16-17 parts above, plus `visio/media/*`).
  - **Round-trip**: unzip the `.vsdx` and assert **XML well-formedness of every part**.
  - Assert **icon dedupe**: number of `visio/media/image*.png` parts == number of unique icons used (not one per node).
  - A **fixture `tfdata`** (small, multi-cloud, with at least one nested cluster and one edge) renders without error and produces the expected shape/part counts.
  - Existing output formats still pass their tests (no regressions).
- **Coverage parity:** for the fixture, every node that gets an icon in the native/`drawio` output also gets one in `.vsdx` (assert no node falls back to a missing/blank icon) — proving reuse of the existing resolution.

# DELIVERABLES
1. `modules/vsdx_emitter.py` (mirrors `drawio_emitter.py`).
2. Bundled Visio skeleton template as package data (and packaging/`MANIFEST`/`setup`/`pyproject` updates so it ships).
3. CLI wiring: `vsdx` in `--format` `click.Choice` + `--help`, routed in the same dispatch as `drawio`.
4. Unit + integration tests: the fixture `tfdata`, the libvisio render-and-contains-images assertion, the required-part-list assertion, XML well-formedness round-trip, and the dedupe + coverage-parity checks.
5. README/usage update documenting `--format vsdx` and the org-wide `combined.json` flow.
6. A clean PR description containing: the Step 0 "how icons/layout are resolved and exactly which functions I reused" note, the manual-Visio-verification statement, and a summary of the part set you generate vs. bundle.

# OUTPUT / WORKING STYLE
- Begin by reporting your Step 0 findings (the exact reused functions/paths) **before** writing the emitter.
- Prefer the bundle-skeleton + generate-pages strategy unless you can prove a from-scratch package opens in real Visio.
- Keep changes surgical and additive; do not refactor unrelated code.
- If any existing function needed for reuse is unclear, state the ambiguity and the file/line you inspected rather than guessing or re-implementing.
