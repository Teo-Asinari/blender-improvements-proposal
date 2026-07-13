# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure voxel-remesh cost estimator. NO bpy import — the bpy layer
(core.py) feeds it plain numbers, so the whole module also runs under a
bare python3 and the headless suite can exercise every edge case without
building meshes.

Terminology discipline (from research/scale-aware-remesh-safety.md): the
destructive Voxel Remesh OPERATION (``bpy.ops.object.voxel_remesh``,
settings on the Mesh datablock) and the non-destructive Remesh MODIFIER
in VOXEL mode are separate entry points with different geometry sources.
This module is deliberately ignorant of which one it serves: the caller
states the geometry source label and whether the fed bounds/area are
exact for that entry point, and the result carries both verbatim.

Coordinate space: every distance handed to :func:`estimate` — bounds AND
voxel size — must be OBJECT/local space. Probed on 5.1.2: the native
voxel size is object-space (a cube with unapplied 2x scale remeshes to
the identical vert count), so mixing world-scaled dimensions with the
native voxel size is exactly the mismatch this add-on exists to prevent.
The result's ``coordinate_space`` field says 'OBJECT' so downstream UI
can never "forget" the space.

Scores (all formulas from the research doc):

- ``axis_cells``     — per-axis ``ceil(d/v) + padding``. The padding
  (default 1) is documented honesty, not physics: OpenVDB adds narrow-
  band slop around the surface and an exact-looking count would imply
  false precision.
- ``bounding_cells`` — product of the padded axis counts with SATURATING
  integer arithmetic (capped at :data:`SATURATION_CAP`). A conservative
  DOMAIN risk indicator, not an OpenVDB memory prediction (VDB grids are
  sparse).
- ``longest_axis_cells`` — raw unpadded ``ceil(max(d)/v)``; the intuitive
  number the UI leads with.
- ``surface_cells``  — ``area / v**2``, a RELATIVE output-complexity
  score. Never presented as a face count or megabytes: the face factor
  depends on topology and remesher version and is uncalibrated.

Risk bands compare ``log10(bounding_cells)`` against configurable
yellow/red exponents (defaults :data:`DEFAULT_YELLOW_EXP` /
:data:`DEFAULT_RED_EXP`, i.e. ~215^3 and ~1000^3 cells). Bands are a
warn-and-override device — the estimator itself rejects ONLY invalid
input (``v <= 0``, non-finite values): zero extents are a probed-valid
remesh input (a flat plane voxel-remeshes fine on 5.1.2).

Scale analysis uses the singular values of the world matrix's 3x3 block
(pure-python Jacobi on M^T M): for sheared transforms the three scale
properties/column norms lie, singular values do not (research doc). The
conservative world->object conversion divides a requested world-space
cell size by the LARGEST singular value so no transformed axis comes out
coarser than the target.
"""

import math

# Saturation cap for the bounding-cell product. Python ints do not
# overflow, but a score beyond this is numerically meaningless noise and
# downstream consumers (UI formatting, thresholds) want a bounded value.
# 2**63 - 1 keeps the value representable in anything C-sized.
SATURATION_CAP = 2 ** 63 - 1

# Documented per-axis padding for the bounding score (see module doc).
DEFAULT_PADDING = 1

# Default log10(bounding_cells) band thresholds. 1e7 ~= 215 cells/axis
# uniform (seconds of work, real memory), 1e9 ~= 1000 cells/axis
# (gigabytes of narrow band, likely stall). Deliberately configurable:
# Implementation-sequence §5 (calibration across hardware) is out of
# scope, so these are honest ballpark defaults, not measurements.
DEFAULT_YELLOW_EXP = 7.0
DEFAULT_RED_EXP = 9.0

# Relative tolerance for scale/shear classification. Unity scale within
# this factor is "applied enough"; float slop from matrix round-trips
# sits orders of magnitude below it.
SCALE_TOL = 1e-4

RISK_GREEN = 'GREEN'
RISK_YELLOW = 'YELLOW'
RISK_RED = 'RED'

CONF_EXACT = 'EXACT'
CONF_APPROXIMATE = 'APPROXIMATE'

# Scale-warning codes (stable identifiers; the UI maps them to text).
WARN_UNAPPLIED_SCALE = 'UNAPPLIED_SCALE'
WARN_NON_UNIFORM_SCALE = 'NON_UNIFORM_SCALE'
WARN_NEGATIVE_SCALE = 'NEGATIVE_SCALE'
WARN_SHEARED = 'SHEARED'


class EstimateError(ValueError):
    """Invalid estimator input (v <= 0, non-finite bounds/matrix)."""


class Estimate:
    """Frozen result of :func:`estimate`. Plain attributes, write-locked
    after construction (a dataclass would work too; this keeps the
    module import-light and the freeze explicit)."""

    __slots__ = (
        "source", "coordinate_space", "confidence",
        "bounds_min", "bounds_max", "dimensions", "voxel_size",
        "axis_cells", "longest_axis_cells", "bounding_cells", "saturated",
        "surface_cells", "risk", "scale_warnings",
        "world_axis_sizes", "effective_axis_scales",
        "_frozen",
    )

    def __init__(self, **kw):
        for name in self.__slots__:
            if name == "_frozen":
                continue
            object.__setattr__(self, name, kw.pop(name))
        if kw:
            raise TypeError("unexpected fields: %r" % sorted(kw))
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise AttributeError("Estimate is frozen")
        object.__setattr__(self, name, value)

    def __repr__(self):
        return ("Estimate(source=%r, confidence=%s, risk=%s, "
                "longest_axis_cells=%d, bounding_cells=%d%s, "
                "surface_cells=%.3g, warnings=%r)"
                % (self.source, self.confidence, self.risk,
                   self.longest_axis_cells, self.bounding_cells,
                   " SATURATED" if self.saturated else "",
                   self.surface_cells, self.scale_warnings))


# ---------------------------------------------------------------------------
# Matrix helpers (pure python; a 4x4 or 3x3 arrives as nested sequences)
# ---------------------------------------------------------------------------

def _linear_3x3(matrix):
    """The upper-left 3x3 block of a 3x3 or 4x4 nested-sequence matrix,
    as row-major tuples. Validates finiteness."""
    if matrix is None:
        return None
    rows = [list(r) for r in matrix]
    if len(rows) not in (3, 4) or any(len(r) != len(rows) for r in rows):
        raise EstimateError("matrix must be 3x3 or 4x4, got %dx%s"
                            % (len(rows), [len(r) for r in rows]))
    m = tuple(tuple(float(rows[i][j]) for j in range(3)) for i in range(3))
    for row in m:
        for x in row:
            if not math.isfinite(x):
                raise EstimateError("non-finite matrix element %r" % x)
    return m


def _det_3x3(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def _column_norms(m):
    """Lengths of the transformed basis vectors: |M @ e_i|. For a
    rotation*scale matrix these ARE the axis scales; under shear they
    understate nothing but stop matching the singular values."""
    return tuple(
        math.sqrt(sum(m[r][c] * m[r][c] for r in range(3)))
        for c in range(3))


def singular_values_3x3(matrix):
    """The three singular values of the matrix's 3x3 block, descending.

    Computed as sqrt(eigenvalues(M^T M)) with a pure-python cyclic
    Jacobi iteration — M^T M is symmetric positive semi-definite, for
    which Jacobi is unconditionally stable, and 3x3 converges in a
    handful of sweeps. No numpy: estimate.py stays runnable under bare
    python3 (see module doc)."""
    m = _linear_3x3(matrix)
    if m is None:
        raise EstimateError("matrix is required")
    # B = M^T M (symmetric PSD)
    b = [[sum(m[k][i] * m[k][j] for k in range(3)) for j in range(3)]
         for i in range(3)]
    # Cyclic Jacobi: zero the largest off-diagonal until negligible.
    for _sweep in range(64):
        # largest off-diagonal element
        p, q = max(((0, 1), (0, 2), (1, 2)),
                   key=lambda pq: abs(b[pq[0]][pq[1]]))
        apq = b[p][q]
        scale = max(abs(b[0][0]), abs(b[1][1]), abs(b[2][2]), 1e-300)
        if abs(apq) <= 1e-15 * scale:
            break
        app, aqq = b[p][p], b[q][q]
        theta = 0.5 * math.atan2(2.0 * apq, aqq - app)
        c, s = math.cos(theta), math.sin(theta)
        # Rotate rows/columns p and q of the symmetric matrix.
        for k in range(3):
            bkp, bkq = b[k][p], b[k][q]
            b[k][p] = c * bkp - s * bkq
            b[k][q] = s * bkp + c * bkq
        for k in range(3):
            bpk, bqk = b[p][k], b[q][k]
            b[p][k] = c * bpk - s * bqk
            b[q][k] = s * bpk + c * bqk
    eigs = sorted((max(b[i][i], 0.0) for i in range(3)), reverse=True)
    return tuple(math.sqrt(e) for e in eigs)


def analyze_scale(matrix):
    """(warnings, singular_values, column_norms) for a world matrix.

    Warning codes (order stable):
    - WARN_NEGATIVE_SCALE   — det < 0 (mirrored transform)
    - WARN_UNAPPLIED_SCALE  — any singular value differs from 1
    - WARN_NON_UNIFORM_SCALE — singular values differ from each other
    - WARN_SHEARED          — column norms disagree with the singular
      values (the three "scale" numbers lie; per the research doc the
      singular values are used for every derived size instead)
    """
    m = _linear_3x3(matrix)
    if m is None:
        return (), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0)
    sv = singular_values_3x3(m)
    norms = _column_norms(m)
    warnings = []
    if _det_3x3(m) < 0.0:
        warnings.append(WARN_NEGATIVE_SCALE)
    if any(abs(s - 1.0) > SCALE_TOL for s in sv):
        warnings.append(WARN_UNAPPLIED_SCALE)
    if sv[0] > 0.0 and (sv[0] - sv[2]) > SCALE_TOL * max(sv[0], 1.0):
        warnings.append(WARN_NON_UNIFORM_SCALE)
    # Shear: for M = R @ diag(s) (no shear), the column norms equal the
    # singular values up to ordering. Compare the sorted sets.
    sorted_norms = sorted(norms, reverse=True)
    if any(abs(a - b) > SCALE_TOL * max(a, b, 1.0)
           for a, b in zip(sv, sorted_norms)):
        warnings.append(WARN_SHEARED)
    return tuple(warnings), sv, norms


def world_target_to_object(world_size, matrix):
    """Convert a requested WORLD-space cell size to the object-space
    voxel size to write into the native property.

    Conservative policy from the research doc: divide by the LARGEST
    effective axis scale (largest singular value — correct under shear),
    so no transformed axis ends up coarser than the target. Axes scaled
    below the maximum come out finer (more expensive), never coarser.
    """
    if not (isinstance(world_size, (int, float))
            and math.isfinite(world_size)) or world_size <= 0.0:
        raise EstimateError(
            "world target size must be a finite value > 0, got %r"
            % (world_size,))
    if matrix is None:
        return float(world_size)
    sv = singular_values_3x3(matrix)
    if sv[0] <= 0.0:
        raise EstimateError(
            "degenerate transform (all axes scale to zero); cannot "
            "convert a world-space target")
    return float(world_size) / sv[0]


# ---------------------------------------------------------------------------
# Cell arithmetic
# ---------------------------------------------------------------------------

def _sat_ceil_div(d, v):
    """ceil(d / v) as a saturating non-negative int. d >= 0, v > 0."""
    cells = math.ceil(d / v)
    if not isinstance(cells, int):    # math.ceil(inf) never reaches here
        cells = int(cells)
    return min(max(cells, 0), SATURATION_CAP)


def _sat_mul(a, b):
    prod = a * b
    return prod if prod <= SATURATION_CAP else SATURATION_CAP


def risk_band(bounding_cells, yellow_exp=DEFAULT_YELLOW_EXP,
              red_exp=DEFAULT_RED_EXP):
    """GREEN/YELLOW/RED from log10(bounding_cells) against the two
    configurable exponents. yellow_exp > red_exp is a configuration
    error and is normalized (red wins)."""
    if bounding_cells <= 0:
        return RISK_GREEN
    yellow_exp = min(float(yellow_exp), float(red_exp))
    exp = math.log10(bounding_cells)
    if exp >= float(red_exp):
        return RISK_RED
    if exp >= yellow_exp:
        return RISK_YELLOW
    return RISK_GREEN


# ---------------------------------------------------------------------------
# The estimator
# ---------------------------------------------------------------------------

def estimate(bounds_min, bounds_max, voxel_size, *,
             surface_area=0.0, matrix_world=None,
             source="original mesh", exact=True,
             padding=DEFAULT_PADDING,
             yellow_exp=DEFAULT_YELLOW_EXP, red_exp=DEFAULT_RED_EXP):
    """Estimate voxel-remesh cost for OBJECT-space bounds + voxel size.

    bounds_min/bounds_max — object-space AABB corners (any per-axis
        ordering; normalized internally). Zero extents are valid (probed:
        a flat plane voxel-remeshes fine).
    voxel_size    — the NATIVE object-space value, verbatim. Must be > 0
        and finite; anything else raises EstimateError (matching the
        native operator, which raises on 0.0 — probed on 5.1.2).
    surface_area  — object-space mesh area for the relative
        ``surface_cells`` score (0 => score 0).
    matrix_world  — optional 4x4 (or 3x3) world transform as nested
        sequences; drives scale warnings and world-space axis sizes.
        None => identity (no warnings, world == object).
    source/exact  — geometry-source label and whether bounds/area are
        exact for the entry point being estimated (destructive operation
        vs Remesh modifier — see module doc). exact=False marks the
        result CONF_APPROXIMATE.
    """
    if not (isinstance(voxel_size, (int, float))
            and math.isfinite(voxel_size)) or voxel_size <= 0.0:
        raise EstimateError(
            "voxel size must be a finite value > 0, got %r"
            % (voxel_size,))
    v = float(voxel_size)

    bmin = tuple(float(x) for x in bounds_min)
    bmax = tuple(float(x) for x in bounds_max)
    if len(bmin) != 3 or len(bmax) != 3:
        raise EstimateError("bounds must be 3-vectors")
    for x in bmin + bmax:
        if not math.isfinite(x):
            raise EstimateError("non-finite bounds element %r" % x)
    lo = tuple(min(a, b) for a, b in zip(bmin, bmax))
    hi = tuple(max(a, b) for a, b in zip(bmin, bmax))
    dims = tuple(b - a for a, b in zip(lo, hi))

    if not (isinstance(surface_area, (int, float))
            and math.isfinite(surface_area)) or surface_area < 0.0:
        raise EstimateError(
            "surface area must be finite and >= 0, got %r"
            % (surface_area,))

    padding = max(int(padding), 0)
    raw_axis = tuple(_sat_ceil_div(d, v) for d in dims)
    axis_cells = tuple(min(n + padding, SATURATION_CAP) for n in raw_axis)
    longest_axis_cells = max(raw_axis)
    bounding = _sat_mul(_sat_mul(axis_cells[0], axis_cells[1]),
                        axis_cells[2])
    saturated = (bounding >= SATURATION_CAP
                 or any(n >= SATURATION_CAP for n in axis_cells))

    surface_cells = float(surface_area) / (v * v)

    warnings, sv, norms = analyze_scale(matrix_world)
    # Per-object-axis world cell sizes come from the column norms (the
    # length each object axis maps to in world space — labelable X/Y/Z
    # in the UI). The singular values are kept alongside as the
    # effective scales: under shear the two disagree (WARN_SHEARED
    # fires) and every CONVERSION uses the singular values instead.
    world_sizes = tuple(n * v for n in norms)

    return Estimate(
        source=str(source),
        coordinate_space='OBJECT',
        confidence=CONF_EXACT if exact else CONF_APPROXIMATE,
        bounds_min=lo,
        bounds_max=hi,
        dimensions=dims,
        voxel_size=v,
        axis_cells=axis_cells,
        longest_axis_cells=longest_axis_cells,
        bounding_cells=bounding,
        saturated=saturated,
        surface_cells=surface_cells,
        risk=risk_band(bounding, yellow_exp, red_exp),
        scale_warnings=warnings,
        world_axis_sizes=world_sizes,
        effective_axis_scales=sv,
    )
