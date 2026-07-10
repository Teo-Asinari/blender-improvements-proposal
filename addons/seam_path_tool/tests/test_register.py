# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless registration + operator test (run inside
`blender --background --python`).

Loads the add-on from source, registers it, builds a mesh, selects two
vertices and runs the non-modal `mesh.seam_path_mark` operator, asserts
seams, then unregisters cleanly.

Prints REGISTER_TESTS_PASSED on success.
"""

import math
import os
import sys
import traceback

import bpy
import bmesh
from mathutils import Vector

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


def vert_at(bm, co, tol=1e-5):
    best, best_d = None, tol
    for v in bm.verts:
        d = (v.co - Vector(co)).length
        if d < best_d:
            best, best_d = v, d
    assert best is not None, "no vertex near %r" % (co,)
    return best


def main():
    # Clean slate first: read_factory_settings resets keyconfigs, so it must
    # happen BEFORE register() or the add-on's keymap references go stale.
    bpy.ops.wm.read_factory_settings(use_empty=True)

    import seam_path_tool

    # --- register ---------------------------------------------------------
    seam_path_tool.register()
    check("operator mesh.seam_path_mark registered",
          hasattr(bpy.ops.mesh, "seam_path_mark")
          and bpy.ops.mesh.seam_path_mark.idname_py() == "mesh.seam_path_mark")
    check("operator mesh.seam_path_interactive registered",
          hasattr(bpy.ops.mesh, "seam_path_interactive"))

    # bl_info sanity (legacy add-on packaging works on this binary).
    check("bl_info present", isinstance(seam_path_tool.bl_info, dict)
          and seam_path_tool.bl_info.get("name") == "Seam Path Tool")

    # --- build a test mesh -------------------------------------------------
    seg = 4
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=seg, y_subdivisions=seg,
                                    size=2.0)
    obj = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    check("edit mode entered", bpy.context.mode == 'EDIT_MESH')

    bm = bmesh.from_edit_mesh(obj.data)
    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    ys = sorted({round(v.co.y, 6) for v in bm.verts})
    a = vert_at(bm, (xs[0], ys[0], 0.0))
    b = vert_at(bm, (xs[-1], ys[-1], 0.0))

    # Select the two endpoints and put them in the selection history
    # (mimics clicking one then shift-clicking the other).
    for v in bm.verts:
        v.select = False
    bm.select_flush(False)
    a.select = True
    b.select = True
    bm.select_history.clear()
    bm.select_history.add(a)
    bm.select_history.add(b)
    bmesh.update_edit_mesh(obj.data)

    # --- run the operator ---------------------------------------------------
    check("poll passes in edit mode", bpy.ops.mesh.seam_path_mark.poll())
    result = bpy.ops.mesh.seam_path_mark(mode='LENGTH')
    check("operator returned FINISHED", result == {'FINISHED'},
          "got %r" % (result,))

    bm = bmesh.from_edit_mesh(obj.data)
    seam_edges = [e for e in bm.edges if e.seam]
    check("seam edge count == 2*seg (Manhattan path)",
          len(seam_edges) == 2 * seg, "got %d" % len(seam_edges))
    total = sum(e.calc_length() for e in seam_edges)
    a2 = vert_at(bm, (xs[0], ys[0], 0.0))
    b2 = vert_at(bm, (xs[-1], ys[-1], 0.0))
    manhattan = abs(b2.co.x - a2.co.x) + abs(b2.co.y - a2.co.y)
    check("seam total length == Manhattan distance",
          math.isclose(total, manhattan, rel_tol=1e-6),
          "got %f expected %f" % (total, manhattan))

    # --- clear option --------------------------------------------------------
    # Selection history is consumed-agnostic; re-add endpoints and clear.
    bm.select_history.clear()
    bm.select_history.add(a2)
    bm.select_history.add(b2)
    result = bpy.ops.mesh.seam_path_mark(mode='LENGTH', clear=True)
    check("clear run returned FINISHED", result == {'FINISHED'})
    bm = bmesh.from_edit_mesh(obj.data)
    check("clear removed all seams", not any(e.seam for e in bm.edges))

    # --- error path: bad selection -------------------------------------------
    # In background mode, an operator that reports {'ERROR'} raises
    # RuntimeError from bpy.ops rather than returning {'CANCELLED'}.
    bm.select_history.clear()
    for v in bm.verts:
        v.select = True
    try:
        result = bpy.ops.mesh.seam_path_mark()
        cancelled = result == {'CANCELLED'}
    except RuntimeError as ex:
        cancelled = "Select exactly 2 vertices" in str(ex)
    check("all-verts selection is rejected", cancelled)

    bpy.ops.object.mode_set(mode='OBJECT')

    # --- preview overlay: pure parts (no GPU in background mode) --------------
    from seam_path_tool import preview as pv

    lines = pv.compose_help_lines('LENGTH', 2, 1, False)
    check("help panel is two lines", len(lines) == 2)
    check("help controls line mentions bindings",
          "LMB" in lines[0] and "Backspace" in lines[0]
          and "finish" in lines[0])
    check("help status line shows counts and mode",
          "Anchors: 2" in lines[1] and "Segments: 1" in lines[1]
          and "LENGTH" in lines[1])
    check("no ERASE tag when inactive", "[ERASE]" not in lines[1])
    lines_erase = pv.compose_help_lines('TOPOLOGY', 0, 0, True)
    check("ERASE tag when active", "[ERASE]" in lines_erase[1])
    check("TOPOLOGY mode shown", "TOPOLOGY" in lines_erase[1])

    ring = pv.circle_points_2d((10.0, -4.0), 5.0, segments=16)
    check("snap ring is closed with segments+1 points",
          len(ring) == 17
          and math.isclose(ring[0][0], ring[-1][0], abs_tol=1e-9)
          and math.isclose(ring[0][1], ring[-1][1], abs_tol=1e-9))
    check("snap ring points lie on the radius",
          all(math.isclose(math.hypot(x - 10.0, y + 4.0), 5.0,
                           rel_tol=1e-9) for x, y in ring))

    check("ui scale guard is positive in background mode",
          pv._ui_scale() > 0.0)

    p = pv.PathPreview()
    p.set_status(mode='LENGTH', anchors=3, segments=2, erase_active=True)
    check("PathPreview.help_lines reflects status",
          "Anchors: 3" in p.help_lines()[1]
          and "[ERASE]" in p.help_lines()[1])
    check("stop before start is safe", (p.stop() or True))
    # Handler add/remove works headlessly (draw callbacks never fire in
    # background, so no GPU resources are created).
    p.committed_segments.append(([(0, 0, 0), (1, 0, 0)], False))
    p.snap_coord = (0.0, 0.0, 0.0)
    p.start()
    check("start registers both draw handlers",
          p._handle_3d is not None and p._handle_2d is not None)
    p.start()  # idempotent
    p.stop()
    check("stop removes handlers and clears geometry state",
          p._handle_3d is None and p._handle_2d is None
          and not p.committed_segments and p.snap_coord is None)
    p.stop()  # idempotent
    check("stop is idempotent", True)

    # --- unregister ------------------------------------------------------------
    seam_path_tool.unregister()
    check("operator gone after unregister",
          not hasattr(bpy.ops.mesh, "seam_path_mark")
          or not _op_exists("mesh.seam_path_mark"))

    # Re-register/unregister cycle must not raise (idempotent lifecycle).
    seam_path_tool.register()
    seam_path_tool.unregister()
    check("register/unregister cycle clean", True)


def _op_exists(idname):
    try:
        op = bpy.ops.mesh.seam_path_mark
        op.get_rna_type()
        return True
    except Exception:
        return False


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("REGISTER_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("REGISTER_TESTS_PASSED")
sys.stdout.flush()
