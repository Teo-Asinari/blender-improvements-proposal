# SPDX-License-Identifier: GPL-2.0-or-later
"""Calipers bpy-side core: geometry statistics, the stats cache, and
entry-point resolution for the two voxel-remesh paths.

Terminology discipline (research/scale-aware-remesh-safety.md): the
destructive Voxel Remesh OPERATION and the Remesh MODIFIER are separate
entry points with different geometry sources, and this module keeps them
separate end to end:

- CONTEXT_MESH — the destructive ``bpy.ops.object.voxel_remesh``.
  Probed on 5.1.2: the operation uses ORIGINAL mesh data (a cube+subsurf
  remeshes to the same count as the bare cube; shape keys are ignored
  and destroyed), so ``obj.data`` vertex coordinates are the EXACT
  geometry source.
- CONTEXT_MODIFIER — the Remesh modifier in VOXEL mode. Its input is
  whatever the stack produced BEFORE it, which Python cannot read
  mid-stack. Confidence rules (each case probed):
  * modifier FIRST in the stack     -> input == base mesh   -> EXACT
  * modifier LAST and show_viewport
    False (the safe-add pending
    state)                          -> input == evaluated_get() output
                                       (probed: 98 subsurf verts with a
                                       disabled trailing remesh) -> EXACT
  * anything else                   -> base-mesh stats stand in
                                       -> APPROXIMATE, and the UI says so.

Cache discipline: panel draw code calls ONLY :func:`cached_stats` /
:func:`current_estimate` (dict lookup + pure arithmetic). Stats
extraction — vertex/area scans and any depsgraph read — happens in
operators and the debounced timer, never in a draw callback.

Defaults are read from RNA at runtime (:func:`rna_default`), never
hardcoded. Probed caveat for anyone extending this: the RNA default of
``Modifier.show_viewport`` is False on 5.1.2 while ``modifiers.new()``
actually creates modifiers with True — RNA defaults and factory behavior
can disagree, so probe before trusting a default for behavior.
"""

import time
from collections import namedtuple

import bpy
import numpy as np

from . import estimate
from . import live

# The two entry points (see module doc).
CONTEXT_MESH = 'MESH'
CONTEXT_MODIFIER = 'MODIFIER'

# Debounce timing for the live estimate refresh: a burst of edits costs
# one stats recompute, after the burst.
QUIET_S = 0.30
POLL_S = 0.15

# Geometry statistics for one entry point, in OBJECT space.
GeomStats = namedtuple(
    "GeomStats",
    "source exact bounds_min bounds_max surface_area vert_count "
    "poly_count")

_ZERO3 = (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# RNA introspection (defaults at runtime, native-operator existence)
# ---------------------------------------------------------------------------

def rna_default(struct, prop_name):
    """The RNA default of ``struct.bl_rna.properties[prop_name]``."""
    return struct.bl_rna.properties[prop_name].default


def mesh_voxel_size_default():
    return rna_default(bpy.types.Mesh, "remesh_voxel_size")


def modifier_voxel_size_default():
    return rna_default(bpy.types.RemeshModifier, "voxel_size")


def voxel_remesh_op_exists():
    """True when the native destructive operator exists.

    Probed on 5.1.2: hasattr(bpy.types, 'OBJECT_OT_voxel_remesh') is
    FALSE even though the operator runs — native (C) operators are not
    exposed as bpy.types attributes, so the sibling add-ons' bpy.types
    probe does not work here. get_rna_type() raises KeyError for
    non-existent ops and is the reliable signal. (hasattr(bpy.ops.X, y)
    is always True and useless, as the siblings note.)
    """
    try:
        bpy.ops.object.voxel_remesh.get_rna_type()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stats extraction (operators / timer only — never draw)
# ---------------------------------------------------------------------------

def mesh_stats(me, source, exact):
    """Object-space GeomStats for a Mesh datablock. Vectorized
    foreach_get reads (the sibling add-ons' probed fast path); an empty
    mesh yields zero bounds and area — a valid estimator input, not an
    error (probed: the native op CANCELs gracefully on empty meshes and
    happily remeshes zero-extent flat geometry)."""
    n = len(me.vertices)
    if n == 0:
        return GeomStats(source, exact, _ZERO3, _ZERO3, 0.0, 0, 0)
    coords = np.empty(n * 3, dtype=np.float32)
    me.vertices.foreach_get("co", coords)
    coords = coords.reshape(n, 3)
    bmin = tuple(float(x) for x in coords.min(axis=0))
    bmax = tuple(float(x) for x in coords.max(axis=0))
    npoly = len(me.polygons)
    if npoly:
        areas = np.empty(npoly, dtype=np.float32)
        me.polygons.foreach_get("area", areas)
        area = float(areas.sum())
    else:
        area = 0.0
    return GeomStats(source, exact, bmin, bmax, area, n, npoly)


def evaluated_stats(obj, depsgraph, source, exact):
    """GeomStats of the object's fully evaluated geometry (object
    space). Only meaningful for the modifier context when the Remesh
    modifier itself is NOT part of the evaluation (disabled trailing
    modifier — see module doc)."""
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        return mesh_stats(me, source, exact)
    finally:
        ev.to_mesh_clear()


# ---------------------------------------------------------------------------
# Entry-point resolution
# ---------------------------------------------------------------------------

def find_voxel_modifier(obj):
    """The Remesh modifier in VOXEL mode this panel/overlay should talk
    about: the active modifier when it qualifies, else the first
    qualifying one in the stack, else None."""
    if obj is None or obj.type != 'MESH':
        return None
    active = getattr(obj.modifiers, "active", None)
    if (active is not None and active.type == 'REMESH'
            and active.mode == 'VOXEL'):
        return active
    for mod in obj.modifiers:
        if mod.type == 'REMESH' and mod.mode == 'VOXEL':
            return mod
    return None


def modifier_input_source(obj, mod):
    """(exact, source_label, needs_depsgraph) describing the geometry
    ENTERING the modifier — the confidence rules in the module doc."""
    idx = obj.modifiers.find(mod.name)
    last = (idx == len(obj.modifiers) - 1)
    if idx == 0:
        return True, "base mesh (modifier is first in stack)", False
    if last and not mod.show_viewport:
        return True, "evaluated stack entering the modifier", True
    return (False,
            "base mesh (approximate: %d preceding modifier(s) not "
            "inspected)" % max(idx, 0),
            False)


def bounds_derived_voxel_size(bounds_min, bounds_max, cells):
    """A safe mesh-derived initial voxel size: the longest bounding-box
    axis divided into ``cells`` cells. Degenerate bounds (empty mesh)
    fall back to the RNA default rather than 0.0 — probed: a modifier
    with voxel_size 0.0 logs 'Zero voxel size cannot be solved'."""
    longest = max(b - a for a, b in zip(bounds_min, bounds_max))
    if not longest > 0.0:
        return float(modifier_voxel_size_default())
    return longest / max(int(cells), 1)


# ---------------------------------------------------------------------------
# Stats cache + debounced refresh plumbing
# ---------------------------------------------------------------------------

_stats_cache = {}                  # (object_name, context_kind) -> GeomStats
_debounce = live.Debounce(QUIET_S)


def invalidate(obj_name=None):
    """Drop cached stats (for one object, or all)."""
    if obj_name is None:
        _stats_cache.clear()
        return
    for kind in (CONTEXT_MESH, CONTEXT_MODIFIER):
        _stats_cache.pop((obj_name, kind), None)


def cached_stats(obj_name, kind):
    """Draw-safe cache lookup; None when nothing is cached yet."""
    return _stats_cache.get((obj_name, kind))


def refresh_stats(obj, depsgraph=None):
    """Recompute stats for both entry points of ``obj`` right now.
    Operator/timer territory (vertex scans, optional depsgraph read).
    Returns True when something was cached."""
    if obj is None or obj.type != 'MESH':
        return False
    _stats_cache[(obj.name, CONTEXT_MESH)] = mesh_stats(
        obj.data, "original mesh datablock", True)
    mod = find_voxel_modifier(obj)
    if mod is None:
        _stats_cache.pop((obj.name, CONTEXT_MODIFIER), None)
    else:
        exact, label, needs_deps = modifier_input_source(obj, mod)
        if needs_deps and depsgraph is not None:
            st = evaluated_stats(obj, depsgraph, label, exact)
        elif needs_deps:
            # No depsgraph offered (headless caller): base mesh stands
            # in and the confidence honestly degrades.
            st = mesh_stats(obj.data,
                            "base mesh (approximate: no depsgraph)",
                            False)
        else:
            st = mesh_stats(obj.data, label, exact)
        _stats_cache[(obj.name, CONTEXT_MODIFIER)] = st
    return True


def note_activity(now=None):
    """Depsgraph handler hook: O(1) timestamp note."""
    _debounce.note_change(time.monotonic() if now is None else now)


def refresh_for(context):
    """Refresh stats for the context's active object. Fetches the
    depsgraph ONLY when the modifier context needs it for exact input
    stats — and in that state the remesh modifier itself is disabled,
    so the read is never the expensive event."""
    obj = getattr(context, "active_object", None)
    if obj is None or obj.type != 'MESH':
        return False
    depsgraph = None
    mod = find_voxel_modifier(obj)
    if mod is not None and modifier_input_source(obj, mod)[2]:
        try:
            depsgraph = context.evaluated_depsgraph_get()
        except Exception:
            depsgraph = None
    return refresh_stats(obj, depsgraph)


def poll_debounce(context, now=None):
    """Timer hook: when a quiet period has elapsed since the last
    activity, refresh stats for the active object. Returns True when a
    refresh ran (caller tags a redraw)."""
    if not _debounce.try_fire(time.monotonic() if now is None else now):
        return False
    return refresh_for(context)


def reset_debounce():
    _debounce.reset()


# ---------------------------------------------------------------------------
# Draw-safe estimates (pure arithmetic over the cache)
# ---------------------------------------------------------------------------

def current_estimate(obj, kind, settings=None):
    """The Estimate for one entry point of ``obj``, or None when no
    stats are cached / no modifier exists / the voxel size is invalid.

    Panel-draw safe: a cache lookup, RNA property reads, and pure
    estimator arithmetic (the 3x3 Jacobi is microseconds). NEVER
    evaluates the depsgraph or scans geometry.
    """
    if obj is None or obj.type != 'MESH':
        return None
    st = _stats_cache.get((obj.name, kind))
    if st is None:
        return None
    if kind == CONTEXT_MESH:
        v = obj.data.remesh_voxel_size
    else:
        mod = find_voxel_modifier(obj)
        if mod is None:
            return None
        v = mod.voxel_size
    kwargs = {}
    if settings is not None:
        kwargs["yellow_exp"] = settings.yellow_exp
        kwargs["red_exp"] = settings.red_exp
    try:
        return estimate.estimate(
            st.bounds_min, st.bounds_max, v,
            surface_area=st.surface_area,
            matrix_world=[list(row) for row in obj.matrix_world],
            source=st.source, exact=st.exact, **kwargs)
    except estimate.EstimateError:
        # v <= 0 (assignable on 5.1.2: hard_min is 0.0) — the panel
        # shows its own "invalid voxel size" row for this.
        return None


def resolve_guide_context(obj, prefer='AUTO'):
    """Which entry point the viewport guide follows: 'AUTO' prefers an
    existing VOXEL Remesh modifier and falls back to the mesh
    (destructive-operation) settings."""
    if prefer == CONTEXT_MESH:
        return CONTEXT_MESH
    if prefer == CONTEXT_MODIFIER:
        return (CONTEXT_MODIFIER if find_voxel_modifier(obj) is not None
                else None)
    return (CONTEXT_MODIFIER if find_voxel_modifier(obj) is not None
            else CONTEXT_MESH)
