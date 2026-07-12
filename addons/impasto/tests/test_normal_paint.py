# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-node tangent-normal painting regression for Blender 5.1."""

import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import compat, engine, model, paint


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def socket(sockets, name):
    found = compat.find_socket(sockets, name)
    if found is None:
        raise AssertionError("missing socket " + name)
    return found


def linked(tree, from_node, from_socket, to_node, to_socket):
    return any(
        link.from_node.name == from_node
        and link.from_socket.name == from_socket
        and link.to_node.name == to_node
        and (link.to_socket.name == to_socket
             or link.to_socket.identifier == to_socket)
        for link in tree.links
    )


try:
    impasto.register()
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {"FINISHED"})

    mat = obj.active_material
    root = engine.find_stack_for_material(mat)
    layer = root.impasto.active_layer()
    image = bpy.data.images[layer.image_name]

    # A normal-only layer must be a Non-Color native paint canvas and must
    # reach Principled only after tangent-space decoding.
    with engine.stack_edit_session(root):
        layer.bindings.clear()
        binding = layer.bindings.add()
        binding.name = "normal"
        binding.mode = "SHARED"
    engine.rebuild(root)
    image.colorspace_settings.name = compat.resolve_colorspace(image, "sRGB")
    repaired = paint.activate_paint_target(bpy.context, layer)
    check("normal canvas colorspace repaired", repaired)
    check("normal canvas is Non-Color",
          image.colorspace_settings.name
          == compat.resolve_colorspace(image, "Non-Color"),
          image.colorspace_settings.name)
    check("normal canvas is active",
          bpy.context.scene.tool_settings.image_paint.canvas is image)

    layer_tree = bpy.data.node_groups[model.layer_tree_name(layer.name)]
    source = layer_tree.nodes[model.n_src(layer.name)]
    check("normal source is paint image", source.image is image)
    check("paint image feeds layer normal output",
          linked(layer_tree, source.name, "Color", model.n_out(layer.name),
                 "ch:normal"))

    decoder = root.nodes[model.n_normal_map()]
    check("normal decoder is Normal Map node",
          decoder.bl_idname == "ShaderNodeNormalMap")
    check("normal decoder uses tangent space", decoder.space == "TANGENT")
    normal_blend = root.nodes[model.n_blend("normal", layer.name)]
    check("encoded normal blend feeds decoder",
          linked(root, normal_blend.name, "Result", decoder.name, "Color")
          or linked(root, normal_blend.name, "Result_Color",
                    decoder.name, "Color"))
    root_out = root.nodes[model.n_root_out()]
    check("normal-only decode feeds root Normal",
          linked(root, decoder.name, "Normal", root_out.name, "Normal"))

    principled = compat.find_principled(mat.node_tree)
    material_stack = mat.node_tree.nodes[model.n_material_stack()]
    check("root Normal feeds Principled Normal",
          linked(mat.node_tree, material_stack.name, "Normal",
                 principled.name, "Normal"))

    # Adding height must compose, not replace or compete with, the decoded
    # normal: NormalMap.Normal -> Bump.Normal, Bump.Normal -> root output.
    with engine.stack_edit_session(root):
        binding = layer.bindings.add()
        binding.name = "height"
        binding.mode = "SHARED"
    engine.rebuild(root)
    bump = root.nodes[model.n_bump()]
    check("height uses Bump node", bump.bl_idname == "ShaderNodeBump")
    check("decoded normal feeds Bump Normal",
          linked(root, decoder.name, "Normal", bump.name, "Normal"))
    check("height scalar feeds Bump Height",
          socket(bump.inputs, "Height").is_linked)
    check("combined Bump output feeds root Normal",
          linked(root, bump.name, "Normal", root_out.name, "Normal"))
    check("decoder no longer bypasses Bump",
          not linked(root, decoder.name, "Normal", root_out.name, "Normal"))
    check("Principled has exactly one Normal link",
          len(socket(principled.inputs, "Normal").links) == 1)

    impasto.unregister()
    print("IMPASTO_NORMAL_PAINT_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_NORMAL_PAINT_FAILED")
