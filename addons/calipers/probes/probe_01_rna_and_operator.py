# SPDX-License-Identifier: GPL-2.0-or-later
"""Probe 01: verify the research doc's RNA/operator claims on the real
5.1.2 binary (the doc was written without a live Blender).

Claims under test:
- Mesh.remesh_voxel_size default 0.1, adaptivity 0.0 (and RNA min/subtype)
- RemeshModifier voxel_size default 0.1, adaptivity 0.0, mode enum + default
- bpy.types.OBJECT_OT_voxel_remesh exists
- the destructive operator uses ORIGINAL mesh data (ignores modifiers)
- the voxel size is OBJECT-space (unapplied scale does not change topology)
- behavior on empty mesh / zero-extent (flat) mesh
- modifiers.new() picks up RNA defaults; show_viewport default True
- depsgraph evaluated_get() with a disabled trailing Remesh modifier
  yields the geometry ENTERING that modifier

Prints PROBE_01_DONE at the end.
"""

import traceback

import bpy


def p(label, value):
    print("  PROBE %-46s %r" % (label + ":", value))


def rna_prop(struct, name):
    return struct.bl_rna.properties[name]


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # --- Mesh remesh RNA ---------------------------------------------------
    for pname in ("remesh_voxel_size", "remesh_voxel_adaptivity"):
        pr = rna_prop(bpy.types.Mesh, pname)
        p("Mesh.%s default" % pname, pr.default)
        p("Mesh.%s hard min/max" % pname, (pr.hard_min, pr.hard_max))
        p("Mesh.%s soft min/max" % pname, (pr.soft_min, pr.soft_max))
        p("Mesh.%s subtype/unit" % pname, (pr.subtype, pr.unit))
    p("Mesh.remesh_mode enum",
      [(i.identifier, i.name)
       for i in rna_prop(bpy.types.Mesh, "remesh_mode").enum_items]
      if "remesh_mode" in bpy.types.Mesh.bl_rna.properties else "ABSENT")

    # --- RemeshModifier RNA --------------------------------------------------
    for pname in ("voxel_size", "adaptivity"):
        pr = rna_prop(bpy.types.RemeshModifier, pname)
        p("RemeshModifier.%s default" % pname, pr.default)
        p("RemeshModifier.%s hard min/max" % pname, (pr.hard_min, pr.hard_max))
        p("RemeshModifier.%s subtype/unit" % pname, (pr.subtype, pr.unit))
    mode = rna_prop(bpy.types.RemeshModifier, "mode")
    p("RemeshModifier.mode items",
      [i.identifier for i in mode.enum_items])
    p("RemeshModifier.mode default", mode.default)
    p("Modifier.show_viewport default",
      rna_prop(bpy.types.Modifier, "show_viewport").default)

    # --- operator type exists (hasattr(bpy.ops...) is useless) -------------
    p("OBJECT_OT_voxel_remesh registered",
      hasattr(bpy.types, "OBJECT_OT_voxel_remesh"))

    # --- build a reference cube and remesh it -------------------------------
    def new_cube(name, size=2.0):
        import bmesh
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

    cube = new_cube("RefCube")
    cube.data.remesh_voxel_size = 0.1
    make_active(cube)
    try:
        ret = bpy.ops.object.voxel_remesh()
        p("voxel_remesh on cube (object mode, headless)", ret)
        p("cube verts after remesh v=0.1", len(cube.data.vertices))
    except Exception as exc:
        p("voxel_remesh on cube RAISED", str(exc).strip())

    # --- object-space claim: unapplied scale must not change topology -------
    scaled = new_cube("ScaledCube")
    scaled.scale = (2.0, 2.0, 2.0)
    scaled.data.remesh_voxel_size = 0.1
    make_active(scaled)
    bpy.context.view_layer.update()
    try:
        bpy.ops.object.voxel_remesh()
        p("scaled(2x) cube verts after remesh v=0.1",
          len(scaled.data.vertices))
        p("scaled cube scale after remesh (auto-applied?)",
          tuple(scaled.scale))
    except Exception as exc:
        p("voxel_remesh on scaled cube RAISED", str(exc).strip())

    # --- geometry source: does the op see modifier output? ------------------
    modded = new_cube("ModdedCube")
    sub = modded.modifiers.new("Subdiv", 'SUBSURF')
    sub.levels = 2
    modded.data.remesh_voxel_size = 0.1
    make_active(modded)
    bpy.context.view_layer.update()
    try:
        bpy.ops.object.voxel_remesh()
        p("cube+subsurf verts after remesh (op saw base mesh if ~= "
          "RefCube)", len(modded.data.vertices))
        p("modifier still present after op",
          [m.type for m in modded.modifiers])
    except Exception as exc:
        p("voxel_remesh on modified cube RAISED", str(exc).strip())

    # --- empty mesh ----------------------------------------------------------
    empty_me = bpy.data.meshes.new("EmptyMesh")
    empty_ob = bpy.data.objects.new("EmptyObj", empty_me)
    bpy.context.collection.objects.link(empty_ob)
    make_active(empty_ob)
    try:
        ret = bpy.ops.object.voxel_remesh()
        p("voxel_remesh on EMPTY mesh", ret)
    except Exception as exc:
        p("voxel_remesh on EMPTY mesh RAISED", str(exc).strip())

    # --- zero-extent (flat plane) -------------------------------------------
    import bmesh
    plane_me = bpy.data.meshes.new("PlaneMesh")
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0)
    bm.to_mesh(plane_me)
    bm.free()
    plane = bpy.data.objects.new("Plane", plane_me)
    bpy.context.collection.objects.link(plane)
    plane.data.remesh_voxel_size = 0.1
    make_active(plane)
    try:
        ret = bpy.ops.object.voxel_remesh()
        p("voxel_remesh on FLAT plane", ret)
        p("plane verts after", len(plane.data.vertices))
    except Exception as exc:
        p("voxel_remesh on FLAT plane RAISED", str(exc).strip())

    # --- Mesh.remesh_voxel_size clamping ------------------------------------
    clamp_me = bpy.data.meshes.new("ClampMesh")
    try:
        clamp_me.remesh_voxel_size = 0.0
        p("remesh_voxel_size after assigning 0.0",
          clamp_me.remesh_voxel_size)
    except Exception as exc:
        p("assigning remesh_voxel_size=0.0 RAISED", str(exc).strip())

    # --- modifiers.new defaults ---------------------------------------------
    fresh = new_cube("FreshCube")
    mod = fresh.modifiers.new("Remesh", 'REMESH')
    p("modifiers.new REMESH mode", mod.mode)
    p("modifiers.new REMESH voxel_size", mod.voxel_size)
    p("modifiers.new REMESH adaptivity", mod.adaptivity)
    p("modifiers.new REMESH show_viewport", mod.show_viewport)
    try:
        mod.voxel_size = 0.0
        p("modifier voxel_size after assigning 0.0", mod.voxel_size)
    except Exception as exc:
        p("assigning modifier voxel_size=0.0 RAISED", str(exc).strip())

    # --- depsgraph: disabled trailing modifier => entering geometry ---------
    stacked = new_cube("StackedCube")
    sub = stacked.modifiers.new("Subdiv", 'SUBSURF')
    sub.levels = 2
    rm = stacked.modifiers.new("Remesh", 'REMESH')
    rm.mode = 'VOXEL'
    rm.show_viewport = False
    make_active(stacked)
    deps = bpy.context.evaluated_depsgraph_get()
    ev = stacked.evaluated_get(deps)
    ev_me = ev.to_mesh()
    p("evaluated verts, subsurf lv2 + DISABLED trailing remesh "
      "(expect 98 = subsurf output)", len(ev_me.vertices))
    p("evaluated bound_box present", len(ev.bound_box) == 8)
    ev.to_mesh_clear()

    # enable the remesh and re-evaluate: does evaluated include remesh?
    rm.show_viewport = True
    rm.voxel_size = 0.2
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    ev = stacked.evaluated_get(deps)
    ev_me = ev.to_mesh()
    p("evaluated verts with remesh ENABLED (differs => modifier ran)",
      len(ev_me.vertices))
    ev.to_mesh_clear()

    print("PROBE_01_DONE")


try:
    main()
except Exception:
    traceback.print_exc()
    print("PROBE_01_CRASHED")
