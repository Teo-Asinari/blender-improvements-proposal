# SPDX-License-Identifier: GPL-2.0-or-later
"""Save/reopen persistence and load-time self-heal in real Blender."""

import os
import sys
import tempfile
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
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    check("stack init", bpy.ops.impasto.stack_init(template="PRINCIPLED_STANDARD") == {"FINISHED"})
    mat = obj.active_material
    tree = engine.find_stack_for_material(mat)
    check("stack state available", tree is not None and len(tree.impasto.channels) == 5)
    # Build representative stored state directly. Operator undo semantics are
    # covered separately; this test isolates serialization and load_post.
    with engine.stack_edit_session(tree):
        fill = tree.impasto.layers.add()
        fill.name = "aa11bb22"
        fill.label = "Persisted Fill"
        fill.layer_type = "FILL"
        binding = fill.bindings.add()
        binding.name = "base_color"
        binding.mode = "COLOR"
        paint = tree.impasto.layers.add()
        paint.name = "c3a91f02"
        paint.label = "Persisted Paint"
        paint.layer_type = "PAINT"
        binding = paint.bindings.add()
        binding.name = "roughness"
        canvas = bpy.data.images.new("Persisted Roughness Canvas", 8, 8,
                                     alpha=True)
        binding.image_name = canvas.name
    tree_name = tree.name
    uids = tuple(ly.name for ly in tree.impasto.layers)
    types = tuple(ly.layer_type for ly in tree.impasto.layers)
    bindings = tuple(tuple(b.name for b in ly.bindings) for ly in tree.impasto.layers)
    binding_images = tuple(tuple(b.image_name for b in ly.bindings)
                           for ly in tree.impasto.layers)
    mat_name = mat.name

    victim_name = model.n_root_out()
    victim = tree.nodes.get(victim_name)
    check("self-heal victim exists before tamper", victim is not None)
    tree.nodes.remove(victim)
    check("victim removed", tree.nodes.get(victim_name) is None)
    path = os.path.join(tempfile.gettempdir(), "impasto_persistence_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False)
    check("blend saved", os.path.exists(path))
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)

    mat = bpy.data.materials.get(mat_name)
    tree = bpy.data.node_groups.get(tree_name)
    check("material persisted", mat is not None)
    check("stack tree persisted", tree is not None and tree.impasto.is_stack)
    check("stack rediscovered", engine.find_stack_for_material(mat) is tree)
    check("UID order persisted", tuple(ly.name for ly in tree.impasto.layers) == uids)
    check("layer types persisted", tuple(ly.layer_type for ly in tree.impasto.layers) == types)
    check("bindings persisted", tuple(tuple(b.name for b in ly.bindings) for ly in tree.impasto.layers) == bindings)
    check("per-binding images persisted",
          tuple(tuple(b.image_name for b in ly.bindings)
                for ly in tree.impasto.layers) == binding_images)
    check("load handler self-healed removed node", tree.nodes.get(victim_name) is not None)
    check("self-healed graph converges cleanly", not engine.reconcile_stack(tree).errors)
    print("IMPASTO_PERSISTENCE_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_PERSISTENCE_FAILED")
