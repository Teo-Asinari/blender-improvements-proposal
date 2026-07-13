# SPDX-License-Identifier: GPL-2.0-or-later
"""Probe 02: operator existence probing, poll behavior, zero voxel size
on the destructive op, safe-add timing (show_viewport=False before the
first depsgraph pull), and bound_box semantics.

Prints PROBE_02_DONE at the end.
"""

import traceback

import bpy
import bmesh


def p(label, value):
    print("  PROBE %-52s %r" % (label + ":", value))


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

    # --- reliable existence probe for a NATIVE operator ----------------------
    # probe_01: hasattr(bpy.types, 'OBJECT_OT_voxel_remesh') is False even
    # though the op runs. What works instead?
    try:
        rna = bpy.ops.object.voxel_remesh.get_rna_type()
        p("voxel_remesh.get_rna_type()", rna.identifier)
    except Exception as exc:
        p("voxel_remesh.get_rna_type() RAISED", str(exc).strip()[:80])
    try:
        rna = bpy.ops.object.totally_fake_op.get_rna_type()
        p("fake op get_rna_type()", rna.identifier)
    except Exception as exc:
        p("fake op get_rna_type() RAISED",
          type(exc).__name__ + ": " + str(exc).strip()[:60])
    # Does the idname lookup helper exist?
    p("'voxel_remesh' in dir(bpy.ops.object)",
      "voxel_remesh" in dir(bpy.ops.object))

    # --- poll behavior --------------------------------------------------------
    p("voxel_remesh.poll() no active object",
      bpy.ops.object.voxel_remesh.poll())
    cube = new_cube("PollCube")
    make_active(cube)
    p("voxel_remesh.poll() mesh active, object mode",
      bpy.ops.object.voxel_remesh.poll())
    bpy.ops.object.mode_set(mode='EDIT')
    p("voxel_remesh.poll() in edit mode",
      bpy.ops.object.voxel_remesh.poll())
    bpy.ops.object.mode_set(mode='OBJECT')

    # --- zero voxel size on the destructive op --------------------------------
    cube.data.remesh_voxel_size = 0.0
    try:
        ret = bpy.ops.object.voxel_remesh()
        p("voxel_remesh with remesh_voxel_size=0.0", ret)
        p("verts after zero-size attempt", len(cube.data.vertices))
    except Exception as exc:
        p("voxel_remesh with size=0.0 RAISED",
          type(exc).__name__ + ": " + str(exc).strip()[:100])

    # --- safe-add timing: show_viewport=False in the same execution ----------
    tgt = new_cube("SafeAddCube")
    sub = tgt.modifiers.new("Subdiv", 'SUBSURF')
    sub.levels = 2
    make_active(tgt)
    # force one evaluation so the depsgraph is warm
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    mod = tgt.modifiers.new("Remesh", 'REMESH')
    mod.show_viewport = False           # same execution, before any pull
    mod.show_render = False
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    ev = tgt.evaluated_get(deps)
    ev_me = ev.to_mesh()
    p("evaluated verts after safe-add (98 => remesh never ran)",
      len(ev_me.vertices))
    ev.to_mesh_clear()

    # --- bound_box semantics ---------------------------------------------------
    bb_orig = [tuple(round(c, 4) for c in v) for v in tgt.bound_box]
    ev = tgt.evaluated_get(deps)
    bb_ev = [tuple(round(c, 4) for c in v) for v in ev.bound_box]
    p("original obj.bound_box min/max x",
      (min(v[0] for v in bb_orig), max(v[0] for v in bb_orig)))
    p("evaluated obj.bound_box min/max x (subsurf shrinks corners)",
      (min(v[0] for v in bb_ev), max(v[0] for v in bb_ev)))

    # Mesh-level bounds: does Mesh expose anything, or verts only?
    p("Mesh has bound_box attr", hasattr(tgt.data, "bound_box"))

    # numpy availability + foreach_get pattern used by siblings
    try:
        import numpy as np
        n = len(tgt.data.vertices)
        arr = np.empty(n * 3, dtype=np.float32)
        tgt.data.vertices.foreach_get("co", arr)
        arr = arr.reshape(n, 3)
        p("numpy foreach_get bounds of base cube",
          (tuple(arr.min(axis=0)), tuple(arr.max(axis=0))))
    except Exception as exc:
        p("numpy foreach_get RAISED",
          type(exc).__name__ + ": " + str(exc).strip()[:80])

    # --- modifier_add operator path (menu path) --------------------------------
    fresh = new_cube("MenuAddCube")
    make_active(fresh)
    try:
        ret = bpy.ops.object.modifier_add(type='REMESH')
        p("bpy.ops.object.modifier_add(type='REMESH')", ret)
        m = fresh.modifiers[-1]
        p("menu-added modifier (mode, voxel_size, show_viewport)",
          (m.mode, round(m.voxel_size, 4), m.show_viewport))
    except Exception as exc:
        p("modifier_add RAISED",
          type(exc).__name__ + ": " + str(exc).strip()[:80])

    # --- surface area helpers ---------------------------------------------------
    # Does Mesh expose polygon areas cheaply via foreach_get("area")?
    try:
        import numpy as np
        npoly = len(fresh.data.polygons)
        areas = np.empty(npoly, dtype=np.float32)
        fresh.data.polygons.foreach_get("area", areas)
        p("polygons.foreach_get('area') total (cube 2m => 24.0)",
          float(areas.sum()))
    except Exception as exc:
        p("polygons.foreach_get('area') RAISED",
          type(exc).__name__ + ": " + str(exc).strip()[:80])

    # --- shape keys: does the destructive op see them? (informational) --------
    sk = new_cube("ShapeKeyCube")
    sk.shape_key_add(name="Basis")
    key = sk.shape_key_add(name="Big")
    for pt in key.data:
        pt.co = pt.co * 3.0
    key.value = 1.0
    sk.active_shape_key_index = 1
    sk.data.remesh_voxel_size = 0.1
    make_active(sk)
    try:
        ret = bpy.ops.object.voxel_remesh()
        p("voxel_remesh with shape key (2648 => used basis; ~7900 => "
          "used deformed)", (ret, len(sk.data.vertices)))
    except Exception as exc:
        p("voxel_remesh with shape key RAISED",
          type(exc).__name__ + ": " + str(exc).strip()[:100])

    print("PROBE_02_DONE")


try:
    main()
except Exception:
    traceback.print_exc()
    print("PROBE_02_CRASHED")
