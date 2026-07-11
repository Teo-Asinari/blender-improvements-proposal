# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for the stage-2 readiness checklist on constructed
meshes (run inside `blender --background --python`).

Prints READINESS_TESTS_PASSED on success.
"""

import math
import os
import sys
import traceback

import bpy

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


def states(items):
    return {i.key: i.state for i in items}


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    from ez_bake import readiness

    OK, WARN, FAIL = readiness.OK, readiness.WARN, readiness.FAIL

    # --- mesh with no UV layer ----------------------------------------------
    me = bpy.data.meshes.new("NoUV")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                   [], [(0, 1, 2, 3)])
    me.update()
    ob = bpy.data.objects.new("NoUV", me)
    bpy.context.collection.objects.link(ob)

    items = readiness.evaluate(ob)
    st = states(items)
    check("checklist has the four documented keys",
          set(st) == {"uv_layer", "uv_valid", "scale", "normals"})
    check("no UV layer -> uv_layer FAIL + uv_valid FAIL",
          st["uv_layer"] == FAIL and st["uv_valid"] == FAIL)
    check("scale/normals OK on a fresh object",
          st["scale"] == OK and st["normals"] == OK)
    check("blocking() returns exactly the FAILs",
          [i.key for i in readiness.blocking(items)]
          == ["uv_layer", "uv_valid"])
    check("FAIL details are actionable (mention unwrap)",
          "unwrap" in items[0].detail.lower())

    # --- all-zero (default) UV layer is degenerate -----------------------------
    me.uv_layers.new(name="UVMap")
    me.uv_layers.active.data.foreach_set(
        "uv", [0.0] * (len(me.loops) * 2))
    st = states(readiness.evaluate(ob))
    check("all-zero UVs -> uv_layer OK but uv_valid FAIL",
          st["uv_layer"] == OK and st["uv_valid"] == FAIL)

    # --- collapsed-to-a-line UVs are degenerate ----------------------------------
    me.uv_layers.active.data.foreach_set(
        "uv", [c for uv in [(0, 0), (0.5, 0), (1, 0), (0.25, 0)]
               for c in uv])
    st = states(readiness.evaluate(ob))
    check("line-collapsed UVs -> uv_valid FAIL", st["uv_valid"] == FAIL)

    # --- healthy UVs pass ----------------------------------------------------------
    me.uv_layers.active.data.foreach_set(
        "uv", [c for uv in [(0, 0), (1, 0), (1, 1), (0, 1)] for c in uv])
    st = states(readiness.evaluate(ob))
    check("healthy quad layout -> all OK",
          all(s == OK for s in st.values()), "got %r" % st)

    # --- primitive cube (auto-UVs) is ready out of the box ----------------------------
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    items = readiness.evaluate(cube)
    check("default cube with generated UVs is fully ready",
          all(i.state == OK for i in items),
          "got %r" % (states(items),))
    check("warnings() empty when all OK",
          readiness.warnings(items) == [])

    # --- scale warnings ---------------------------------------------------------------
    cube.scale = (2.0, 1.0, 1.0)
    st = states(readiness.evaluate(cube))
    check("non-unit scale -> scale WARN (not FAIL: bake still allowed)",
          st["scale"] == WARN and st["normals"] == OK)
    check("scale WARN does not block",
          readiness.blocking(readiness.evaluate(cube)) == [])

    cube.scale = (1.0, 1.0, -1.0)
    st = states(readiness.evaluate(cube))
    check("negative scale -> normals WARN (mirrored transform)",
          st["normals"] == WARN)
    check("negative scale also fails the applied-scale check",
          st["scale"] == WARN)
    cube.scale = (1.0, 1.0, 1.0)

    # --- edit-mode evaluation (update_from_editmode path) ------------------------------
    bpy.context.view_layer.objects.active = cube
    bpy.ops.object.mode_set(mode='EDIT')
    items = readiness.evaluate(cube)
    check("evaluation works in edit mode (probed 5.1.2: mesh uv arrays "
          "are empty while the edit bmesh owns the data, so the mesh "
          "is synced first)",
          all(i.state == OK for i in items),
          "got %r" % (states(items),))
    bpy.ops.object.mode_set(mode='OBJECT')

    # --- pair diagonal (drives the extrusion heuristic) --------------------------------
    diag = readiness.pair_diagonal(cube, cube)
    check("pair diagonal of one 2x2x2 cube is 2*sqrt(3)",
          abs(diag - 2.0 * math.sqrt(3.0)) < 1e-5, "got %r" % diag)
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(10, 0, 0))
    cube2 = bpy.context.active_object
    diag = readiness.pair_diagonal(cube, cube2)
    check("pair diagonal spans both objects' world-space boxes",
          abs(diag - math.sqrt(12.0 ** 2 + 4 + 4)) < 1e-5,
          "got %r" % diag)
    cube2.scale = (2.0, 2.0, 2.0)
    bpy.context.view_layer.update()
    diag2 = readiness.pair_diagonal(cube, cube2)
    check("pair diagonal respects object transforms (scaled cube grows "
          "the box)", diag2 > diag, "got %r vs %r" % (diag2, diag))

    # --- panel cache (TTL + invalidate) -------------------------------------------------
    readiness.invalidate()
    a = readiness.evaluate_cached(cube, now=100.0)
    b = readiness.evaluate_cached(cube, now=100.5)
    check("cache hit within TTL returns the same list object", a is b)
    c = readiness.evaluate_cached(cube, now=100.0 + readiness.CACHE_TTL_S
                                  + 0.1)
    check("cache expires after the TTL", c is not a)
    readiness.invalidate()
    d = readiness.evaluate_cached(cube, now=100.0 + readiness.CACHE_TTL_S
                                  + 0.2)
    check("invalidate() forces a fresh evaluation", d is not c)


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("READINESS_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("READINESS_TESTS_PASSED")
sys.stdout.flush()
