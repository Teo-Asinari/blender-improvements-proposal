"""Standalone voxel sculpt reference / benchmark.

This is the Python-only fallback described in docs/VOXEL_SCULPT_DESIGN.md
section 5: a dense numpy SDF grid, vectorised sphere CSG, marching-cubes
meshing. It is deliberately unoptimised so the code reads as executable
pseudocode for the fallback path.

NOT the target architecture. The real backend is narrow-band sparse via
OpenVDB: dense grids scale O(N^3) in memory (128^3 float32 = 8 MB, 256^3
= 64 MB, 512^3 = 512 MB) and re-mesh the entire volume every rebuild
instead of just dirty tiles. Use this script to sanity-check the CSG
math and to generate a wall-clock baseline to beat, nothing more.

Example:
    python tools/voxel_sculpt_demo.py --output out.obj --resolution 128
    python tools/voxel_sculpt_demo.py --scene figure --benchmark

Dependencies: numpy, scikit-image. No Blender required.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np


# Bounds of the sculpt cube in world units. Grid coords map linearly to
# [-WORLD_EXTENT, +WORLD_EXTENT] on every axis so scenes look the same
# at any resolution.
WORLD_EXTENT = 1.0

# SDF sentinel for "far outside the surface". Marching cubes only needs
# the sign and a reasonable gradient near iso=0, so clamping to a finite
# value keeps float32 well-behaved across smoothing passes.
FAR = 10.0 * WORLD_EXTENT


@dataclass
class Op:
    kind: str
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    radius: float = 0.0
    region: tuple[tuple[float, float, float], float] | None = None
    iterations: int = 1


@dataclass
class Timings:
    phases: list[tuple[str, float]] = field(default_factory=list)

    def record(self, label: str, seconds: float) -> None:
        self.phases.append((label, seconds))

    def total(self) -> float:
        return sum(s for _, s in self.phases)

    def dump(self, stream=sys.stdout) -> None:
        width = max((len(label) for label, _ in self.phases), default=8)
        for label, seconds in self.phases:
            stream.write(f"  {label:<{width}}  {seconds * 1000.0:>9.2f} ms\n")
        stream.write(f"  {'total':<{width}}  {self.total() * 1000.0:>9.2f} ms\n")


class Grid:
    """Dense float32 SDF on a uniform cube.

    Index convention: `sdf[i, j, k]` where i,j,k index x,y,z. Signed
    distance is positive outside, negative inside, zero on the surface.
    """

    def __init__(self, resolution: int) -> None:
        self.n = resolution
        self.voxel_size = (2.0 * WORLD_EXTENT) / resolution
        self.sdf = np.full((resolution,) * 3, FAR, dtype=np.float32)
        # Precompute world-space coordinates of voxel centres once; all
        # CSG ops slice into these views rather than rebuilding meshgrid
        # arrays every call.
        axis = np.linspace(
            -WORLD_EXTENT + 0.5 * self.voxel_size,
            WORLD_EXTENT - 0.5 * self.voxel_size,
            resolution,
            dtype=np.float32,
        )
        self._axis = axis

    def world_to_index(self, p: tuple[float, float, float]) -> tuple[int, int, int]:
        return tuple(
            int(round((c + WORLD_EXTENT) / self.voxel_size - 0.5))
            for c in p
        )

    def box_around(
        self,
        center: tuple[float, float, float],
        radius: float,
        pad: int = 1,
    ) -> tuple[slice, slice, slice]:
        """AABB of a sphere clipped to the grid, with extra padding.

        Padding matters: sphere CSG needs one extra voxel so the SDF
        gradient stays continuous across the tile boundary, and smoothing
        needs the filter's half-width so edge voxels see full support.
        """
        ijk = []
        for axis, c in enumerate(center):
            lo = (c - radius + WORLD_EXTENT) / self.voxel_size - pad
            hi = (c + radius + WORLD_EXTENT) / self.voxel_size + pad
            lo_i = max(0, int(math.floor(lo)))
            hi_i = min(self.n, int(math.ceil(hi)) + 1)
            ijk.append(slice(lo_i, hi_i))
        return tuple(ijk)

    def coords(
        self,
        sl: tuple[slice, slice, slice],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return np.ix_(self._axis[sl[0]], self._axis[sl[1]], self._axis[sl[2]])


def sphere_sdf(
    xs: np.ndarray, ys: np.ndarray, zs: np.ndarray,
    center: tuple[float, float, float],
    radius: float,
) -> np.ndarray:
    cx, cy, cz = center
    # Broadcast explicit to avoid allocating a full XYZ meshgrid; each
    # axis array is 1D with a unique non-singleton dim (from np.ix_).
    d = np.sqrt(
        (xs - cx) ** 2 + (ys - cy) ** 2 + (zs - cz) ** 2
    ).astype(np.float32)
    d -= np.float32(radius)
    return d


def add_sphere(
    grid: Grid,
    center: tuple[float, float, float],
    radius: float,
) -> None:
    sl = grid.box_around(center, radius)
    xs, ys, zs = grid.coords(sl)
    sd = sphere_sdf(xs, ys, zs, center, radius)
    region = grid.sdf[sl]
    # CSG union of two SDFs is the pointwise minimum.
    np.minimum(region, sd, out=region)


def subtract_sphere(
    grid: Grid,
    center: tuple[float, float, float],
    radius: float,
) -> None:
    sl = grid.box_around(center, radius)
    xs, ys, zs = grid.coords(sl)
    sd = sphere_sdf(xs, ys, zs, center, radius)
    region = grid.sdf[sl]
    # A - B = max(A, -B) on SDFs. Negating flips inside/outside of B so
    # the intersection with A's exterior becomes the union operand.
    np.maximum(region, -sd, out=region)


def smooth(
    grid: Grid,
    center: tuple[float, float, float],
    radius: float,
    iterations: int = 1,
) -> None:
    # Half-width 1 box filter (3x3x3 mean). Real tool uses mean-curvature
    # flow; this is cheaper, and sufficient to demonstrate the hook.
    pad = iterations + 1
    sl = grid.box_around(center, radius, pad=pad)
    region = grid.sdf[sl].copy()
    for _ in range(iterations):
        region = _box_blur_3x3x3(region)
    grid.sdf[sl] = region


def _box_blur_3x3x3(a: np.ndarray) -> np.ndarray:
    # Separable 3-tap blur along each axis, replicate-padding so the
    # boundary voxels do not drift toward zero (which would carve holes
    # in an isosurface).
    out = a
    for axis in range(3):
        padded = np.empty(
            tuple(s + 2 if i == axis else s for i, s in enumerate(out.shape)),
            dtype=out.dtype,
        )
        slicer_mid = [slice(None)] * 3
        slicer_mid[axis] = slice(1, -1)
        padded[tuple(slicer_mid)] = out
        slicer_lo = [slice(None)] * 3
        slicer_lo[axis] = slice(0, 1)
        slicer_src_lo = [slice(None)] * 3
        slicer_src_lo[axis] = slice(0, 1)
        padded[tuple(slicer_lo)] = out[tuple(slicer_src_lo)]
        slicer_hi = [slice(None)] * 3
        slicer_hi[axis] = slice(-1, None)
        slicer_src_hi = [slice(None)] * 3
        slicer_src_hi[axis] = slice(-1, None)
        padded[tuple(slicer_hi)] = out[tuple(slicer_src_hi)]
        s_lo = [slice(None)] * 3
        s_mi = [slice(None)] * 3
        s_hi = [slice(None)] * 3
        s_lo[axis] = slice(0, -2)
        s_mi[axis] = slice(1, -1)
        s_hi[axis] = slice(2, None)
        out = (
            padded[tuple(s_lo)] + padded[tuple(s_mi)] + padded[tuple(s_hi)]
        ) / 3.0
    return out.astype(np.float32, copy=False)


OP_DISPATCH: dict[str, Callable[..., None]] = {
    "add": lambda g, op: add_sphere(g, op.center, op.radius),
    "sub": lambda g, op: subtract_sphere(g, op.center, op.radius),
    "smooth": lambda g, op: smooth(
        g, op.region[0], op.region[1], iterations=op.iterations
    ),
}


def scene_blob() -> list[Op]:
    return [
        Op("add", center=(0.0, 0.0, 0.0), radius=0.55),
        Op("add", center=(0.35, 0.2, 0.0), radius=0.25),
        Op("add", center=(-0.35, -0.2, 0.0), radius=0.25),
        Op("sub", center=(0.0, 0.0, 0.45), radius=0.3),
        Op("sub", center=(0.0, 0.0, -0.45), radius=0.3),
        Op("smooth", region=((0.0, 0.0, 0.0), 0.8), iterations=2),
    ]


def scene_figure() -> list[Op]:
    # Crude snowman: three stacked balls with "eye" cavities and a
    # chopped base so marching cubes produces visible features beyond a
    # single sphere.
    return [
        Op("add", center=(0.0, -0.55, 0.0), radius=0.35),
        Op("add", center=(0.0, -0.05, 0.0), radius=0.28),
        Op("add", center=(0.0, 0.38, 0.0), radius=0.22),
        Op("sub", center=(0.12, 0.42, 0.18), radius=0.05),
        Op("sub", center=(-0.12, 0.42, 0.18), radius=0.05),
        Op("sub", center=(0.0, 0.30, 0.20), radius=0.04),
        Op("sub", center=(0.0, -0.9, 0.0), radius=0.5),
        Op("smooth", region=((0.0, 0.0, 0.0), 1.0), iterations=1),
    ]


def scene_swiss() -> list[Op]:
    ops: list[Op] = [Op("add", center=(0.0, 0.0, 0.0), radius=0.7)]
    rng = random.Random(0)
    for _ in range(24):
        c = (
            rng.uniform(-0.6, 0.6),
            rng.uniform(-0.6, 0.6),
            rng.uniform(-0.6, 0.6),
        )
        r = rng.uniform(0.08, 0.22)
        ops.append(Op("sub", center=c, radius=r))
    ops.append(Op("smooth", region=((0.0, 0.0, 0.0), 0.9), iterations=1))
    return ops


SCENES: dict[str, Callable[[], list[Op]]] = {
    "blob": scene_blob,
    "figure": scene_figure,
    "swiss": scene_swiss,
}


def scene_random(seed: int) -> list[Op]:
    rng = random.Random(seed)
    ops: list[Op] = []
    for _ in range(rng.randint(3, 6)):
        c = (rng.uniform(-0.4, 0.4),) * 1 + (
            rng.uniform(-0.4, 0.4),
            rng.uniform(-0.4, 0.4),
        )
        ops.append(Op("add", center=c, radius=rng.uniform(0.2, 0.45)))
    for _ in range(rng.randint(2, 5)):
        c = (
            rng.uniform(-0.5, 0.5),
            rng.uniform(-0.5, 0.5),
            rng.uniform(-0.5, 0.5),
        )
        ops.append(Op("sub", center=c, radius=rng.uniform(0.1, 0.3)))
    ops.append(Op("smooth", region=((0.0, 0.0, 0.0), 1.0), iterations=1))
    return ops


def run_scene(
    resolution: int,
    ops: list[Op],
    timings: Timings,
    verbose: bool = False,
) -> Grid:
    t0 = time.perf_counter()
    grid = Grid(resolution)
    timings.record("alloc", time.perf_counter() - t0)
    if verbose:
        print(f"  grid {resolution}^3 ({grid.sdf.nbytes / (1 << 20):.1f} MB)")

    for idx, op in enumerate(ops):
        t0 = time.perf_counter()
        OP_DISPATCH[op.kind](grid, op)
        dt = time.perf_counter() - t0
        label = f"{op.kind}[{idx}]"
        timings.record(label, dt)
        if verbose:
            detail = ""
            if op.kind in ("add", "sub"):
                detail = f"c={op.center} r={op.radius:.3f}"
            elif op.kind == "smooth":
                detail = f"iter={op.iterations}"
            print(f"  {label:<10} {dt * 1000:.2f} ms  {detail}")
    return grid


def mesh_grid(
    grid: Grid, timings: Timings
) -> tuple[np.ndarray, np.ndarray]:
    from skimage import measure

    t0 = time.perf_counter()
    # Iso=0 is the surface by construction. Spacing converts marching
    # cubes' index-space vertices into world units so the OBJ is scale-
    # independent of resolution.
    vs = grid.voxel_size
    try:
        verts, faces, _normals, _values = measure.marching_cubes(
            grid.sdf, level=0.0, spacing=(vs, vs, vs)
        )
    except (ValueError, RuntimeError) as exc:
        timings.record("mesh", time.perf_counter() - t0)
        raise SystemExit(
            f"marching_cubes produced no surface ({exc}). "
            "Scene may be empty or entirely inside; try a different scene."
        )
    # Shift so the mesh is centred on the origin to match the world box.
    verts = verts - np.float32(WORLD_EXTENT)
    timings.record("mesh", time.perf_counter() - t0)
    return verts, faces


def write_obj(path: str, verts: np.ndarray, faces: np.ndarray) -> None:
    # Hand-rolled OBJ to keep deps at two packages. OBJ is 1-indexed and
    # whitespace-delimited. We skip normals/uvs; any DCC will recompute.
    with open(path, "w", encoding="ascii") as f:
        f.write("# voxel_sculpt_demo.py\n")
        f.write(f"# {len(verts)} vertices, {len(faces)} faces\n")
        for v in verts:
            f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
        for tri in faces:
            f.write(f"f {tri[0] + 1} {tri[1] + 1} {tri[2] + 1}\n")


def build_scene(args: argparse.Namespace) -> list[Op]:
    if args.seed is not None:
        return scene_random(args.seed)
    return SCENES[args.scene]()


def estimate_memory_mb(resolution: int) -> float:
    # SDF is float32 (4 bytes). Marching cubes allocates its own working
    # buffers on top, so budget ~4x the raw grid as a conservative peak.
    bytes_per_voxel = 4
    envelope = 4
    return envelope * bytes_per_voxel * (resolution ** 3) / (1 << 20)


def run_benchmark(args: argparse.Namespace) -> int:
    resolutions = [32, 64, 128, 256]
    mem_budget_mb = args.memory_budget_mb
    rows: list[tuple[int, str, float, float, int, int]] = []
    print(f"benchmark scene='{args.scene}' memory_budget={mem_budget_mb:.0f} MB")
    print(
        f"{'N':>5} {'status':>10} {'csg ms':>10} {'mesh ms':>10} "
        f"{'verts':>10} {'faces':>10}"
    )
    for n in resolutions:
        est = estimate_memory_mb(n)
        if est > mem_budget_mb:
            print(
                f"{n:>5} {'SKIP':>10} "
                f"(~{est:.0f} MB exceeds budget)"
            )
            continue
        timings = Timings()
        ops = build_scene(args)
        try:
            grid = run_scene(n, ops, timings, verbose=False)
            verts, faces = mesh_grid(grid, timings)
        except MemoryError:
            print(f"{n:>5} {'OOM':>10}")
            continue
        csg_ms = sum(
            s for lbl, s in timings.phases
            if lbl not in ("alloc", "mesh")
        ) * 1000.0
        mesh_ms = next(
            s for lbl, s in timings.phases if lbl == "mesh"
        ) * 1000.0
        rows.append((n, "ok", csg_ms, mesh_ms, len(verts), len(faces)))
        print(
            f"{n:>5} {'ok':>10} {csg_ms:>10.2f} {mesh_ms:>10.2f} "
            f"{len(verts):>10d} {len(faces):>10d}"
        )
        del grid, verts, faces
    if not rows:
        print("no resolutions completed successfully")
        return 1
    return 0


def run_once(args: argparse.Namespace) -> int:
    ops = build_scene(args)
    timings = Timings()
    if args.verbose:
        src = f"seed={args.seed}" if args.seed is not None else f"scene={args.scene}"
        print(f"sculpt [{src}] at {args.resolution}^3 -> {args.output}")

    grid = run_scene(args.resolution, ops, timings, verbose=args.verbose)
    verts, faces = mesh_grid(grid, timings)

    t0 = time.perf_counter()
    write_obj(args.output, verts, faces)
    timings.record("write", time.perf_counter() - t0)

    print(f"mesh: {len(verts)} vertices, {len(faces)} faces -> {args.output}")
    timings.dump()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Standalone voxel sculpt reference (Python-only fallback).",
    )
    p.add_argument(
        "--output", "-o", default="voxel_sculpt_demo.obj",
        help="Path to write the .obj mesh (default: voxel_sculpt_demo.obj).",
    )
    p.add_argument(
        "--resolution", "-r", type=int, default=128,
        help="Grid resolution per axis (default 128).",
    )
    p.add_argument(
        "--scene", choices=sorted(SCENES.keys()), default="blob",
        help="Predefined scene (ignored if --seed is given).",
    )
    p.add_argument(
        "--seed", type=int, default=None,
        help="If set, build a random scene with this seed instead of --scene.",
    )
    p.add_argument(
        "--benchmark", action="store_true",
        help="Run the scene at 32/64/128/256 and print a timing table.",
    )
    p.add_argument(
        "--memory-budget-mb", type=float, default=2048.0,
        help="Max grid working-set in --benchmark mode before skipping (MB).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-op timings as they run.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.resolution < 8:
        print("resolution must be >= 8", file=sys.stderr)
        return 2
    if args.benchmark:
        return run_benchmark(args)
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
