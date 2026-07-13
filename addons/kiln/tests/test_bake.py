# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless END-TO-END bake test (run inside `blender --background
--python`): a real Cycles CPU normal bake from a displaced high-poly
sphere onto a smart-UV-projected icosphere, through the actual
operator — asserting the image lands on disk with real normal detail,
the material gets wired, and engine/selection state is restored.

Headless Cycles CPU baking works in --background (probed on 5.1.2),
so this is a genuine bake, not a mock. The bake runs at 128x128 (the
module-level baking.RESOLUTIONS table is shrunk for the test) to keep
the suite fast; the operator path is otherwise 100% real.

Prints BAKE_TESTS_PASSED on success.
"""

import math
import os
import sys
import tempfile
import time
import traceback

import bpy

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

FAILURES = []
T0 = time.time()


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def pixel_stats(img):
    import numpy as np
    px = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(-1, 4)
    return px[:, :3].mean(axis=0), px[:, :3].std(axis=0)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    import kiln
    from kiln import baking, cage, flowcore, readiness

    kiln.register()

    # Shrink the resolution table: the spec'd fast-suite bake is
    # 128x128; everything else in the operator path stays real.
    real_res = dict(baking.RESOLUTIONS)
    baking.RESOLUTIONS['1K'] = 128
    try:
        run(kiln, baking, cage, flowcore, readiness)
    finally:
        baking.RESOLUTIONS.clear()
        baking.RESOLUTIONS.update(real_res)
        kiln.unregister()


def run(kiln, baking, cage, flowcore, readiness):
    s = bpy.context.scene.kiln

    # --- build the pair -------------------------------------------------------
    # High: UV sphere with a sine/cosine radial displacement (~2.3k tris
    # of real detail). Low: subdiv-2 icosphere, smart-UV-projected.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
    high = bpy.context.active_object
    high.name = "Sculpt"
    me = high.data
    for v in me.vertices:
        d = 0.04 * math.sin(8.0 * v.co.x) * math.cos(8.0 * v.co.y)
        v.co += v.normal * d
    me.update()
    check("high-poly built (1152 faces, displaced)",
          len(me.polygons) == 1152)

    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2)
    low = bpy.context.active_object
    low.name = "Retopo"
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.02)
    bpy.ops.object.mode_set(mode='OBJECT')
    check("low-poly built (80 faces, smart-UV-projected)",
          len(low.data.polygons) == 80
          and len(low.data.uv_layers) == 1)

    s.high_object = high
    s.low_object = low
    s.resolution = '1K'          # -> 128 via the shrunk table
    s.margin = 4
    s.use_auto_distances = True
    s.wire_normal_map = True

    tmp = tempfile.gettempdir()
    out_dir = os.path.join(tmp, "kiln_suite", "textures")
    # Trailing separator -> "this directory, default file name"; the
    # nested non-existent dirs also exercise directory creation.
    s.output_path = out_dir + os.sep
    out_file = os.path.join(out_dir, "Retopo_normal.png")
    if os.path.exists(out_file):
        os.remove(out_file)

    # --- auto-distance heuristic feeds the bake ----------------------------------
    diag = readiness.pair_diagonal(high, low)
    exp_ext, exp_ray = flowcore.auto_distances(diag)
    got_ext, got_ray = baking.resolved_distances(s, high, low)
    check("auto distances = heuristic(pair diagonal) [diag %.3f -> "
          "extr %.4f, ray %.4f]" % (diag, got_ext, got_ray),
          got_ext == exp_ext and got_ray == exp_ray
          and abs(got_ext - 0.02 * diag) < 1e-9)
    s.use_auto_distances = False
    s.cage_extrusion = 0.123
    s.max_ray_distance = 0.456
    ovr = baking.resolved_distances(s, high, low)
    check("manual override returns the user's values",
          abs(ovr[0] - 0.123) < 1e-6 and abs(ovr[1] - 0.456) < 1e-6)
    s.use_auto_distances = True

    # --- state to be restored ------------------------------------------------------
    prev_engine = bpy.context.scene.render.engine
    check("baseline engine is not Cycles (restore is observable)",
          prev_engine != 'CYCLES', "got %r" % prev_engine)
    bpy.ops.object.select_all(action='DESELECT')
    high.select_set(True)
    bpy.context.view_layer.objects.active = high

    # --- THE bake --------------------------------------------------------------------
    t_bake = time.time()
    result = bpy.ops.object.kiln_bake()
    t_bake = time.time() - t_bake
    print("  info bake operator wall time: %.1fs" % t_bake)
    check("bake operator returned FINISHED", result == {'FINISHED'})

    check("image saved to the derived path (dirs created)",
          os.path.exists(out_file), out_file)
    check("saved file is a non-trivial PNG",
          os.path.getsize(out_file) > 1000)

    img = bpy.data.images.get("Retopo_normal")
    check("bake image datablock named <low>_normal", img is not None)
    check("image is 128x128 (test-shrunk 1K)",
          tuple(img.size) == (128, 128))
    check("image colorspace is Non-Color (normal data)",
          img.colorspace_settings.name == 'Non-Color')

    mean, std = pixel_stats(img)
    print("  info pixel mean RGB: (%.4f, %.4f, %.4f)" % tuple(mean))
    print("  info pixel std  RGB: (%.4f, %.4f, %.4f)" % tuple(std))
    check("tangent-space mean is ~(0.5, 0.5, 1.0)-ish",
          0.45 <= mean[0] <= 0.55 and 0.45 <= mean[1] <= 0.55
          and mean[2] >= 0.90,
          "mean %r" % (tuple(mean),))
    check("pixels are not uniform: normal detail captured "
          "(std R and G above floor)",
          std[0] > 0.02 and std[1] > 0.02, "std %r" % (tuple(std),))

    # --- state restored ------------------------------------------------------------------
    check("render engine restored", bpy.context.scene.render.engine
          == prev_engine, "got %r" % bpy.context.scene.render.engine)
    check("selection restored (high selected, low not)",
          high.select_get() and not low.select_get())
    check("active object restored",
          bpy.context.view_layer.objects.active == high)

    # --- material wiring ---------------------------------------------------------------------
    mat = low.active_material
    check("low-poly got a material", mat is not None and mat.use_nodes)
    nt = mat.node_tree
    tex = nt.nodes.get(baking.BAKE_NODE_NAME)
    check("bake-target Image Texture node exists and holds the image",
          tex is not None and tex.bl_idname == 'ShaderNodeTexImage'
          and tex.image == img)
    check("bake-target node is the ACTIVE node (5.1.2 bake target "
          "mechanism; compared with ==, not is)",
          nt.nodes.active == tex)
    nm = nt.nodes.get(baking.NORMAL_MAP_NODE_NAME)
    check("Normal Map node exists, tangent space",
          nm is not None and nm.bl_idname == 'ShaderNodeNormalMap'
          and nm.space == 'TANGENT')
    links = {(l.from_node.name, l.from_socket.name,
              l.to_node.name, l.to_socket.name) for l in nt.links}
    check("Image Texture.Color -> Normal Map.Color link",
          (tex.name, "Color", nm.name, "Color") in links)
    principled = next(n for n in nt.nodes
                      if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    check("Normal Map.Normal -> Principled BSDF.Normal link",
          (nm.name, "Normal", principled.name, "Normal") in links)

    # --- re-bake reuses datablocks (no .001 pile-up) ---------------------------------------------
    n_nodes = len(nt.nodes)
    result = bpy.ops.object.kiln_bake()
    check("re-bake FINISHED", result == {'FINISHED'})
    check("re-bake reused the image datablock (no Retopo_normal.001)",
          bpy.data.images.get("Retopo_normal.001") is None
          and bpy.data.images.get("Retopo_normal") == img)
    check("re-bake reused the nodes (count unchanged)",
          len(nt.nodes) == n_nodes,
          "%d -> %d" % (n_nodes, len(nt.nodes)))

    # --- automatic cage: extrusion is the only projection distance ------------
    s.projection_mode = 'AUTO_CAGE'
    auto_kwargs = baking._bake_kwargs(s, 0.1, 0.2)
    check("automatic-cage kwargs use extrusion, not max ray",
          auto_kwargs["use_cage"] is True
          and abs(auto_kwargs["cage_extrusion"] - 0.1) < 1e-6
          and "max_ray_distance" not in auto_kwargs)
    result = bpy.ops.object.kiln_bake()
    check("automatic-cage bake FINISHED", result == {'FINISHED'})

    # --- explicit cage: preview geometry is the actual bake cage ----------------
    s.projection_mode = 'PAINTED_CAGE'
    # Artists commonly hide the dense source while inspecting the low-poly.
    # Kiln must still establish a valid selected-to-active set for the bake.
    high.hide_set(True)
    result = bpy.ops.object.kiln_bake()
    outer = cage._find(low, cage.OUTER_ROLE)
    check("explicit-cage bake FINISHED", result == {'FINISHED'})
    check("explicit-cage bake restores hidden high-poly state",
          high.hide_get())
    check("explicit-cage bake generated exact-topology outer guide",
          outer is not None
          and len(outer.data.vertices) == len(low.data.vertices)
          and len(outer.data.polygons) == len(low.data.polygons))
    kwargs = baking._bake_kwargs(s, 0.1, 0.2, outer)
    check("explicit-cage kwargs use named cage without double extrusion",
          kwargs["use_cage"] is True
          and kwargs["cage_object"] == outer.name
          and kwargs["cage_extrusion"] == 0.0
          and "max_ray_distance" not in kwargs)
    s.projection_mode = 'SURFACE'
    surface_kwargs = baking._bake_kwargs(s, 0.1, 0.2)
    check("surface-ray kwargs use max ray, not extrusion",
          surface_kwargs["use_cage"] is False
          and abs(surface_kwargs["max_ray_distance"] - 0.2) < 1e-6
          and "cage_extrusion" not in surface_kwargs)

    # --- wiring can be disabled --------------------------------------------------------------------
    low_b = low.copy()
    low_b.data = low.data.copy()
    low_b.name = "Retopo_b"
    low_b.data.materials.clear()
    bpy.context.collection.objects.link(low_b)
    s.low_object = low_b
    s.wire_normal_map = False
    result = bpy.ops.object.kiln_bake()
    check("wire-off bake FINISHED", result == {'FINISHED'})
    mat_b = low_b.active_material
    check("material auto-created for the material-less low-poly",
          mat_b is not None and mat_b.name == "Retopo_b_baked")
    check("bake-target node present, but NO Normal Map node wired",
          mat_b.node_tree.nodes.get(baking.BAKE_NODE_NAME) is not None
          and mat_b.node_tree.nodes.get(baking.NORMAL_MAP_NODE_NAME)
          is None)
    check("wire-off image saved under its own default name",
          os.path.exists(os.path.join(out_dir, "Retopo_b_normal.png")))
    s.low_object = low
    s.wire_normal_map = True

    # --- actionable error paths (background: {'ERROR'} raises RuntimeError) -----------------
    def expect_error(name, fragment, setup, teardown):
        setup()
        try:
            bpy.ops.object.kiln_bake()
            raised, msg = False, ""
        except RuntimeError as exc:
            raised, msg = True, str(exc)
        finally:
            teardown()
        check(name, raised and fragment in msg,
              "raised=%r msg=%r" % (raised, msg))

    expect_error(
        "same high and low -> clear error",
        "same object",
        lambda: setattr(s, "high_object", low),
        lambda: setattr(s, "high_object", high))

    plane_me = bpy.data.meshes.new("NoUVPlane")
    plane_me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                         [], [(0, 1, 2, 3)])
    plane_me.update()
    plane = bpy.data.objects.new("NoUVPlane", plane_me)
    bpy.context.collection.objects.link(plane)
    expect_error(
        "low-poly without UVs -> blocked by the checklist",
        "not ready",
        lambda: setattr(s, "low_object", plane),
        lambda: setattr(s, "low_object", low))

    expect_error(
        "empty output path in an unsaved .blend -> actionable error",
        "unsaved",
        lambda: setattr(s, "output_path", ""),
        lambda: setattr(s, "output_path", out_dir + os.sep))

    check("engine still restored after the error paths",
          bpy.context.scene.render.engine == prev_engine)


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

print("  info total test wall time: %.1fs" % (time.time() - T0))
sys.stdout.flush()
if FAILURES:
    print("BAKE_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("BAKE_TESTS_PASSED")
sys.stdout.flush()
