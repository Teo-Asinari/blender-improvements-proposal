# volume_roundtrip_benchmark.py
#
# Measures the current cost of the openvdb.FloatGrid -> .vdb file ->
# bpy.data.volumes.load() round-trip that Blender add-ons are forced to use
# to push an in-memory OpenVDB grid into a Volume datablock.
#
# How to run:
#
#   1) Scripting workspace: open Blender 4.4+, paste this file into a Text
#      block, press Run Script. Output goes to the system console
#      (Window > Toggle System Console on Windows; launch Blender from a
#      terminal on macOS/Linux).
#
#   2) Command line:
#          blender --background --python tools/volume_roundtrip_benchmark.py
#      or to tweak parameters:
#          blender --background --python tools/volume_roundtrip_benchmark.py -- \
#              --resolution 256 --iterations 5
#
# The script imports cleanly outside Blender; it only touches bpy / openvdb
# inside functions, so it can be linted / imported by tools that don't have
# them installed.

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import tempfile
import time
from typing import Callable


def _expose_bundled_openvdb():
    """Import the bundled openvdb module, handling the 4.4+ rename.

    Returns the module, or raises RuntimeError with an actionable message.
    """
    try:
        import bpy  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "bpy is not importable. Run this script inside Blender "
            "(Scripting workspace) or via `blender --python`."
        ) from exc

    # 4.4+ requires this before `import openvdb` works.
    try:
        import bpy
        expose = getattr(bpy.utils, "expose_bundled_modules", None)
        if expose is not None:
            expose()
    except Exception:
        # Older Blender (3.6-4.3) exposes pyopenvdb directly; ignore.
        pass

    last_err: Exception | None = None
    for name in ("openvdb", "pyopenvdb"):
        try:
            return __import__(name)
        except ImportError as exc:
            last_err = exc
    raise RuntimeError(
        "Neither `openvdb` (Blender 4.4+) nor `pyopenvdb` (3.6-4.3) could be "
        "imported. On some distro builds (e.g. NixOS) the module is simply "
        "not shipped; use an official Blender build from blender.org."
    ) from last_err


def _build_sphere_sdf(vdb, resolution: int, voxel_size: float):
    """Construct a FloatGrid holding a sphere SDF at roughly the target
    active-voxel count implied by `resolution` on a side.

    We use the narrow-band level set helper so active-voxel count scales with
    surface area, not volume, matching how a brush stroke stamps a grid.
    """
    radius_world = 0.5 * resolution * voxel_size
    half_width = 3.0  # voxels of narrow band on each side of the surface
    grid = vdb.createLevelSetSphere(
        radius=radius_world,
        center=(0.0, 0.0, 0.0),
        voxelSize=voxel_size,
        halfWidth=half_width,
    )
    grid.name = "density"
    return grid


def _time(fn: Callable[[], object]) -> tuple[float, object]:
    t0 = time.perf_counter()
    result = fn()
    return (time.perf_counter() - t0) * 1000.0, result


def _run_once(vdb, bpy, resolution: int, voxel_size: float) -> dict:
    t_create, grid = _time(lambda: _build_sphere_sdf(vdb, resolution, voxel_size))

    tmp = tempfile.NamedTemporaryFile(suffix=".vdb", delete=False)
    tmp.close()
    path = tmp.name
    try:
        t_write, _ = _time(lambda: vdb.write(path, grids=[grid]))
        file_bytes = os.path.getsize(path)

        t_load, vol = _time(lambda: bpy.data.volumes.load(path))
        # Force the grid metadata to populate so the cost is in the timing.
        _ = list(vol.grids)

        bpy.data.volumes.remove(vol)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    total = t_create + t_write + t_load
    active = grid.activeVoxelCount() if hasattr(grid, "activeVoxelCount") else -1
    return {
        "create_ms": t_create,
        "write_ms": t_write,
        "load_ms": t_load,
        "total_ms": total,
        "file_bytes": file_bytes,
        "active_voxels": active,
    }


def _fmt_mean_stdev(values: list[float]) -> str:
    if len(values) == 1:
        return f"{values[0]:8.2f}"
    return f"{statistics.mean(values):8.2f} +/- {statistics.stdev(values):6.2f}"


def _print_table(rows: list[dict], resolution: int, voxel_size: float) -> None:
    n = len(rows)
    keys = ("create_ms", "write_ms", "load_ms", "total_ms")
    labels = {
        "create_ms": "grid build",
        "write_ms":  ".vdb write",
        "load_ms":   "volumes.load",
        "total_ms":  "end-to-end",
    }
    active = rows[0]["active_voxels"]
    size_mb = rows[0]["file_bytes"] / (1024 * 1024)

    print()
    print(f"resolution      : {resolution}^3 (voxel_size={voxel_size})")
    print(f"active voxels   : {active:,}" if active >= 0 else "active voxels   : n/a")
    print(f".vdb file size  : {size_mb:.2f} MiB")
    print(f"iterations      : {n}")
    print()
    print("| stage        | mean +/- stdev (ms) |")
    print("|--------------|---------------------|")
    for k in keys:
        print(f"| {labels[k]:<12} | {_fmt_mean_stdev([r[k] for r in rows])} |")
    print()


def main(argv: list[str]) -> int:
    # Blender passes script args after `--`.
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = argv[1:] if argv and argv[0].endswith(".py") else argv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolution", type=int, default=256,
                        help="grid side in voxels (default 256; ~5M active at NB=3)")
    parser.add_argument("--voxel-size", type=float, default=0.01)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args(argv)

    try:
        vdb = _expose_bundled_openvdb()
        import bpy
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Warmup: JIT-ish caches, filesystem metadata, and Blender's lazy loader.
    _run_once(vdb, bpy, args.resolution, args.voxel_size)

    rows = [
        _run_once(vdb, bpy, args.resolution, args.voxel_size)
        for _ in range(args.iterations)
    ]
    _print_table(rows, args.resolution, args.voxel_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
