# Voxel Sculpt Add-on Feasibility Research

Research date: 2026-04-20. Target Blender versions considered: 4.2 LTS, 4.3, 4.4, and 5.0/5.1 where relevant. Where information could not be verified from primary sources, this is explicitly flagged.

---

## 1. `pyopenvdb` / `openvdb` in Blender's bundled Python

**Status: partially available since Blender 3.6; module name changed in 4.4; must be explicitly exposed.**

- Blender began shipping an OpenVDB Python module in the official build as of **Blender 3.6**, following long-standing requests (see ["Build pyopenvdb as part of make deps"](https://devtalk.blender.org/t/build-pyopenvdb-as-part-of-make-deps/14148) and [D8123](https://developer.blender.org/D8123)). The module was called `pyopenvdb`.
- As of **Blender 4.4**, the module is still bundled but the import name was changed from `pyopenvdb` to `openvdb`, and it is no longer on `sys.path` by default. An add-on must call [`bpy.utils.expose_bundled_modules()`](https://developer.blender.org/docs/release_notes/4.4/python_api/) to add Blender's vendored VFX libraries to `sys.path` before importing (Blender 4.4 release notes, Python API section).
  ```python
  import bpy
  bpy.utils.expose_bundled_modules()
  import openvdb
  ```
- **Critical caveat:** the bundled `openvdb` Python module is the stock upstream module. It lets an add-on **load `.vdb` files from disk and manipulate grids in its own memory**, but it does **not** share C++ grid pointers with Blender's internal `Volume` datablocks. That is the same limitation called out in the original 2020 forum thread: "pyopenvdb in Blender's Python Console without the ability to modify Blender's OpenVDB grids, only to have access to out-of-box pyopenvdb." No evidence was found that this has changed.
- On Linux distro builds (e.g. NixOS, issue [NixOS/nixpkgs#447287](https://github.com/NixOS/nixpkgs/issues/447287)) the module is sometimes missing because it was not built — add-ons must handle `ImportError` gracefully.

Roadmap: no public tracker item was located proposing a Python binding that would give Python direct, writable access to the grids owned by `bpy.data.volumes[...]`. This should be treated as **not currently on the roadmap**.

---

## 2. Python API surface for `bpy.types.Volume` / `VolumeGrid`

**Status: read-only metadata; no voxel-level read/write from Python.**

From the current Blender Python API docs ([`bpy.types.Volume`](https://docs.blender.org/api/current/bpy.types.Volume.html), [`bpy.types.VolumeGrid`](https://docs.blender.org/api/current/bpy.types.VolumeGrid.html), [`bpy.types.VolumeGrids`](https://docs.blender.org/api/current/bpy.types.VolumeGrids.html)):

- `Volume` datablock exposes a `filepath` (a `.vdb` file path), sequence settings, a `grids` collection, and display properties. It is backed by a file — setting `filepath` causes the file to be (lazily) loaded on evaluation.
- `VolumeGrid` exposes only **`name`, `data_type` (enum, read-only), `channels` (int, read-only), `is_loaded` (bool, read-only), `matrix_object`**, plus `load()` and `unload()` methods. There is **no voxel buffer accessor, no active-voxel iterator, no setter for values**.
- The C API documented on the Volume wiki page ([wiki.blender.org/wiki/Source/Objects/Volume](https://wiki.blender.org/wiki/Source/Objects/Volume)) uses `BKE_volume_grid_openvdb_for_read` / `BKE_volume_grid_openvdb_for_write` with a copy-on-write model, but none of these are wrapped for Python.
- `bpy.data.volumes.new()` / `bpy.data.volumes.load()` exist, but `new()` creates an empty datablock that must still point at a `.vdb` file on disk to get any grid content. There is **no supported Python path to construct a `Volume` datablock from an in-memory NumPy array or from a `pyopenvdb.FloatGrid`** (confirmed via [Blender Artists: "How can I get data from a volume grid?"](https://blenderartists.org/t/how-can-i-get-data-from-a-volume-grid/1365344), where the accepted workaround is to write a `.vdb` file and reload it).

**Consequence for this proposal:** a Python-only sculpt loop would have to (a) maintain its own `openvdb.FloatGrid`, (b) serialize to a temp `.vdb` file after each stroke, (c) reassign `Volume.filepath` to force a reload, then (d) meshify and swap into a mesh object for display. This is not viable at interactive rates for non-trivial grid sizes.

---

## 3. Prior art — voxel-adjacent Blender add-ons

None of the add-ons found implement genuine 3DCoat-style voxel sculpting. The landscape splits into:

- **Remesh wrappers (VDB-backed, but not sculpt):** [Voxel Master (Superhive)](https://superhivemarket.com/products/voxel-master) wraps the built-in Voxel Remesh with inflate/relax/iteration settings; from v0.0.4 it ships as a Geometry Nodes asset. Its ancestor is the community [OpenVDB Remesh patch D5407](https://developer.blender.org/D5407) / [blenderartists thread](https://blenderartists.org/t/openvdb-remesh/1102023), upstreamed in 2.81.
- **Cube/tile "voxel-look" authoring (not voxel engines):** [VOX](https://superhivemarket.com/products/vox-tools), [Voxelize 2.0](https://manfredpichler.gumroad.com/l/jcmOXY), [MagicaVoxel Vox Exporter](https://superhivemarket.com/products/vox-exporter-for-magicavoxel-and-voxedit), [BLENDVOXEL](https://github.com/yashkurade/BLENDVOXEL), [VoxelDraw](https://blender-addons.org/voxeldraw-add-on/), [Voxel Machine](https://blenderlabs.gumroad.com/l/voxelmachine) — all use one mesh cube per voxel.
- **Other "voxel" terminology:** [Voxel Heat Diffuse Skinning](https://superhivemarket.com/products/voxel-heat-diffuse-skinning) uses a grid internally for weight diffusion, unrelated to sculpting.

**Key finding:** no published add-on performs interactive SDF brush edits on an OpenVDB grid inside Blender. The closest approximation is Blender's own Voxel Remesh + sculpt loop (edit mesh → reproject to voxels → marching-cubes out), which is not a true voxel sculpt.

Importantly, **Blender 5.0 (Oct 2025) shipped native Geometry Node wrappers for OpenVDB SDF operations**: `Mesh to SDF Grid`, `Points to SDF Grid`, `SDF Grid Boolean`, `Grid to Mesh`, `Grid Curl/Divergence/Laplacian/Gradient`, `SDF Grid Fillet`, Mean Curvature filter, advection ([Volume Grids in Geometry Nodes, code.blender.org](https://code.blender.org/2025/10/volume-grids-in-geometry-nodes/); [Blender 5.0 GN release notes](https://developer.blender.org/docs/release_notes/5.0/geometry_nodes/)). This means an add-on can drive real VDB SDF operations **through the node tree** without C++ bindings, at the cost of re-evaluating the tree per edit.

---

## 4. C++ extension path inside an add-on

**Status: technically supported, but with real friction.**

- Blender Extensions supports Python wheels (`.whl`) with compiled binaries ([Python Wheels manual](https://docs.blender.org/manual/en/dev/advanced/extensions/python_wheels.html), design issue [#119681](https://projects.blender.org/blender/blender/issues/119681)). Extensions may bundle multiple `(platform, python)` wheels.
- Platform matrix in practice: `win_amd64`, `macosx_*_x86_64`, `macosx_*_arm64`, `manylinux_*_x86_64`, and increasingly `manylinux_*_aarch64`. Each must be a separately built wheel — reports show `macosx_10_9_universal2` wheels getting rejected against `macosx_11.2_arm64` Blender builds ([combined extensions thread](https://devtalk.blender.org/t/combined-add-on-extensions/34860)). Missing any target → the add-on fails to load for those users.
- **CPython version is pinned per Blender release.** Confirmed bundled: 4.0 = 3.10, 4.1 / 4.2 LTS / 4.3 / 4.4 = 3.11.x ([#113155](https://projects.blender.org/blender/blender/issues/113155), [#127090](https://projects.blender.org/blender/blender/issues/127090), [Blender Artists 4.4 thread](https://blenderartists.org/t/blender-4-4-python-3-11-11-issues/1590805)). Extension submissions are restricted to a single CPython version ([#127090](https://projects.blender.org/blender/blender/issues/127090)). **Uncertain:** I could not verify the CPython version in 5.0/5.1 from primary sources.
- `abi3` (Stable ABI) would cut rebuilds, but pybind11 has only partial experimental `abi3` support; OpenVDB's own Python binding is not `abi3`. ctypes/cffi avoid CPython ABI issues but require hand-rolled C shims around OpenVDB types.
- Shipping OpenVDB inside a wheel duplicates what Blender already carries (~tens of MB × 4 platforms). Better: use `bpy.utils.expose_bundled_modules()` + `import openvdb` and ship only a small C++ brush-kernel shim.

**Bottom line:** native binaries are permitted, but every Blender minor that bumps Python or OpenVDB forces a rebuild across four platform targets.

---

## 5. Meshing a VDB grid for viewport display

Options, roughly in order of quality:

1. **`openvdb::tools::volumeToMesh` / `VolumeToMesh`** — adaptive dual-contouring-ish with adaptivity, optionally feature-preserving via a reference surface. Quality is good; the docs note the reference-surface path is ~15% slower than the plain path ([openvdb.org VolumeToMesh docs](https://www.openvdb.org/documentation/doxygen/VolumeToMesh_8h.html)). **This is what Blender's Voxel Remesher already calls internally.**
2. **Blender's Volume-to-Mesh modifier / Voxel Remesh** — same underlying OpenVDB call, but exposed through Blender's DNA/modifier stack. Triggerable from Python by toggling the modifier or by issuing `bpy.ops.object.voxel_remesh()`. Adds operator-dispatch and depsgraph overhead per stroke.
3. **Blender 5.0 "Grid to Mesh" Geometry Node** ([docs](https://docs.blender.org/manual/en/latest/modeling/geometry_nodes/volume/operations/grid_to_mesh.html)) — same underlying call again, accessible via a GN tree. Good for a v0 prototype because it keeps everything in Blender's eval graph.
4. **Custom marching cubes / dual contouring in Python + NumPy** — far too slow for interactive rates on any useful grid.
5. **Screen-space SDF raymarching directly** — avoid meshing entirely; draw the grid with a custom GPU shader. Blender's `gpu` module permits custom viewport draw handlers but does not let you intercept selection / depth composition properly, and you lose all of Blender's sculpt brush integration.

**Performance reality check.** Community reports and the Blender manual's own warnings note that Voxel Remesh at small voxel sizes (<0.01) on ~1M-triangle inputs takes multiple seconds and can crash (see [blenderartists voxel remesh thread](https://blenderartists.org/t/openvdb-remesh/1102023), [3dx.info workflow guide](https://3dx.info/mastering-the-blender-sculpting-workflow-leveraging-voxel-remesh-and-dynamesh-for-cleaner-topology/)). **Uncertainty flag:** I did not find authoritative benchmarks specifically for re-meshing only the narrow band around a brush stamp, which is what 3DCoat does. 3DCoat's speed comes from (a) never remeshing globally between strokes and (b) doing local-only volumeToMesh in a small bounding box. Implementing the same pattern on top of Blender's whole-mesh replacement semantics is the principal performance challenge.

---

## 6. Key blockers

Ranked by severity for an add-on-only approach:

1. **No Python-level write access to `bpy.data.volumes[*]` grids.** The Volume datablock API is filepath-driven and read-only metadata only (Section 2). Every edit must round-trip through either a `.vdb` file on disk or a Geometry Nodes tree rebuild. Not interactive-rate.
2. **No sculpt brush integration point for volumes.** Blender's Sculpt mode operates on `Mesh` (and SubD/multires), not on `Volume`. The brush system, stroke API, PBVH, and undo stack all assume mesh input. An add-on would have to re-implement brush strokes from modal operators and live with Blender's undo system not covering them natively.
3. **Local meshing of a narrow band is not directly exposed.** Blender's Voxel Remesh always re-meshes the whole object. Without C++ access to `tools::volumeToMesh` on a user-provided bbox, per-stroke viewport meshing will be dominated by full-grid conversion cost (Section 5).
4. **Bundled `openvdb` module does not share grids with Blender's internal Volume storage.** An add-on can use `openvdb` for its own computation, but pushing the result back into a `Volume` datablock requires the file-round-trip (Section 1). No public plan to fix this was found.
5. **CPython + OpenVDB ABI churn.** Blender has bumped Python 3.10 → 3.11 within the 4.x cycle, and OpenVDB major versions change roughly yearly. Any compiled helper wheel must be re-cut per Blender release, across 4 platform targets, each time (Section 4).
6. **Undo/Redo semantics for volumes.** Blender's global undo does not understand in-memory VDB edits made outside the depsgraph. Add-ons typically re-apply operators; a voxel sculpt add-on would need a custom undo ring buffer of grid diffs.
7. **Viewport display of dynamic volumes.** `Volume` objects render correctly in EEVEE/Cycles as smoke/fog, but previewing them as solid SDF surfaces in Solid shading is not native — you either meshify (Section 5) or build a custom GPU draw handler (loses selection/occlusion).
8. **Blender Extensions policy** forbids network installs of binary deps — everything must be in the wheel ([guidelines](https://developer.blender.org/docs/features/extensions/moderation/guidelines/)). Fine in theory, painful at the ~50 MB OpenVDB wheel-size tier × 4 platforms.

---

## Recommended next steps

1. **Prototype the minimum viable round-trip.** In a plain Python script in Blender 4.4+, call `expose_bundled_modules()`, build a `FloatGrid` with one stamped sphere, write it to `tempfile.NamedTemporaryFile(".vdb")`, reload as a `Volume` datablock, and measure end-to-end latency for grid sizes 128³, 256³, 512³. This quantifies blocker #1.
2. **Prototype the Geometry Nodes path.** On Blender 5.0/5.1, build a GN tree that takes a parametric "brush stamp" (position, radius) and unions it into an SDF grid via `SDF Grid Boolean`, then `Grid to Mesh` out. Drive the stamp inputs from a modal Python operator. This is the most realistic *pure-Python* voxel sculpt path and lets us benchmark against blocker #3 without any C++.
3. **Decide the compiled-extension boundary early.** If prototypes 1 and 2 cannot hit ~30 ms per stroke stamp at 256³, commit to a minimal C++ helper wheel (pybind11 around a couple of OpenVDB calls: local-bbox `volumeToMesh`, SDF CSG in a bbox). Budget for 4-platform CI (win/mac-x64/mac-arm64/linux-x64) and a rebuild each Blender minor.
4. **Open a devtalk / projects.blender.org issue** requesting a supported Python path to construct a `Volume` datablock from an in-memory `openvdb.FloatGrid` without a disk round-trip. This is the single change that would most unlock add-on-based experimentation and may be small enough to land upstream. I did not find an existing ticket for this exact ask — filing one is free signal.
5. **Do not commit to "true 3DCoat parity" in an add-on.** Based on blockers #2 (sculpt-mode integration), #6 (undo), and #7 (native viewport), a serious voxel sculpt mode ultimately belongs in C++ inside Blender. Position the add-on explicitly as a **proof-of-concept and upstream design driver**, with a clear hand-off plan to a patch series once the interaction model is validated.
