# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for stage 1: Create Low-Poly Candidate — the
QuadriFlow path on a real mesh, the Decimate fallback (simulated
QuadriFlow failure/absence via the module's monkeypatch seams), and
the operator's error reporting.

Prints RETOPO_TESTS_PASSED on success.
"""

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


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    import kiln
    from kiln import retopo

    kiln.register()
    try:
        run(kiln, retopo)
    finally:
        kiln.unregister()


def run(kiln, retopo):
    s = bpy.context.scene.kiln

    # --- QuadriFlow primary path ---------------------------------------------
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16)
    high = bpy.context.active_object
    high.name = "Sculpt"
    check("high sphere has 512 faces", len(high.data.polygons) == 512)

    check("op poll fails with no high-poly set",
          not bpy.ops.object.kiln_create_lowpoly.poll())
    s.high_object = high
    check("op poll passes with a high-poly set",
          bpy.ops.object.kiln_create_lowpoly.poll())

    s.target_faces = 200
    s.low_source = 'GENERATE'   # as the panel's Generate mode sets it
    result = bpy.ops.object.kiln_create_lowpoly()
    check("create-lowpoly returned FINISHED", result == {'FINISHED'})
    low = s.low_object
    check("low-poly pointer was set to the new object", low is not None)
    check("success flips low_source back to EXISTING (user lands on "
          "the filled picker, not the generator)",
          s.low_source == 'EXISTING')
    check("low-poly named <high>_low", low.name == "Sculpt_low")
    faces = len(low.data.polygons)
    check("QuadriFlow face count in ballpark of target 200 (100..400)",
          100 <= faces <= 400, "got %d" % faces)
    check("QuadriFlow output is all quads",
          all(len(p.vertices) == 4 for p in low.data.polygons))
    check("no leftover modifiers on the candidate",
          len(low.modifiers) == 0)
    check("candidate keeps the high-poly's transform",
          list(low.matrix_world) == list(high.matrix_world))
    check("remesh drops UVs (stage 2 - seams + unwrap - follows)",
          len(low.data.uv_layers) == 0)
    check("candidate is linked, selected and active",
          low.name in bpy.context.view_layer.objects
          and bpy.context.view_layer.objects.active == low)

    # --- evaluated-mesh duplication (modifiers applied) -------------------------
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    modded = bpy.context.active_object
    modded.name = "Modded"
    sub = modded.modifiers.new("Sub", 'SUBSURF')
    sub.levels = 2
    dup, method, detail = retopo.create_lowpoly_candidate(
        bpy.context, modded, 5000)
    check("duplicate uses the EVALUATED mesh (subsurf applied: "
          "candidate remeshes the visible surface, not the 6-face cage)",
          len(dup.data.polygons) > 24,
          "got %d faces via %s" % (len(dup.data.polygons), method))

    # --- Decimate fallback on simulated QuadriFlow failure ------------------------
    real_run = retopo._run_quadriflow

    def boom(target_faces):
        raise RuntimeError("simulated non-manifold failure")

    retopo._run_quadriflow = boom
    s.low_source = 'GENERATE'
    try:
        result = bpy.ops.object.kiln_create_lowpoly()
    finally:
        retopo._run_quadriflow = real_run
    check("fallback run returned FINISHED (with a WARNING report)",
          result == {'FINISHED'})
    low2 = s.low_object
    check("fallback produced a fresh candidate object",
          low2 is not None and low2 != low
          and low2.name.startswith("Sculpt_low"))
    check("fallback path also flips low_source back to EXISTING",
          s.low_source == 'EXISTING')
    faces = len(low2.data.polygons)
    check("Decimate face count in ballpark of target 200 (100..400; "
          "ratio derived from the triangulated count)",
          100 <= faces <= 400, "got %d" % faces)
    check("decimate modifier was applied, not left on the stack",
          len(low2.modifiers) == 0)

    # --- Decimate fallback when QuadriFlow is unavailable ---------------------------
    real_avail = retopo._quadriflow_available
    retopo._quadriflow_available = lambda: False
    try:
        dup, method, detail = retopo.create_lowpoly_candidate(
            bpy.context, high, 200)
    finally:
        retopo._quadriflow_available = real_avail
    check("unavailable QuadriFlow -> DECIMATE with the reason reported",
          method == 'DECIMATE' and "not available" in detail)
    check("unavailable-path face count in ballpark",
          100 <= len(dup.data.polygons) <= 400,
          "got %d" % len(dup.data.polygons))

    # --- direct-call reason string for the failure path ------------------------------
    retopo._run_quadriflow = boom
    try:
        dup, method, detail = retopo.create_lowpoly_candidate(
            bpy.context, high, 200)
    finally:
        retopo._run_quadriflow = real_run
    check("failure path reports DECIMATE + the QuadriFlow error text",
          method == 'DECIMATE' and "QuadriFlow failed" in detail
          and "simulated non-manifold failure" in detail)

    # --- error reporting: empty high-poly -----------------------------------------------
    empty_me = bpy.data.meshes.new("Empty")
    empty_ob = bpy.data.objects.new("Empty", empty_me)
    bpy.context.collection.objects.link(empty_ob)
    s.high_object = empty_ob
    s.low_source = 'GENERATE'
    try:
        bpy.ops.object.kiln_create_lowpoly()
        raised = False
        msg = ""
    except RuntimeError as exc:   # background: {'ERROR'} report raises
        raised = True
        msg = str(exc)
    check("empty high-poly -> actionable error, no traceback",
          raised and "no faces" in msg, "got %r" % msg)
    check("failed generate leaves low_source on GENERATE (user stays "
          "in the mode they were using)",
          s.low_source == 'GENERATE')


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("RETOPO_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("RETOPO_TESTS_PASSED")
sys.stdout.flush()
