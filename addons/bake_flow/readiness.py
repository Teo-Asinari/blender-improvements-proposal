# SPDX-License-Identifier: GPL-2.0-or-later
"""Stage 2 readiness checklist for the low-poly mesh (bpy glue around
flowcore's pure checks).

The checklist answers "can this low-poly receive a normal bake":

- has a UV layer                          (FAIL blocks baking)
- UVs are non-degenerate                  (FAIL blocks baking)
- object scale is applied (|s - 1| < eps) (WARN: non-uniform scale
  distorts the cage and the tangent basis)
- no negative scale component             (WARN: mirrored transforms
  flip effective normals — the panel offers Recalculate Outside /
  Apply Scale as fixes)

Deliberately pragmatic on flipped normals: a full winding-consistency
analysis is overkill for a checklist row, so only the cheap, reliable
negative-scale case is detected (matching the sibling overlay add-on's
back-face-culling diagnostic, which shows flipped faces visually).
"""

import time
from collections import namedtuple

import numpy as np
from mathutils import Vector

from . import flowcore

# States, ordered by severity.
OK = 'OK'
WARN = 'WARN'
FAIL = 'FAIL'

CheckItem = namedtuple("CheckItem", "key label state detail")

# Panel-draw cache: readiness evaluation reads full UV arrays, which on
# a large mesh is milliseconds — too slow to run on every viewport
# redraw. Entries live CACHE_TTL_S seconds, keyed by object name; the
# fix operators call invalidate() so their effect shows immediately.
CACHE_TTL_S = 0.8
_cache = {}


def invalidate():
    _cache.clear()


def pair_diagonal(high, low):
    """Combined world-space bounding-box diagonal of the pair (drives
    the extrusion auto-heuristic; see flowcore.auto_distances)."""
    pts = []
    for ob in (high, low):
        if ob is None:
            continue
        mw = ob.matrix_world
        for corner in ob.bound_box:
            v = mw @ Vector(corner)
            pts.append((v.x, v.y, v.z))
    return flowcore.combined_diagonal(pts)


def _gather_uv_arrays(obj):
    """(uvs (N,2), tris (T,3) row indices into uvs) for the object's
    active UV layer, or (None, None) when it has no UV layer.

    In Edit Mode the Mesh ``uv_layers`` data arrays are EMPTY while the
    edit bmesh owns the data — probed on 5.1.2 (update_from_editmode()
    does not repopulate them either), matching the sibling overlay's
    finding — so a bmesh loop-triangle walk is used there instead."""
    if obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        layer = bm.loops.layers.uv.active
        if layer is None:
            return None, None
        tri_loops = bm.calc_loop_triangles()
        uvs = np.array(
            [tuple(loop[layer].uv) for tri in tri_loops for loop in tri],
            dtype=np.float64).reshape(-1, 2)
        tris = np.arange(len(tri_loops) * 3,
                         dtype=np.int64).reshape(-1, 3)
        return uvs, tris
    me = obj.data
    uvl = me.uv_layers.active
    if uvl is None:
        return None, None
    n_loops = len(me.loops)
    uvs = np.empty(n_loops * 2, dtype=np.float32)
    uvl.data.foreach_get("uv", uvs)
    me.calc_loop_triangles()
    tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("loops", tris)
    return uvs.reshape(-1, 2), tris.reshape(-1, 3)


def evaluate(low):
    """Fresh checklist for the low-poly object -> [CheckItem, ...]."""
    items = []

    uvs, tris = _gather_uv_arrays(low)
    if uvs is None:
        items.append(CheckItem(
            "uv_layer", "UV layer", FAIL,
            "No UV layer - mark seams and unwrap (or Smart UV Project)"))
        items.append(CheckItem(
            "uv_valid", "UVs non-degenerate", FAIL,
            "No UVs to validate"))
    else:
        items.append(CheckItem("uv_layer", "UV layer", OK, ""))
        if flowcore.uv_layout_degenerate(uvs, tris):
            items.append(CheckItem(
                "uv_valid", "UVs non-degenerate", FAIL,
                "UV layout has (near-)zero area - re-unwrap; an "
                "all-zero/collapsed layout cannot receive a bake"))
        else:
            items.append(CheckItem("uv_valid", "UVs non-degenerate",
                                   OK, ""))

    if flowcore.scale_applied(low.scale):
        items.append(CheckItem("scale", "Scale applied", OK, ""))
    else:
        items.append(CheckItem(
            "scale", "Scale applied", WARN,
            "Object scale is %s - non-uniform scale distorts the bake "
            "cage and tangent basis (use Apply Scale)"
            % (tuple(round(s, 3) for s in low.scale),)))

    if flowcore.scale_negative(low.scale):
        items.append(CheckItem(
            "normals", "No mirrored transform", WARN,
            "Negative scale flips effective normals - apply scale, "
            "then Recalculate Outside"))
    else:
        items.append(CheckItem("normals", "No mirrored transform",
                               OK, ""))
    return items


def evaluate_cached(low, now=None):
    """TTL-cached evaluate() for panel drawing."""
    now = time.monotonic() if now is None else now
    key = low.name
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    items = evaluate(low)
    _cache[key] = (now, items)
    return items


def blocking(items):
    """The FAIL items (these block the bake operator)."""
    return [i for i in items if i.state == FAIL]


def warnings(items):
    return [i for i in items if i.state == WARN]
