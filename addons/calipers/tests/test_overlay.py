# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for the GPU voxel-size guide (run inside
``blender --background --python``).

GPU objects cannot be created in --background (probed: SystemError), so
this suite covers what CAN be verified headlessly, the same split as
the sibling overlay's tests:

- the pure guide-geometry builders (box/cell/slices, caps, fallback);
- the shader create-info DESCRIPTOR (construction is pure bookkeeping)
  and the GLSL source structure;
- the draw path's error latch: a forced draw headlessly hits the
  SystemError from shader compilation and must latch loudly ONCE;
- an AST audit that every gpu.state.*_set call sits inside the
  _gpu_state_restored guard;
- enable/disable round-trips as harmless no-ops.

Prints CALIPERS_OVERLAY_TESTS_PASSED on success.
"""

import ast
import inspect
import os
import sys
import traceback

import bpy
import bmesh

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from calipers import core, overlay

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def gpu_state_guard_audit(module, guard_name="_gpu_state_restored"):
    """(guard_spans, offenders) — see seam_path_tool's test_register:
    gpu state set/get raises SystemError in --background, so restoration
    is audited structurally: every ``gpu.state.*_set(...)`` call must
    sit inside a ``with <guard_name>(...):`` block, inside the guard
    helper itself, or inside a helper whose every call site is guarded.
    """
    tree = ast.parse(inspect.getsource(module))

    def span(node):
        return (node.lineno, node.end_lineno)

    guard_spans = []
    allowed = []
    funcs = {}
    call_sites = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            funcs[node.name] = span(node)
            if node.name == guard_name:
                allowed.append(span(node))
        elif isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                f = ctx.func if isinstance(ctx, ast.Call) else None
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name == guard_name:
                    guard_spans.append(span(node))
                    allowed.append(span(node))
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            call_sites.setdefault(name, []).append(node.lineno)

    def is_allowed(line):
        return any(a <= line <= b for a, b in allowed)

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.endswith("_set")
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "state"
                and getattr(node.func.value.value, "id", None) == "gpu"):
            continue
        line = node.lineno
        if is_allowed(line):
            continue
        owner, owner_size = None, None
        for fname, (a, b) in funcs.items():
            if a <= line <= b and fname != guard_name:
                if owner is None or (b - a) < owner_size:
                    owner, owner_size = fname, b - a
        sites = call_sites.get(owner, []) if owner else []
        if owner and sites and all(is_allowed(ln) for ln in sites):
            continue
        offenders.append("%s at line %d" % (node.func.attr, line))
    return guard_spans, offenders


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
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # --- pure geometry: bounding box ---------------------------------------
    lo, hi = (-1.0, -2.0, -3.0), (1.0, 2.0, 3.0)
    box = overlay.build_box_lines(lo, hi)
    check("box: 12 edges = 24 endpoints", len(box) == 24)
    xs = sorted({p[0] for p in box})
    check("box spans lo/hi", xs == [-1.0, 1.0]
          and sorted({p[2] for p in box}) == [-3.0, 3.0])

    # --- pure geometry: sample cell -----------------------------------------
    cell = overlay.build_cell_lines((-1.0, -1.0, -1.0), 0.25)
    check("cell: 24 endpoints", len(cell) == 24)
    check("cell anchored at min corner, edge v",
          min(p[0] for p in cell) == -1.0
          and abs(max(p[0] for p in cell) - (-0.75)) < 1e-9)

    # --- pure geometry: slices ------------------------------------------------
    lo, hi = (-1.0,) * 3, (1.0,) * 3
    pts, capped = overlay.build_slice_lines(lo, hi, 0.5)
    # cube, v=0.5: 4 cells per axis -> per slice (4+1)+(4+1)=10 lines
    # -> 20 endpoints; 3 slices -> 60.
    check("slices: 3 x 10 lines for cube at v=0.5",
          len(pts) == 60, len(pts))
    check("slices: not capped at moderate density", not capped)
    check("slice lines clipped to bounds",
          all(-1.0 - 1e-9 <= c <= 1.0 + 1e-9 for p in pts for c in p))

    # cap boundary: cap of exactly n+1 lines is allowed...
    pts_ok, capped_ok = overlay.build_slice_lines(lo, hi, 0.5, cap=5)
    check("cap boundary: n+1 == cap allowed",
          not capped_ok and len(pts_ok) == 60)
    # ...one less caps out, and capped slices are dropped ENTIRELY
    pts_over, capped_over = overlay.build_slice_lines(lo, hi, 0.5, cap=4)
    check("over cap: slices dropped entirely (no partial grid)",
          capped_over and len(pts_over) == 0, len(pts_over))

    # extreme density: default cap kicks in, fallback engaged
    pts_x, capped_x = overlay.build_slice_lines(lo, hi, 0.0001)
    check("extreme density: capped, nothing drawn per-voxel",
          capped_x and len(pts_x) == 0)

    # zero-extent axis: still valid (flat plane), no exception
    pts_f, capped_f = overlay.build_slice_lines(
        (-1.0, -1.0, 0.0), (1.0, 1.0, 0.0), 0.5)
    check("flat bounds: slices build without error",
          len(pts_f) > 0 and not capped_f)

    # build_guide assembles all parts + cap flag
    guide = overlay.build_guide(lo, hi, 0.0001)
    check("build_guide: box+cell survive the fallback",
          len(guide["box"]) == 24 and len(guide["cell"]) == 24
          and guide["capped"] and len(guide["slices"]) == 0)

    # --- shader descriptor (pure bookkeeping headless) ------------------------
    try:
        info = overlay._shader_create_info()
        check("create-info descriptor builds headless",
              info is not None)
    except Exception as exc:
        check("create-info descriptor builds headless", False, repr(exc))
    for token in ("ModelViewProjectionMatrix", "pos"):
        check("vertex source mentions %s" % token,
              token in overlay.VERT_SHADER_SRC)
    for token in ("line_color", "fragColor"):
        check("fragment source mentions %s" % token,
              token in overlay.FRAG_SHADER_SRC)
    # The compile step must be the (guarded) GPU boundary: headless it
    # raises — this is exactly why creation is lazy behind the latch.
    try:
        overlay._create_shader()
        check("create_from_info raises headless (probed)", False,
              "compilation unexpectedly succeeded in --background")
    except Exception:
        check("create_from_info raises headless (probed)", True)

    # --- gpu.state hygiene audit ------------------------------------------------
    guard_spans, offenders = gpu_state_guard_audit(overlay)
    check("at least one _gpu_state_restored block",
          len(guard_spans) >= 1)
    check("every gpu.state.*_set guarded", offenders == [],
          offenders)

    # --- enable / draw-latch / disable round-trip -------------------------------
    cube = new_cube("GuideCube")
    make_active(cube)
    check("enable requires a mesh: ok", overlay.enable(bpy.context))
    check("enabled", overlay.is_enabled())
    check("tracked object", overlay.tracked_object_name() == cube.name)

    # Draw with no cached stats: silent no-draw, NOT an error.
    overlay._draw_3d()
    check("draw without stats: no latch",
          overlay.last_draw_error() is None)

    # With stats + valid voxel size the draw reaches shader creation,
    # which raises headless -> the latch must trip, once.
    cube.data.remesh_voxel_size = 0.1
    core.refresh_stats(cube)
    overlay.mark_dirty()
    overlay._draw_3d()
    check("draw with stats headless: error latched (SystemError at "
          "the GPU boundary)", overlay.last_draw_error() is not None)
    latched = overlay.last_draw_error()
    overlay._draw_3d()
    check("latch holds: second draw does not re-raise/re-log",
          overlay.last_draw_error() == latched)
    overlay.mark_dirty()
    check("mark_dirty clears the latch (fresh chance)",
          overlay.last_draw_error() is None)

    # 2D pass without annotation: quiet no-op
    overlay._draw_2d()
    check("2d draw without annotation: no latch",
          overlay.last_draw_error() is None)

    overlay.disable()
    check("disabled cleanly", not overlay.is_enabled()
          and overlay.tracked_object_name() is None
          and overlay.last_draw_error() is None)

    # enable with no mesh active -> refused
    bpy.context.view_layer.objects.active = None
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    check("enable refused without an active mesh",
          not overlay.enable(bpy.context))

    print()
    if FAILURES:
        print("FAILED: %d checks: %s" % (len(FAILURES), FAILURES))
    else:
        print("CALIPERS_OVERLAY_TESTS_PASSED")


try:
    main()
except Exception:
    traceback.print_exc()
    print("CALIPERS_OVERLAY_TESTS_CRASHED")
