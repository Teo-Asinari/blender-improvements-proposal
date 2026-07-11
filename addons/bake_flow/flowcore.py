# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure logic for the Bake Flow add-on: extrusion/ray auto-heuristic,
output path and image naming, UV-layout degeneracy math, scale checks
and the decimate fallback ratio.

Imports neither ``bpy`` nor ``gpu`` (numpy only), following the sibling
add-ons' purity convention, so everything here is unit-testable without
touching Blender state.
"""

import os

import numpy as np


# ---------------------------------------------------------------------------
# Bake type table
#
# The single switch point for everything type-specific: image name
# suffix, colorspace, and (in baking.py) the per-type operator settings.
# ---------------------------------------------------------------------------

BAKE_TYPES = {
    'NORMAL': {
        "label": "Normal",
        "suffix": "normal",
        "non_color": True,   # tangent-space normal data, never sRGB
    },
    # TODO: AO, cavity/curvature, displacement — same machinery: add an
    # entry here (suffix + colorspace), a matching EnumProperty item in
    # __init__.BakeFlowSettings.bake_type, and a per-type settings
    # branch in baking._bake_kwargs. Image naming, node creation and
    # saving already switch on this table.
}


# ---------------------------------------------------------------------------
# Extrusion / max-ray-distance auto-heuristic
#
# Both distances scale with the PAIR's combined world-space bounding-box
# diagonal, so the defaults adapt to any model size without the user
# touching a field:
#
#   extrusion        = 2% of the diagonal
#   max ray distance = 4% of the diagonal (2x the extrusion)
#
# Rationale: the cage must be inflated past the largest high/low surface
# deviation, which for a sane retopo is a small fraction of the model
# size — 2% comfortably covers typical QuadriFlow/manual retopo error
# without ballooning the cage into self-intersection on concave areas.
# Rays then need to travel from the inflated cage back through the low
# surface to the high surface, i.e. up to roughly twice the extrusion.
# Verified empirically on 5.1.2 (displaced-sphere pair in the test
# suite): these factors capture full detail with no misses. Both values
# are exposed for override (Auto Distances off) for extreme cases —
# very thin shells (lower them) or badly matched pairs (raise them).
# ---------------------------------------------------------------------------

EXTRUSION_FACTOR = 0.02
MAX_RAY_FACTOR = 0.04


def auto_distances(diagonal):
    """(cage_extrusion, max_ray_distance) from a bounding-box diagonal."""
    d = max(float(diagonal), 0.0)
    return (EXTRUSION_FACTOR * d, MAX_RAY_FACTOR * d)


def combined_diagonal(points):
    """Diagonal length of the axis-aligned box enclosing ``points``
    (an iterable of (x, y, z) tuples — e.g. both objects' world-space
    bound_box corners). 0.0 for fewer than 2 points."""
    pts = np.asarray(list(points), dtype=np.float64)
    if pts.shape[0] < 2:
        return 0.0
    return float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))


# ---------------------------------------------------------------------------
# Naming and output path
# ---------------------------------------------------------------------------

def image_name(low_name, bake_type):
    """Datablock name for the bake image: ``<lowpoly>_<suffix>``."""
    return "%s_%s" % (low_name, BAKE_TYPES[bake_type]["suffix"])


def default_relpath(low_name, bake_type):
    """Default output location relative to the .blend:
    ``textures/<lowpoly>_<suffix>.png``."""
    return os.path.join("textures", image_name(low_name, bake_type) + ".png")


def resolve_output_path(prop_value, blend_dir, low_name, bake_type):
    """Resolve the Output Path property to an absolute file path.

    Returns (path, None) on success or (None, reason) with an
    actionable message. ``blend_dir`` is the directory of the saved
    .blend ("" when unsaved). Rules:

    - empty property -> ``<blend_dir>/textures/<low>_<suffix>.png``;
      needs a saved .blend (probed on 5.1.2: ``bpy.path.abspath("//x")``
      in an UNSAVED file silently resolves relative to the process CWD,
      so we refuse instead of writing somewhere surprising).
    - ``//``-relative -> resolved against the .blend directory.
    - absolute -> used as-is.
    - trailing slash (any of the above) -> the default file name is
      appended inside that directory.
    """
    pv = (prop_value or "").strip()
    fname = image_name(low_name, bake_type) + ".png"

    if not pv:
        if not blend_dir:
            return None, ("No output path and the .blend is unsaved - "
                          "save the file (bakes then default to "
                          "//textures/) or set an absolute Output Path")
        return os.path.normpath(
            os.path.join(blend_dir, "textures", fname)), None

    is_dir = pv.endswith("/") or pv.endswith("\\")
    if pv.startswith("//"):
        if not blend_dir:
            return None, ("Output path is .blend-relative (//) but the "
                          "file is unsaved - save it or use an absolute "
                          "path")
        pv = os.path.join(blend_dir, pv[2:])
    elif not os.path.isabs(pv):
        if not blend_dir:
            return None, ("Output path is relative but the .blend is "
                          "unsaved - save it or use an absolute path")
        pv = os.path.join(blend_dir, pv)
    if is_dir:
        pv = os.path.join(pv, fname)
    return os.path.normpath(pv), None


# ---------------------------------------------------------------------------
# UV layout degeneracy
# ---------------------------------------------------------------------------

# Total UV area below this is considered degenerate (a real layout that
# fills any usable fraction of the 0..1 tile is orders of magnitude
# above; an untouched default/all-zero layout is exactly 0).
DEGENERATE_UV_AREA = 1e-6
DEGENERATE_UV_EXTENT = 1e-6


def uv_total_area(uvs, tris):
    """Total unsigned UV-space area. ``uvs``: (L, 2) float array of
    loop UVs; ``tris``: (T, 3) int array of loop indices per triangle."""
    uvs = np.asarray(uvs, dtype=np.float64).reshape(-1, 2)
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if len(tris) == 0:
        return 0.0
    a = uvs[tris[:, 0]]
    b = uvs[tris[:, 1]]
    c = uvs[tris[:, 2]]
    cross = ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
             - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))
    return float(np.abs(cross).sum() * 0.5)


def uv_extent(uvs):
    """(width, height) of the UV bounding box."""
    uvs = np.asarray(uvs, dtype=np.float64).reshape(-1, 2)
    if len(uvs) == 0:
        return (0.0, 0.0)
    ext = uvs.max(axis=0) - uvs.min(axis=0)
    return (float(ext[0]), float(ext[1]))


def uv_layout_degenerate(uvs, tris):
    """True when the layout cannot receive a bake: all loops collapsed
    (zero extent on either axis) or (near-)zero total chart area —
    e.g. an all-zero default layer or a fully overlapped point pile."""
    w, h = uv_extent(uvs)
    if w < DEGENERATE_UV_EXTENT or h < DEGENERATE_UV_EXTENT:
        return True
    return uv_total_area(uvs, tris) < DEGENERATE_UV_AREA


# ---------------------------------------------------------------------------
# Object transform checks
# ---------------------------------------------------------------------------

SCALE_EPS = 1e-4


def scale_applied(scale, eps=SCALE_EPS):
    """True when all three scale components are within eps of 1.0."""
    return all(abs(float(s) - 1.0) <= eps for s in scale)


def scale_negative(scale):
    """True when any scale component is negative (mirrored transform —
    flips effective face winding/normals and ruins tangent-space
    bakes)."""
    return any(float(s) < 0.0 for s in scale)


# ---------------------------------------------------------------------------
# Decimate fallback ratio
# ---------------------------------------------------------------------------

def decimate_ratio(current_faces, target_faces):
    """Collapse ratio approximating ``target_faces`` from
    ``current_faces``; clamped to (0, 1]."""
    if current_faces <= 0:
        return 1.0
    return min(1.0, max(float(target_faces) / float(current_faces), 1e-4))
