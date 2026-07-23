# SPDX-License-Identifier: GPL-2.0-or-later
"""Rebuild migration contract for generated RNM normal-stack nodes."""

import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import engine, model


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  ok  " + label)


try:
    impasto.register()
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    bpy.context.object.data.uv_layers.new(name="UVMap")
    check("stack init",
          bpy.ops.impasto.stack_init(
              template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("first paint layer",
          bpy.ops.impasto.layer_add(layer_type="PAINT") == {"FINISHED"})
    root = engine.find_stack_for_material(bpy.context.object.active_material)
    first = root.impasto.active_layer()
    check("second paint layer",
          bpy.ops.impasto.layer_add(layer_type="PAINT") == {"FINISHED"})
    second = root.impasto.active_layer()
    with engine.stack_edit_session(root):
        for layer in (first, second):
            layer.bindings.clear()
            binding = layer.bindings.add()
            binding.name = "normal"
            binding.mode = "SHARED"
    engine.rebuild(root)

    stale_node_name = model.n_rnm(second.name, "normalize")
    stale_node = root.nodes.get(stale_node_name)
    check("RNM graph exists before simulated old-file repair",
          stale_node is not None)
    root_pointer = root.as_pointer()
    layer_trees = {
        layer.name: bpy.data.node_groups[
            model.layer_tree_name(layer.name)].as_pointer()
        for layer in (first, second)
    }
    image_names = tuple(layer.image_name for layer in (first, second))

    material = bpy.context.object.active_material
    kiln_image = bpy.data.images.new(
        "RNM Rebuild Kiln Normal", width=8, height=8, alpha=True)
    kiln_node = material.node_tree.nodes.new("ShaderNodeTexImage")
    kiln_node.name = "Kiln Bake Target"
    kiln_node.image = kiln_image
    check("Rebuild discovers loose Kiln Bake Target",
          bpy.ops.impasto.stack_rebuild() == {"FINISHED"})
    kiln_layers = [layer for layer in root.impasto.layers
                   if layer.label == "Kiln Baked Normal"]
    check("discovered Kiln target becomes bottom normal baseline",
          len(kiln_layers) == 1
          and kiln_layers[0].name == root.impasto.layers[-1].name
          and kiln_layers[0].bindings["normal"].image_name
          == kiln_image.name)

    # An old generated graph lacks the new RNM node family. Rebuild must
    # reconcile that graph in place, preserving stack/layer datablocks and
    # every paint canvas rather than recreating the user's stack.
    root.nodes.remove(stale_node)
    deltas = engine.rebuild(root)
    check("Rebuild Stack repairs missing RNM generated nodes",
          not deltas.errors and root.nodes.get(stale_node_name) is not None)
    check("Rebuild Stack preserves the root node tree",
          root.as_pointer() == root_pointer)
    check("Rebuild Stack preserves generated layer trees",
          all(bpy.data.node_groups[
              model.layer_tree_name(layer.name)].as_pointer()
              == layer_trees[layer.name] for layer in (first, second)))
    check("Rebuild Stack preserves paint canvas bindings",
          tuple(layer.image_name for layer in (first, second)) == image_names
          and all(bpy.data.images.get(name) is not None for name in image_names))
    check("post-migration reconcile is idempotent",
          engine.reconcile_stack(root).total() == 0)

    impasto.unregister()
    print("IMPASTO_RNM_REBUILD_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_RNM_REBUILD_FAILED")
