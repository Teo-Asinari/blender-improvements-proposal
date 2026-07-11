# SPDX-License-Identifier: GPL-2.0-or-later
"""Preexisting Principled-input links survive an Impasto lifecycle."""

import sys
import traceback
from pathlib import Path
import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)
import impasto
from impasto import compat, engine


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


try:
    impasto.register()
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    mat = bpy.data.materials.new("Impasto Restore Material")
    mat.use_nodes = True
    obj.data.materials.append(mat)
    principled = compat.find_principled(mat.node_tree)
    source = mat.node_tree.nodes.new("ShaderNodeValue")
    source.name = "Preexisting Roughness"
    destination = compat.find_socket(principled.inputs, "Roughness")
    mat.node_tree.links.new(source.outputs[0], destination)
    check("preexisting link installed", destination.is_linked)
    check("stack init", bpy.ops.impasto.stack_init() == {"FINISHED"})
    check("stack discoverable", engine.find_stack_for_material(mat) is not None)
    check("Impasto displaced old link", destination.is_linked and destination.links[0].from_node.name != source.name)
    check("stack remove", bpy.ops.impasto.stack_remove() == {"FINISHED"})
    check("original link restored", destination.is_linked and destination.links[0].from_node.name == source.name,
          repr([(ln.from_node.name, ln.from_socket.name) for ln in destination.links]))
    print("IMPASTO_RESTORE_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_RESTORE_FAILED")
