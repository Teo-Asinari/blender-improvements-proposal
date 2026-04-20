# Voxel Sculpt: Technical Design

This document describes how the `voxel_sculpt` add-on
(`../addons/voxel_sculpt/`) is meant to grow from a skeleton into a
functional 3DCoat-style voxel sculpting tool for Blender 4.x. It covers
architecture, data flow, and the path to a native C++ extension.
Feasibility questions (notably whether `pyopenvdb` is exposed in 4.x)
are tracked as **Open questions** at the bottom.

## 1. Architecture overview

The add-on is a thin Python shell around an OpenVDB-backed voxel
engine, in four layers:

1. **UI** (`panel.py`, `properties.py`) -- sidebar panel and
   `VoxelSculptSettings` PropertyGroup. Purely declarative.
2. **Operators** (`operators.py`) -- object lifecycle
   (`voxel.new_voxel_object`, `voxel.remesh_to_mesh`) and the modal
   brush operator (`voxel.brush_modal`).
3. **Stroke / scheduling** (future `stroke.py`) -- converts modal
   events into brush dabs, tracks dirty tiles, debounces re-meshing.
4. **VDB backend** (`vdb_backend.py`) -- data plane: creates grids,
   applies CSG primitives, meshes the result. Two interchangeable
   implementations planned (section 4).

Blender's main thread owns the grid. Heavy work (meshing, large CSG)
runs on a worker thread inside the native backend and is marshalled
back via a thread-safe queue polled on a timer, avoiding GIL and
single-threaded-API constraints.

## 2. Data model

Three plausible homes for a voxel object:

| Option | Pros | Cons |
|---|---|---|
| Custom ID datablock (C++) | Clean, undo-friendly, round-trips in `.blend` | Requires forking Blender |
| `bpy.types.Volume` + ID-props | Existing datablock understands VDB on disk | Intended for rendering; API is read-mostly |
| Empty + external `.vdb` + preview Mesh | Works in an add-on today | Grid lives outside `.blend`; manual relink on load |

The skeleton leans toward **option 3**: a Mesh (the live preview
surface) acts as the selectable anchor, and a backend-owned grid is
referenced via an ID-property (`obj["voxel_sculpt.grid_id"]`). A
background `.vdb` file next to the `.blend` provides persistence. If
the Volume datablock ever gains edit-in-place support we migrate to
option 2.

Per-object state: `grid_id`, `voxel_size` (frozen at creation), dirty
tile bounds, preview-mesh name. Global settings (brush size, strength,
mode) live on `Scene.voxel_sculpt` for now; they should move to a
per-brush store so presets are first-class.

## 3. Brush pipeline

Per-stroke flow:

1. `voxel.brush_modal` checks for an active voxel object and installs
   a modal handler.
2. On `LEFTMOUSE PRESS`, raycast into the viewport against the
   **preview mesh** (already in Blender's BVH).
3. Transform the hit point into grid-local coords and enqueue a dab
   `(center, radius, strength, mode)`.
4. On `MOUSEMOVE`, place additional dabs along the path spaced at
   ~25% of the brush radius to avoid stippling.
5. Backend applies each dab, touching only affected narrow-band tiles
   and recording dirty bounds.
6. A debounced timer (~60 ms idle) runs `grid_to_mesh` restricted to
   dirty bounds and splices new triangles into the preview mesh.
7. `RELEASE` flushes and triggers a final clean re-mesh of the dirty
   region. `ESC` / `RIGHTMOUSE` cancels and restores the pre-stroke
   snapshot.

Undo leverages OpenVDB's copy-on-write tile sharing: snapshot the
root before each stroke, keep a bounded ring. A custom
`bpy.ops.ed.undo_push` at stroke end suffices initially.

## 4. C++ extension plan

A pure-Python path on top of `pyopenvdb` is viable only if that module
is actually exposed in Blender 4.x (open question). The long-term plan
is a small **pybind11** extension shipped as pre-built wheels inside
the add-on zip.

Minimal surface to expose:

- `create_level_set(voxel_size, half_width) -> GridHandle`
- `rasterise_sphere(grid, center, radius, mode, strength)` -- direct
  narrow-band write, no intermediate grid
- `filter_smooth(grid, bounds, iterations)` -- mean-curvature flow
  limited to a bounding box
- `volume_to_mesh(grid, iso, adaptivity, bounds=None) -> (verts, tris, quads)`
- `serialize` / `deserialize` for `.vdb` IO
- `snapshot` / `restore` for undo

Build and distribution:

- CMake + pybind11, linking OpenVDB, TBB, Blosc, and transitive deps.
- Target matrix: manylinux_2_28 x86_64/aarch64, macOS universal2,
  Windows x64 -- matching what Blender 4.x itself ships.
- Python ABI: `abi3` targeting 3.11 so one wheel covers 4.x point
  releases.
- Statically link OpenVDB to avoid clashing with any copy Blender has
  loaded. TBB is trickier: if Blender has already loaded a TBB, we
  must bind to it rather than ship a duplicate -- probe at load time.
- Ship wheels under `addons/voxel_sculpt/wheels/<platform>/` and
  unpack the right one on first enable. Blender 4.2+ extension format
  has first-class support for platform-specific wheels.

## 5. Python-only fallback

If neither `pyopenvdb` nor a native wheel is available, a degraded
mode keeps the UX coherent for small scenes:

- Dense `numpy.float32` grid, `N <= 128`.
- Dabs rasterised via vectorised distance-to-sphere and elementwise
  `min` (union) or negated `min` (difference).
- Meshing via scikit-image `marching_cubes`.
- No narrow band, no adaptivity, full-grid snapshots for undo.

At 128^3 this is ~8 MB per grid and marching cubes takes hundreds of
ms per rebuild -- demo-able, not a real tool.

## 6. Performance budget

Rough targets per brush dab on mid-range hardware (8-core x86_64, 2024
era), assuming dirty-tile re-meshing (only touched 8^3 tiles) and a
brush radius of ~10 voxels:

| Voxel size | CSG dab | Tile re-mesh | Notes |
|---|---|---|---|
| 0.04 m | < 2 ms | < 8 ms | Comfortable >120 Hz |
| 0.02 m | < 5 ms | < 20 ms | Default UX target (~60 Hz) |
| 0.01 m | < 15 ms | < 60 ms | Fine-detail pass |
| 0.005 m | < 50 ms | < 250 ms | Batch-only |

Full-grid re-meshing is linear in active voxels and is used only on
stroke release or "Remesh to Mesh". The Python-only fallback is closer
to 2-5 Hz at 64^3 and under 1 Hz at 128^3.

## 7. Open questions

Tracked by the research agent; not prejudged here.

- Is `pyopenvdb` exposed in Blender 4.x's bundled Python? If so, which
  OpenVDB API version?
- Does `bpy.types.Volume` support in-place grid edits from Python, or
  is it strictly a render-time input?
- State of GPU-resident VDB in Cycles / EEVEE: can we display the
  sculpt directly without re-meshing?
- Which TBB / Blosc / IlmBase versions does Blender 4.x link against?
  Determines whether shared-lib extensions are viable at all.
- Blender 4.2 extension platform: does it handle compiled wheels
  cleanly, or do we manage unpack ourselves?
- Can a custom operator participate in Blender's global undo stack
  with a memory-bounded custom payload?
