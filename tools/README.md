# tools

Standalone utilities that do **not** require Blender.

## voxel_sculpt_demo.py

A command-line reference implementation of the voxel-sculpt Python-only
fallback described in [`../docs/VOXEL_SCULPT_DESIGN.md`](../docs/VOXEL_SCULPT_DESIGN.md)
section 5. It builds a dense `numpy.float32` signed-distance grid,
applies a scripted sequence of sphere CSG operations, meshes the result
with `skimage.measure.marching_cubes`, and writes an `.obj`.

This is a reference and a benchmark baseline, not the target
architecture. The real backend will be narrow-band sparse via OpenVDB.
Dense grids scale O(N^3) in memory (128^3 = 8 MB, 256^3 = 64 MB,
512^3 = 512 MB) and re-mesh the entire volume on every rebuild.

### Install

```
pip install numpy scikit-image
```

No other dependencies.

### Run

Default scene at 128^3, write to `out.obj`:

```
python tools/voxel_sculpt_demo.py --output out.obj
```

Pick one of the built-in scenes (`blob`, `figure`, `swiss`):

```
python tools/voxel_sculpt_demo.py --scene figure --resolution 128 -o figure.obj
```

Random scene from a seed (overrides `--scene`):

```
python tools/voxel_sculpt_demo.py --seed 42 -o random.obj
```

Per-op timings:

```
python tools/voxel_sculpt_demo.py --verbose -o out.obj
```

### Benchmark

Run the chosen scene at 32, 64, 128, and 256 and print a timing table:

```
python tools/voxel_sculpt_demo.py --benchmark --scene swiss
```

Resolutions whose estimated working set exceeds `--memory-budget-mb`
(default 2048) are skipped with a note. Lower the budget to force skips
on constrained hosts:

```
python tools/voxel_sculpt_demo.py --benchmark --memory-budget-mb 64
```

### Output

OBJ only, vertices and triangle faces. No normals, no UVs, no
materials; any DCC will recompute normals on import. The mesh is
centred on the origin in a `[-1, +1]` cube regardless of resolution.

### Limitations

- Dense grid: memory is O(N^3). Do not push past 256^3 without a
  machine that can spare ~1 GB peak.
- Smoothing is a 3x3x3 box blur, not mean-curvature flow. Close enough
  for a demo, wrong for production.
- No narrow band, no dirty-tile tracking, no undo. The design doc
  covers what the native path does differently.
