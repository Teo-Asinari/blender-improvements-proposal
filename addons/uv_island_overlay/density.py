# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure texel-density math for the DENSITY display mode.

Convention
----------
Texel density here is the LINEAR ratio

    density = sqrt(UV area / 3D area)

i.e. UV units per world unit — the square root turns the area ratio into
a length ratio, so an island whose UVs are scaled 2x reports exactly 2x
the density. Multiplied by an assumed square texture edge length
(DEFAULT_TEXTURE_SIZE px) it becomes the familiar "pixels per world
unit" figure: density 0.5 on a 1024 px texture is 512 px/unit.

Aggregation is per island over the SUMMED areas (not a mean of per-face
ratios), so large faces weigh in proportionally to the surface they
cover. Degenerate faces — zero UV area or zero 3D area — are excluded
from both sums; an island with no valid face at all has an undefined
(NaN) density and is excluded from the mesh statistics.

No bpy/gpu imports — everything operates on plain numpy arrays handed in
by the caller, so this module is importable and fully testable in
``blender --background``.
"""

import numpy as np

# Assumed square texture edge (px) used to express density as px/unit.
DEFAULT_TEXTURE_SIZE = 1024

# The deviation tint saturates at +/- this many octaves (log2 factors)
# from the median density.
TINT_CLAMP_OCTAVES = 2.0

# Areas at or below this are treated as zero (degenerate triangles,
# excluded from the density statistics).
AREA_EPSILON = 1e-12

# Tint endpoints (RGB). Neutral multiplies the checker by 1 (no change);
# below-median islands drift toward blue, above-median toward red,
# reaching the endpoint at TINT_CLAMP_OCTAVES.
TINT_NEUTRAL = (1.0, 1.0, 1.0)
TINT_BELOW = (0.45, 0.62, 1.0)
TINT_ABOVE = (1.0, 0.5, 0.42)


def triangle_areas_3d(tri_co):
    """Areas of 3D triangles given as an (n, 3, 3) array of corner
    positions (half the cross-product magnitude). float64 math
    regardless of the input dtype."""
    tri_co = np.asarray(tri_co, dtype=np.float64)
    e1 = tri_co[:, 1] - tri_co[:, 0]
    e2 = tri_co[:, 2] - tri_co[:, 0]
    return 0.5 * np.linalg.norm(np.cross(e1, e2), axis=1)


def triangle_areas_uv(tri_uv):
    """Areas of UV-space triangles given as an (n, 3, 2) array of corner
    UVs. Absolute value — flipped UV winding does not produce negative
    area."""
    tri_uv = np.asarray(tri_uv, dtype=np.float64)
    e1 = tri_uv[:, 1] - tri_uv[:, 0]
    e2 = tri_uv[:, 2] - tri_uv[:, 0]
    return 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])


def island_densities(tri_island, area_uv, area_3d, n_islands,
                     epsilon=AREA_EPSILON):
    """Per-island texel density: sqrt(sum(UV area) / sum(3D area)).

    tri_island: (n,) island id per triangle; area_uv / area_3d: (n,)
    per-triangle areas. Degenerate triangles (either area <= epsilon)
    are excluded from BOTH sums; islands left with no valid triangle get
    NaN — density undefined there. Returns a float64 array of length
    n_islands.
    """
    n_islands = int(n_islands)
    densities = np.full(n_islands, np.nan)
    if n_islands == 0:
        return densities
    tri_island = np.asarray(tri_island, dtype=np.int64)
    area_uv = np.asarray(area_uv, dtype=np.float64)
    area_3d = np.asarray(area_3d, dtype=np.float64)
    if tri_island.size == 0:
        return densities
    valid = (area_uv > epsilon) & (area_3d > epsilon)
    uv_sum = np.bincount(tri_island[valid], weights=area_uv[valid],
                         minlength=n_islands)
    a3_sum = np.bincount(tri_island[valid], weights=area_3d[valid],
                         minlength=n_islands)
    ok = (uv_sum > 0.0) & (a3_sum > 0.0)
    densities[ok] = np.sqrt(uv_sum[ok] / a3_sum[ok])
    return densities


def median_density(densities):
    """Median over the defined (non-NaN) island densities, or None when
    no island has a defined density."""
    densities = np.asarray(densities, dtype=np.float64)
    valid = densities[~np.isnan(densities)]
    if valid.size == 0:
        return None
    return float(np.median(valid))


def density_px_per_unit(density, texture_size=DEFAULT_TEXTURE_SIZE):
    """Express a unitless density (UV units per world unit) as pixels
    per world unit for a square texture of the given edge length."""
    return density * float(texture_size)


def deviation_octaves(densities, median, clamp_octaves=TINT_CLAMP_OCTAVES):
    """Signed log2 deviation of each island density from the median,
    clamped to +/- clamp_octaves. NaN densities (or a missing /
    non-positive median) yield NaN."""
    densities = np.asarray(densities, dtype=np.float64)
    out = np.full(densities.shape, np.nan)
    if median is None or median <= 0.0:
        return out
    positive = ~np.isnan(densities) & (densities > 0.0)
    out[positive] = np.clip(np.log2(densities[positive] / median),
                            -clamp_octaves, clamp_octaves)
    # Defensive: a defined-but-zero density counts as maximally below.
    zero = ~np.isnan(densities) & (densities <= 0.0)
    out[zero] = -clamp_octaves
    return out


def deviation_tints(densities, median, alpha=1.0,
                    clamp_octaves=TINT_CLAMP_OCTAVES):
    """Per-island RGBA tint by log2 deviation from the median density:
    neutral at the median, drifting toward TINT_BELOW under it and
    TINT_ABOVE over it, saturating at +/- clamp_octaves. Islands with
    undefined (NaN) density — and every island when there is no median —
    get the neutral tint. Returns an (n, 4) float64 array with alpha in
    the fourth channel."""
    densities = np.asarray(densities, dtype=np.float64)
    n = densities.shape[0]
    tints = np.empty((n, 4))
    tints[:, :3] = TINT_NEUTRAL
    tints[:, 3] = alpha
    dev = deviation_octaves(densities, median, clamp_octaves)
    have = ~np.isnan(dev)
    if not have.any():
        return tints
    t = dev[have] / float(clamp_octaves)          # in [-1, 1]
    neutral = np.asarray(TINT_NEUTRAL)
    target = np.where((t < 0.0)[:, None],
                      np.asarray(TINT_BELOW),
                      np.asarray(TINT_ABOVE))
    tints[have, :3] = neutral + np.abs(t)[:, None] * (target - neutral)
    return tints
