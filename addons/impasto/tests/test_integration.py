# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-Blender Phase 1 lifecycle checks."""

import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import engine, model


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


try:
    impasto.register()
    check("package registration",
          hasattr(bpy.types.ShaderNodeTree, "impasto"))
    check("metadata", impasto.bl_info["version"] == (0, 3, 3))

    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    check("stack init",
          bpy.ops.impasto.stack_init(
              template="PRINCIPLED_STANDARD") == {"FINISHED"})
    mat = obj.active_material
    tree = engine.find_stack_for_material(mat)
    check("stack discoverable", tree is not None)
    check("material group exists",
          mat.node_tree.nodes.get(model.n_material_stack()) is not None)
    check("five standard channels", len(tree.impasto.channels) == 5)
    check("initial reconcile clean",
          engine._last_deltas is not None
          and not engine._last_deltas.errors,
          str(engine._last_deltas))

    check("add fill",
          bpy.ops.impasto.layer_add(layer_type="FILL") == {"FINISHED"})
    check("add paint",
          bpy.ops.impasto.layer_add(layer_type="PAINT") == {"FINISHED"})
    check("requested layer types",
          sorted(ly.layer_type for ly in tree.impasto.layers)
          == ["FILL", "PAINT"])
    check("layer reconcile clean", not engine._last_deltas.errors,
          str(engine._last_deltas))

    d1 = engine.rebuild(tree)
    d2 = engine.reconcile_stack(tree)
    check("rebuild clean", not d1.errors, str(d1))
    check("idempotent second reconcile", d2.total() == 0, str(d2))

    check("remove stack",
          bpy.ops.impasto.stack_remove() == {"FINISHED"})
    check("stack removed", engine.find_stack_for_material(mat) is None)
    impasto.unregister()
    check("package unregistration",
          not hasattr(bpy.types.ShaderNodeTree, "impasto"))
    print("IMPASTO_INTEGRATION_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_INTEGRATION_FAILED")
