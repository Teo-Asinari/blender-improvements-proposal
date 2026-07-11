# SPDX-License-Identifier: GPL-2.0-or-later
"""Stage 1: "Create Low-Poly Candidate" — duplicate the high-poly's
visible (modifier-evaluated) surface and remesh it down to a target
face count.

Primary path: QuadriFlow (``bpy.ops.object.quadriflow_remesh``, probed
on 5.1.2: mode='FACES' + target_faces; runs headless and returns
FINISHED). Fallback when QuadriFlow is unavailable or fails (it can
choke on non-manifold input): a Decimate modifier with
ratio = target / current, applied. The caller is told which path ran.

The result is a STARTING POINT for retopo, not animation-grade
topology: QuadriFlow gives an even, seam-free quad grid with no regard
for edge flow, and Decimate gives ugly-but-faithful triangles. Both
drop UVs — stage 2 (seams + unwrap) always follows.
"""

import bpy

from . import flowcore


class RetopoError(Exception):
    """Actionable failure (message is shown via self.report)."""


def _quadriflow_available():
    """Thin probe so tests can simulate QuadriFlow absence by
    monkeypatching this single seam.

    NOTE (probed on 5.1.2): C-defined operators like quadriflow_remesh
    do NOT appear as bpy.types.OBJECT_OT_* attributes (only
    Python-registered operators do — which is why the sibling add-on
    probe in __init__ can use bpy.types), and bpy.ops attribute access
    lazily succeeds for ANY name. get_rna_type() is the check that
    actually resolves the operator."""
    try:
        bpy.ops.object.quadriflow_remesh.get_rna_type()
        return True
    except Exception:
        return False


def _run_quadriflow(target_faces):
    """Thin wrapper so tests can simulate QuadriFlow failure by
    monkeypatching this single seam."""
    return bpy.ops.object.quadriflow_remesh(
        mode='FACES',
        target_faces=int(target_faces),
        use_mesh_symmetry=False,
    )


def _evaluated_mesh_copy(context, high):
    """New mesh datablock from the high-poly's evaluated geometry
    (modifiers applied — a Multires/Subdiv sculpt remeshes at its
    visible detail, not its base cage). Falls back to a plain data
    copy if the evaluated route fails."""
    try:
        deps = context.evaluated_depsgraph_get()
        me = bpy.data.meshes.new_from_object(
            high.evaluated_get(deps), depsgraph=deps)
        if len(me.polygons) > 0:
            return me
        bpy.data.meshes.remove(me)
    except Exception:
        pass
    return high.data.copy()


def create_lowpoly_candidate(context, high, target_faces):
    """Returns (low_object, method, detail); raises RetopoError.

    method is 'QUADRIFLOW' or 'DECIMATE'; detail says why the fallback
    ran (empty for the primary path). The new object is linked to the
    active collection, named ``<high>_low``, selected and active.
    """
    if high is None or high.type != 'MESH':
        raise RetopoError("Pick a high-poly mesh object first")
    if len(high.data.polygons) == 0:
        raise RetopoError("High-poly mesh has no faces")

    me = _evaluated_mesh_copy(context, high)
    me.name = high.name + "_low"
    dup = bpy.data.objects.new(high.name + "_low", me)
    dup.matrix_world = high.matrix_world.copy()
    context.collection.objects.link(dup)

    # Remesh operators need Object Mode and the dup exclusively active.
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in context.selected_objects:
        ob.select_set(False)
    dup.select_set(True)
    context.view_layer.objects.active = dup

    fallback_reason = ""
    if _quadriflow_available():
        try:
            res = _run_quadriflow(target_faces)
            if 'FINISHED' in res and len(dup.data.polygons) > 0:
                return dup, 'QUADRIFLOW', ""
            fallback_reason = "QuadriFlow was cancelled"
        except RuntimeError as exc:
            # Raised in --background / on non-manifold input.
            fallback_reason = ("QuadriFlow failed: %s"
                               % str(exc).strip().splitlines()[-1])
    else:
        fallback_reason = "QuadriFlow operator not available"

    # --- Decimate fallback -------------------------------------------------
    try:
        mod = dup.modifiers.new("BakeFlowDecimate", 'DECIMATE')
        mod.decimate_type = 'COLLAPSE'
        # Collapse decimation counts in triangles, so derive the ratio
        # from the triangulated face count — a quad input would
        # otherwise land ~2x over target.
        dup.data.calc_loop_triangles()
        mod.ratio = flowcore.decimate_ratio(
            len(dup.data.loop_triangles), target_faces)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    except RuntimeError as exc:
        raise RetopoError(
            "%s; Decimate fallback also failed: %s"
            % (fallback_reason, str(exc).strip().splitlines()[-1]))
    if len(dup.data.polygons) == 0:
        raise RetopoError(
            "%s; Decimate fallback produced an empty mesh"
            % fallback_reason)
    return dup, 'DECIMATE', fallback_reason
