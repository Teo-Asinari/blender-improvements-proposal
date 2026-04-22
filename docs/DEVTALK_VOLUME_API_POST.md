# Proposal: construct `bpy.data.volumes` entries from an in-memory `openvdb.FloatGrid`

Category: Python API / Volumes module

## Motivation

I am prototyping a voxel-sculpt add-on (3DCoat-style SDF brushes on an OpenVDB grid, interactive rates). The add-on keeps its own `openvdb.FloatGrid` in Python, stamps brush edits into a narrow band around the cursor, and needs the result visible in Blender's viewport on every stroke sample. The limiting factor today is not meshing, not the brush kernel, not undo. It is the step in the middle: getting the edited grid from `openvdb`-Python memory into a `bpy.types.Volume` datablock.

The only documented way to do that in 4.4 is to write a temporary `.vdb` and call `bpy.data.volumes.load()` (or reassign `Volume.filepath`). That disk round-trip is the blocker I would like to discuss.

## Current state

- `pyopenvdb` has been bundled since 3.6 ([D8123](https://developer.blender.org/D8123), [devtalk thread](https://devtalk.blender.org/t/build-pyopenvdb-as-part-of-make-deps/14148)). In 4.4 it was renamed to `openvdb` and removed from default `sys.path`; add-ons import it via `bpy.utils.expose_bundled_modules()` ([4.4 release notes](https://developer.blender.org/docs/release_notes/4.4/python_api/)). This part works well.
- The bundled module is stock upstream OpenVDB. Grids it constructs live in their own Python-side memory and do **not** share C++ pointers with Blender's internal `Volume` storage.
- `bpy.types.VolumeGrid` exposes only read-only metadata (`name`, `data_type`, `channels`, `is_loaded`, `matrix_object`, `load()`, `unload()`). The C API around `BKE_volume_grid_openvdb_for_read` / `..._for_write` and its copy-on-write machinery is not wrapped.
- Net effect: to push an edited grid to a `Volume` datablock, the only supported path is serialize to `.vdb`, then `bpy.data.volumes.load(path)` — the workaround the community is using ([Blender Artists thread](https://blenderartists.org/t/how-can-i-get-data-from-a-volume-grid/1365344)).

I searched projects.blender.org and devtalk and could not find a tracker item for this specific ask; apologies if I missed one.

## Why the round-trip blocks interactive work

Sculpt-sample budget is ~16-33 ms end to end. At a representative grid (sphere SDF, ~5M active voxels, narrow band), each stage of the round-trip costs tens of ms on a modern SSD: `grid.write()` (blosc + I/O), `bpy.data.volumes.load()` (file reparse), plus tempfile lifecycle (non-trivial on Windows). Even at the low end this dominates the budget, causes visible GC / mmap churn, and interacts badly with Blender's file-watching. It is also strictly wasteful: no serialize, no parse, no second copy.

`tools/volume_roundtrip_benchmark.py` in the proposal repo quantifies this per machine — I will post numbers in a reply so they are easy to reproduce.

## Proposed API shape (minimal)

Nothing exotic — mirror the existing `.load()` shape, just from a grid object:

```python
import bpy, openvdb
bpy.utils.expose_bundled_modules()

grid = openvdb.FloatGrid()
grid.name = "density"
# ... fill grid ...

# New: construct a Volume datablock directly from one or more grids.
vol = bpy.data.volumes.new_from_grids("MyVolume", [grid])

# New: replace/update the grid(s) on an existing datablock in place.
vol.grids.update_from_grids([grid])            # replace all
vol.grids["density"].update_from_grid(grid)    # replace one by name
```

Proposed semantics:

- Ownership transfer via the existing COW path (`BKE_volume_grid_openvdb_for_write`), so no voxel copy is required in the common case.
- `update_from_grid` is a no-reload operation — bumps runtime state and tags dependents without touching `filepath`.
- `filepath` becomes optional for these datablocks; save-to-blend serializes the grid inline, the same way a loaded `.vdb` is handled today.
- No new types — reuse `bpy.types.Volume` / `VolumeGrid`.

Names are suggestive, not prescriptive; `bpy.data.volumes.new(name, grids=[...])` or a classmethod would work equally well.

## Offer

If the shape above is acceptable, I am willing to prototype a patch (RNA additions plus thin wrapping around the existing COW accessor) and put it up as a draft PR. Before writing code I wanted to (a) confirm there is no in-flight work I have missed, and (b) sanity-check the API shape — cheaper to redesign here than in review.

## References

- Bundled Python modules in 4.4: https://developer.blender.org/docs/release_notes/4.4/python_api/
- `bpy.types.Volume`: https://docs.blender.org/api/current/bpy.types.Volume.html
- `bpy.types.VolumeGrid`: https://docs.blender.org/api/current/bpy.types.VolumeGrid.html
- `bpy.types.VolumeGrids`: https://docs.blender.org/api/current/bpy.types.VolumeGrids.html
- Source of the current loader: `source/blender/blenkernel/intern/volume.cc` and `source/blender/makesrna/intern/rna_volume.cc` in blender/blender.
- Original pyopenvdb build patch: https://developer.blender.org/D8123
- Sculpt / Paint / Texture Module Meeting, 2025-01-28 — there is an open thread there on interaction between sculpt tooling and volume/grid workflows that this ask is adjacent to; cross-linking in case it is useful for the Volumes module to coordinate.

Thanks for reading. Happy to reshape this based on feedback.
