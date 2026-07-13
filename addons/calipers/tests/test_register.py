# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless registration + operator tests (run inside
``blender --background --python``).

Loads the add-on from source, registers it, and exercises the operator
surface: refresh, safe-add (pending state, bounds-derived size, remesh
NEVER evaluated), enable-confirm, set-from-world-target math, the
confirming voxel-remesh wrapper (including invalid-size rejection), and
the overlay toggle round-trip. Unregisters cleanly.

Prints CALIPERS_REGISTER_TESTS_PASSED on success.
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


def evaluated_verts(ob):
    deps = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(deps)
    me = ev.to_mesh()
    try:
        return len(me.vertices)
    finally:
        ev.to_mesh_clear()


def evaluated_longest_axis(ob):
    """Longest bounding-box axis of the EVALUATED vertices. Vertex scan
    on purpose: probed on 5.1.2, evaluated_get().bound_box is NOT tight
    to the evaluated geometry (a subsurf-shrunk cube still reports the
    base ±1 box), so bound_box would be wrong here."""
    deps = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(deps)
    me = ev.to_mesh()
    try:
        lo = [min(v.co[i] for v in me.vertices) for i in range(3)]
        hi = [max(v.co[i] for v in me.vertices) for i in range(3)]
        return max(h - l for l, h in zip(lo, hi))
    finally:
        ev.to_mesh_clear()


def op_cancelled(fn):
    """Run a bpy.ops call expected to CANCEL with an error report.
    In background mode bpy.ops RAISES RuntimeError when the operator
    reports {'ERROR'}, so both shapes count as a clean rejection."""
    try:
        return fn() == {'CANCELLED'}
    except RuntimeError:
        return True


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    import calipers
    from calipers import core, estimate, overlay
    calipers.register()
    try:
        run_registered(calipers, core, estimate, overlay)
    finally:
        calipers.unregister()

    # --- clean unregister -----------------------------------------------------
    check("panel gone after unregister",
          not hasattr(bpy.types, "VIEW3D_PT_calipers"))
    check("scene settings gone after unregister",
          not hasattr(bpy.types.Scene, "calipers"))
    check("depsgraph handler removed",
          calipers._on_depsgraph_update
          not in bpy.app.handlers.depsgraph_update_post)
    check("timer removed",
          not bpy.app.timers.is_registered(calipers._timer_cb))
    check("overlay disabled by unregister", not overlay.is_enabled())


def run_registered(calipers, core, estimate, overlay):
    # --- registration surface ----------------------------------------------
    for type_name in ("VIEW3D_PT_calipers",
                      "OBJECT_OT_calipers_add_remesh_safe",
                      "OBJECT_OT_calipers_enable_modifier",
                      "OBJECT_OT_calipers_voxel_remesh",
                      "OBJECT_OT_calipers_set_from_world",
                      "OBJECT_OT_calipers_refresh",
                      "VIEW3D_OT_calipers_overlay_toggle"):
        check("registered: %s" % type_name,
              hasattr(bpy.types, type_name))
    check("depsgraph handler installed",
          calipers._on_depsgraph_update
          in bpy.app.handlers.depsgraph_update_post)
    check("timer installed",
          bpy.app.timers.is_registered(calipers._timer_cb))

    s = bpy.context.scene.calipers
    check("settings defaults from estimate module",
          s.yellow_exp == estimate.DEFAULT_YELLOW_EXP
          and s.red_exp == estimate.DEFAULT_RED_EXP)
    check("guide source default AUTO", s.guide_source == 'AUTO')
    check("bounding dimensions default off",
          not s.show_bounds_dimensions)

    # --- refresh operator -----------------------------------------------------
    cube = new_cube("RegCube")
    make_active(cube)
    ret = bpy.ops.object.calipers_refresh()
    check("refresh FINISHED", ret == {'FINISHED'})
    check("refresh populated the cache",
          core.cached_stats(cube.name, core.CONTEXT_MESH) is not None)

    # --- safe-add: pending state, bounds-derived size, nothing evaluated ------
    target = new_cube("SafeAddCube")
    sub = target.modifiers.new("Subdiv", 'SUBSURF')
    sub.levels = 2
    make_active(target)
    before = evaluated_verts(target)
    check("precondition: subsurf output 98 verts (probed)",
          before == 98, before)
    longest = evaluated_longest_axis(target)
    check("precondition: subsurf shrank the cube below 2.0",
          0.5 < longest < 2.0, longest)
    ret = bpy.ops.object.calipers_add_remesh_safe()
    check("safe-add FINISHED", ret == {'FINISHED'})
    mod = core.find_voxel_modifier(target)
    check("safe-add created a VOXEL remesh modifier",
          mod is not None and mod.mode == 'VOXEL')
    check("safe-add: show_viewport False (PENDING)",
          not mod.show_viewport)
    check("safe-add: show_render False too", not mod.show_render)
    # Bounds-derived initial size from the EVALUATED input geometry
    # (subsurf shrinks the cube; the tight vertex bounds are what
    # enters the modifier). Tolerance: modifier props are float32 DNA.
    expected = longest / bpy.context.scene.calipers.safe_add_cells
    check("safe-add: bounds-derived voxel size (evaluated longest "
          "axis / cells)",
          abs(mod.voxel_size - expected) < 1e-6,
          (mod.voxel_size, expected))
    check("safe-add: the remesh NEVER evaluated (still 98 verts)",
          evaluated_verts(target) == 98, evaluated_verts(target))
    mod_st = core.cached_stats(target.name, core.CONTEXT_MODIFIER)
    check("safe-add: modifier-input stats cached EXACT (disabled "
          "trailing => evaluated input)",
          mod_st is not None and mod_st.exact
          and mod_st.vert_count == 98, mod_st)

    # --- enable-confirm: the toggle is the confirmed expensive event -----------
    ret = bpy.ops.object.calipers_enable_modifier(
        modifier_name=mod.name)
    check("enable FINISHED", ret == {'FINISHED'})
    check("enable turned viewport evaluation on", mod.show_viewport)
    check("enable turned render evaluation on", mod.show_render)
    after = evaluated_verts(target)
    check("remesh evaluated only after confirm", after != 98, after)

    # --- set from world target ----------------------------------------------------
    scaled = new_cube("ScaledCube")
    scaled.scale = (2.0, 2.0, 2.0)
    make_active(scaled)
    bpy.context.view_layer.update()
    bpy.context.scene.calipers.world_target = 0.1
    ret = bpy.ops.object.calipers_set_from_world(target='MESH')
    check("set-from-world (mesh) FINISHED", ret == {'FINISHED'})
    check("uniform 2x: 0.1 world -> 0.05 object",
          abs(scaled.data.remesh_voxel_size - 0.05) < 1e-6,
          scaled.data.remesh_voxel_size)

    nonuni = new_cube("NonUniCube")
    nonuni.scale = (3.0, 1.0, 0.5)
    nonuni.rotation_euler = (0.3, 0.2, 0.1)   # rotation must not matter
    mod2 = nonuni.modifiers.new("Vox", 'REMESH')
    make_active(nonuni)
    bpy.context.view_layer.update()
    bpy.context.scene.calipers.world_target = 0.3
    ret = bpy.ops.object.calipers_set_from_world(target='MODIFIER')
    check("set-from-world (modifier) FINISHED", ret == {'FINISHED'})
    check("non-uniform: divides by LARGEST axis scale (0.3/3)",
          abs(mod2.voxel_size - 0.1) < 1e-6, mod2.voxel_size)
    # 1e-7: modifier properties are float32 in DNA — exact float64
    # equality with the pure helper is impossible by construction.
    check("conversion matches the documented pure helper",
          abs(mod2.voxel_size - estimate.world_target_to_object(
              0.3, [list(r) for r in nonuni.matrix_world])) < 1e-7)
    check("mesh value untouched by the modifier write",
          abs(nonuni.data.remesh_voxel_size
              - core.mesh_voxel_size_default()) < 1e-6)

    # no modifier -> clean error (bpy.ops raises on ERROR reports in
    # background mode, so accept either shape)
    make_active(cube)
    check("set-from-world without modifier rejected cleanly",
          op_cancelled(lambda: bpy.ops.object.calipers_set_from_world(
              target='MODIFIER')))

    # --- confirming voxel-remesh wrapper ---------------------------------------------
    prey = new_cube("RemeshMe")
    make_active(prey)
    prey.data.remesh_voxel_size = 0.25
    ret = bpy.ops.object.calipers_voxel_remesh()
    check("wrapper remesh FINISHED", ret == {'FINISHED'})
    check("wrapper remesh replaced the mesh",
          len(prey.data.vertices) > 8, len(prey.data.vertices))

    bad = new_cube("BadSize")
    make_active(bad)
    bad.data.remesh_voxel_size = 0.0     # assignable on 5.1.2 (probed)
    check("wrapper rejects v=0 cleanly (native would RAISE mid-op)",
          op_cancelled(bpy.ops.object.calipers_voxel_remesh))
    check("mesh untouched on rejection", len(bad.data.vertices) == 8)

    # poll defers to the native operator's poll (probed: edit mode False)
    bpy.ops.object.mode_set(mode='EDIT')
    check("wrapper poll False in edit mode (defers to native poll)",
          not bpy.ops.object.calipers_voxel_remesh.poll())
    bpy.ops.object.mode_set(mode='OBJECT')
    check("wrapper poll True in object mode",
          bpy.ops.object.calipers_voxel_remesh.poll())

    # --- overlay toggle round-trip (headless no-op drawing) ------------------------------
    make_active(cube)
    ret = bpy.ops.view3d.calipers_overlay_toggle()
    check("overlay toggle on FINISHED", ret == {'FINISHED'})
    check("overlay enabled", overlay.is_enabled())
    check("overlay tracks the active object",
          overlay.tracked_object_name() == cube.name)
    check("no draw error latched by enabling headless",
          overlay.last_draw_error() is None)
    ret = bpy.ops.view3d.calipers_overlay_toggle()
    check("overlay toggle off FINISHED", ret == {'FINISHED'})
    check("overlay disabled", not overlay.is_enabled())

    # re-enable and leave it on: unregister must clean it up
    bpy.ops.view3d.calipers_overlay_toggle()
    check("overlay re-enabled for unregister test",
          overlay.is_enabled())


try:
    main()
    print()
    if FAILURES:
        print("FAILED: %d checks: %s" % (len(FAILURES), FAILURES))
    else:
        print("CALIPERS_REGISTER_TESTS_PASSED")
except Exception:
    traceback.print_exc()
    print("CALIPERS_REGISTER_TESTS_CRASHED")
