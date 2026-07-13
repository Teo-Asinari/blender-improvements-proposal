# SPDX-License-Identifier: GPL-2.0-or-later
"""Kiln-normal import into a pre-existing Impasto stack."""

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
    import impasto
    import kiln
    from impasto import engine, model
    from kiln import baking
    impasto.register()
    kiln.register()
    try:
        bpy.ops.mesh.primitive_cube_add()
        low = bpy.context.object
        low.name = "InteropLow"
        low.data.uv_layers.new(name="BakeUV")
        active_uv_name = low.data.uv_layers.active.name
        check("pre-existing Impasto stack created",
              bpy.ops.impasto.stack_init() == {'FINISHED'})
        check("pre-existing paint layer created",
              bpy.ops.impasto.layer_add(layer_type='PAINT') == {'FINISHED'})
        mat = low.active_material
        tree = engine.find_stack_for_material(mat)
        active_uid = tree.impasto.active_layer_uid

        image = baking.ensure_bake_image(low.name, 'NORMAL', 16)
        mat, tex = baking.ensure_material_target(low, image)
        nt = mat.node_tree
        principled = next(n for n in nt.nodes
                          if n.bl_idname == 'ShaderNodeBsdfPrincipled')

        # Simulate the damaging behavior from old Kiln: direct ownership of
        # Principled Normal after Impasto had already installed its group.
        old_nm = nt.nodes.new("ShaderNodeNormalMap")
        old_nm.name = baking.NORMAL_MAP_NODE_NAME
        nt.links.new(tex.outputs["Color"], old_nm.inputs["Color"])
        nt.links.new(old_nm.outputs["Normal"], principled.inputs["Normal"])
        check("precondition: old direct link bypasses Impasto",
              principled.inputs["Normal"].links[0].from_node == old_nm)

        check("Impasto repair operator is available",
              bpy.ops.impasto.import_kiln_normal.poll())
        check("repair operator imports existing Kiln bake",
              bpy.ops.impasto.import_kiln_normal() == {'FINISHED'})
        check("Kiln wiring integration is idempotent",
              baking.wire_normal_map(mat, tex, low))
        imported = tree.impasto.layers[-1]
        normal_binding = next((b for b in imported.bindings
                               if b.name == 'normal'), None)
        check("baked normal becomes bottom stack layer",
              imported.label == "Kiln Baked Normal"
              and normal_binding is not None
              and normal_binding.image_name == image.name)
        check("re-import creates no duplicate baseline",
              sum(1 for layer in tree.impasto.layers
                  if layer.label == "Kiln Baked Normal") == 1)
        check("import uses the low-poly active UV",
              imported.uv_map == active_uv_name,
              "imported=%r active=%r" % (
                  imported.uv_map,
                  low.data.uv_layers.active.name
                  if low.data.uv_layers.active else None))
        check("existing active paint layer remains active",
              tree.impasto.active_layer_uid == active_uid)
        stack_node = nt.nodes.get(model.n_material_stack())
        check("Impasto regains sole ownership of Principled Normal",
              principled.inputs["Normal"].is_linked
              and principled.inputs["Normal"].links[0].from_node
              == stack_node)

        # The original paint layer must still activate after repair.
        check("existing paint layer still activates",
              bpy.ops.impasto.paint_activate() == {'FINISHED'}
              and low.mode == 'TEXTURE_PAINT')
        bpy.ops.object.mode_set(mode='OBJECT')

        check("removing Impasto restores Kiln baked normal",
              bpy.ops.impasto.stack_remove() == {'FINISHED'}
              and principled.inputs["Normal"].is_linked
              and principled.inputs["Normal"].links[0].from_node.name
              == baking.NORMAL_MAP_NODE_NAME)
    finally:
        kiln.unregister()
        impasto.unregister()


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

if FAILURES:
    print("IMPASTO_INTEROP_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("IMPASTO_INTEROP_TESTS_PASSED")
