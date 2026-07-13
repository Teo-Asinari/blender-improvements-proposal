# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for core.py (run inside
``blender --background --python``): stats extraction, entry-point
resolution and confidence rules, RNA default reads, the stats cache,
and the debounce plumbing.

Prints CALIPERS_CORE_TESTS_PASSED on success.
"""

import os
import sys
import traceback

import bpy
import bmesh

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from calipers import core, estimate, live

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def new_cube(name, size=2.0):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=size)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    return ob


def make_active(ob):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    if ob is not None:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = ob


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # --- RNA defaults read at runtime, never hardcoded ---------------------
    check("mesh voxel default == RNA read",
          core.mesh_voxel_size_default()
          == bpy.types.Mesh.bl_rna.properties["remesh_voxel_size"].default)
    check("modifier voxel default == RNA read",
          core.modifier_voxel_size_default()
          == bpy.types.RemeshModifier.bl_rna
                .properties["voxel_size"].default)
    check("defaults are ~0.1 on this build (probed)",
          abs(core.mesh_voxel_size_default() - 0.1) < 1e-6)

    # --- native-operator existence probe ------------------------------------
    check("voxel_remesh op exists via get_rna_type",
          core.voxel_remesh_op_exists())
    # Regression pin for the probe finding: bpy.types does NOT expose
    # the native operator (the sibling add-ons' probe would fail here).
    check("bpy.types probe would be WRONG for native ops (probed)",
          not hasattr(bpy.types, "OBJECT_OT_voxel_remesh"))

    # --- mesh_stats -----------------------------------------------------------
    cube = new_cube("StatCube")
    st = core.mesh_stats(cube.data, "original mesh datablock", True)
    check("cube bounds min", st.bounds_min == (-1.0, -1.0, -1.0))
    check("cube bounds max", st.bounds_max == (1.0, 1.0, 1.0))
    check("cube area 24", abs(st.surface_area - 24.0) < 1e-4)
    check("cube counts", st.vert_count == 8 and st.poly_count == 6)
    check("stats carry source/exact",
          st.source == "original mesh datablock" and st.exact is True)

    empty_me = bpy.data.meshes.new("EmptyStats")
    st0 = core.mesh_stats(empty_me, "original mesh datablock", True)
    check("empty mesh stats: zero bounds and area",
          st0.bounds_min == (0.0, 0.0, 0.0)
          and st0.bounds_max == (0.0, 0.0, 0.0)
          and st0.surface_area == 0.0 and st0.vert_count == 0)

    # --- find_voxel_modifier -----------------------------------------------------
    check("no modifier -> None", core.find_voxel_modifier(cube) is None)
    check("non-mesh -> None", core.find_voxel_modifier(None) is None)
    sharp = cube.modifiers.new("Sharp", 'REMESH')
    sharp.mode = 'SHARP'
    check("non-VOXEL remesh ignored",
          core.find_voxel_modifier(cube) is None)
    vox = cube.modifiers.new("Vox", 'REMESH')
    check("modifiers.new REMESH defaults to VOXEL (probed)",
          vox.mode == 'VOXEL')
    # == not `is`: PyRNA hands out fresh wrapper objects (the sibling
    # add-ons' probed trap for `nodes.active is node`).
    check("VOXEL remesh found", core.find_voxel_modifier(cube) == vox)
    cube.modifiers.remove(sharp)
    cube.modifiers.remove(vox)

    # --- modifier_input_source confidence rules -----------------------------------
    first = new_cube("FirstCube")
    rm = first.modifiers.new("Remesh", 'REMESH')
    exact, label, needs = core.modifier_input_source(first, rm)
    check("modifier first in stack: exact, no depsgraph",
          exact and not needs, (exact, label, needs))

    stacked = new_cube("StackedCube")
    sub = stacked.modifiers.new("Subdiv", 'SUBSURF')
    sub.levels = 2
    rm = stacked.modifiers.new("Remesh", 'REMESH')
    exact, label, needs = core.modifier_input_source(stacked, rm)
    check("enabled trailing modifier: approximate",
          not exact and not needs, (exact, label, needs))
    check("approximate label says why", "preceding" in label)
    rm.show_viewport = False
    exact, label, needs = core.modifier_input_source(stacked, rm)
    check("DISABLED trailing modifier: exact via depsgraph",
          exact and needs, (exact, label, needs))
    # mid-stack disabled is still approximate
    tail = stacked.modifiers.new("Tail", 'SUBSURF')
    exact, label, needs = core.modifier_input_source(stacked, rm)
    check("disabled MID-stack modifier: approximate",
          not exact and not needs, (exact, label, needs))
    stacked.modifiers.remove(tail)

    # --- refresh_stats + cache -------------------------------------------------------
    make_active(stacked)
    deps = bpy.context.evaluated_depsgraph_get()
    check("refresh_stats returns True",
          core.refresh_stats(stacked, deps))
    mesh_st = core.cached_stats(stacked.name, core.CONTEXT_MESH)
    mod_st = core.cached_stats(stacked.name, core.CONTEXT_MODIFIER)
    check("mesh context cached (base cube, 8 verts)",
          mesh_st is not None and mesh_st.vert_count == 8
          and mesh_st.exact)
    check("modifier context cached from EVALUATED input (98 subsurf "
          "verts, probed)",
          mod_st is not None and mod_st.vert_count == 98
          and mod_st.exact, mod_st)
    check("modifier stats bounds match subsurf output (inside base "
          "cube)",
          mod_st.bounds_max[0] <= 1.0 + 1e-6
          and mod_st.bounds_max[0] > 0.5)

    # needs-depsgraph case WITHOUT a depsgraph: honest degrade
    core.invalidate(stacked.name)
    core.refresh_stats(stacked, None)
    mod_st = core.cached_stats(stacked.name, core.CONTEXT_MODIFIER)
    check("no depsgraph offered: approximate base-mesh stand-in",
          mod_st is not None and not mod_st.exact
          and mod_st.vert_count == 8, mod_st)

    # no modifier: modifier cache entry dropped
    plain = new_cube("PlainCube")
    core.refresh_stats(plain)
    check("plain object: no modifier cache entry",
          core.cached_stats(plain.name, core.CONTEXT_MODIFIER) is None
          and core.cached_stats(plain.name, core.CONTEXT_MESH)
          is not None)

    # --- current_estimate (draw-safe arithmetic) ---------------------------------------
    plain.data.remesh_voxel_size = 0.1
    est = core.current_estimate(plain, core.CONTEXT_MESH)
    check("estimate from cache: 20 cells longest axis",
          est is not None and est.longest_axis_cells == 20, est)
    check("estimate voxel size VERBATIM native value",
          est.voxel_size == plain.data.remesh_voxel_size)
    check("estimate exact for the destructive path",
          est.confidence == estimate.CONF_EXACT)
    check("no warnings at identity", est.scale_warnings == ())

    plain.scale = (2.0, 2.0, 2.0)
    bpy.context.view_layer.update()
    est = core.current_estimate(plain, core.CONTEXT_MESH)
    check("unapplied scale warned via matrix_world",
          estimate.WARN_UNAPPLIED_SCALE in est.scale_warnings)
    check("cells unchanged by scale (object space, probed)",
          est.longest_axis_cells == 20)
    check("world cell sizes doubled",
          all(abs(w - 0.2) < 1e-6 for w in est.world_axis_sizes))
    plain.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()

    plain.data.remesh_voxel_size = 0.0
    check("invalid voxel size -> None (panel shows its own row)",
          core.current_estimate(plain, core.CONTEXT_MESH) is None)
    plain.data.remesh_voxel_size = 0.1

    check("uncached object -> None",
          core.current_estimate(new_cube("Uncached"),
                                core.CONTEXT_MESH) is None)
    check("modifier estimate without modifier -> None",
          core.current_estimate(plain, core.CONTEXT_MODIFIER) is None)

    # --- bounds_derived_voxel_size ------------------------------------------------------
    v = core.bounds_derived_voxel_size((-1, -1, -1), (1, 1, 1), 64)
    check("bounds-derived size: longest/cells", abs(v - 2.0 / 64) < 1e-9)
    v = core.bounds_derived_voxel_size((0, 0, 0), (0, 0, 0), 64)
    check("degenerate bounds fall back to RNA default (never 0.0 — "
          "probed: zero cannot be solved)",
          abs(v - core.modifier_voxel_size_default()) < 1e-9)

    # --- resolve_guide_context -----------------------------------------------------------
    check("AUTO without modifier -> MESH",
          core.resolve_guide_context(plain, 'AUTO')
          == core.CONTEXT_MESH)
    check("MODIFIER without modifier -> None",
          core.resolve_guide_context(plain, core.CONTEXT_MODIFIER)
          is None)
    check("AUTO with modifier -> MODIFIER",
          core.resolve_guide_context(stacked, 'AUTO')
          == core.CONTEXT_MODIFIER)
    check("explicit MESH wins even with modifier",
          core.resolve_guide_context(stacked, core.CONTEXT_MESH)
          == core.CONTEXT_MESH)

    # --- debounce plumbing (fake clock) ----------------------------------------------------
    d = live.Debounce(0.3)
    check("debounce idle", not d.pending and not d.try_fire(0.0))
    d.note_change(0.0)
    check("debounce pending", d.pending)
    check("debounce quiet not elapsed", not d.try_fire(0.2))
    d.note_change(0.25)     # burst extends the countdown
    check("debounce burst extends", not d.try_fire(0.5))
    check("debounce fires once", d.try_fire(0.56))
    check("debounce consumed", not d.try_fire(10.0))

    core.note_activity(now=100.0)
    make_active(plain)
    core.invalidate(plain.name)
    check("poll_debounce quiet not elapsed",
          not core.poll_debounce(bpy.context, now=100.1))
    # +0.01 margin: 100.3 - 100.0 < 0.3 in float arithmetic.
    check("poll_debounce fires after quiet",
          core.poll_debounce(bpy.context,
                             now=100.01 + core.QUIET_S))
    check("poll refreshed the active object",
          core.cached_stats(plain.name, core.CONTEXT_MESH) is not None)
    check("poll idle after fire",
          not core.poll_debounce(bpy.context, now=200.0))

    # --- invalidate -------------------------------------------------------------------------
    core.invalidate()
    check("invalidate() clears everything",
          core.cached_stats(plain.name, core.CONTEXT_MESH) is None)

    print()
    if FAILURES:
        print("FAILED: %d checks: %s" % (len(FAILURES), FAILURES))
    else:
        print("CALIPERS_CORE_TESTS_PASSED")


try:
    main()
except Exception:
    traceback.print_exc()
    print("CALIPERS_CORE_TESTS_CRASHED")
