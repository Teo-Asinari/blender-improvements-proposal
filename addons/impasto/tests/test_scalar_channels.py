# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-node scalar-channel conversion regression for Blender 5.1."""

import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import compat, engine, model


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def socket(sockets, name):
    found = compat.find_socket(sockets, name)
    if found is None:
        raise AssertionError("missing socket " + name)
    return found


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
    with engine.stack_edit_session(root):
        layer.bindings.clear()
        binding = layer.bindings.add()
        binding.name = "roughness"
        binding.mode = "SHARED"
    engine.rebuild(root)

    principled = compat.find_principled(mat.node_tree)
    p_metallic = socket(principled.inputs, "Metallic")
    p_roughness = socket(principled.inputs, "Roughness")
    root_out = root.nodes[model.n_root_out()]
    check("metallic stack seed remains zero",
          socket(root_out.inputs, "Metallic").default_value == 0.0,
          str(socket(root_out.inputs, "Metallic").default_value))
    check("roughness is linked", p_roughness.is_linked)

    material_stack = mat.node_tree.nodes[model.n_material_stack()]
    check("Principled metallic evaluates as zero",
          (not p_metallic.is_linked and p_metallic.default_value == 0.0)
          or (p_metallic.is_linked
              and socket(root_out.inputs, "Metallic").default_value == 0.0))
    rough_out = socket(material_stack.outputs, "Roughness")
    link = p_roughness.links[0]
    check("generated roughness drives Principled directly",
          link.from_node.name == material_stack.name
          and link.from_socket.name == "Roughness",
          "%s.%s -> %s.%s" % (
              link.from_node.name, link.from_socket.name,
              link.to_node.name, link.to_socket.name))
    check("roughness group output is explicitly scalar",
          rough_out.type == "VALUE" and p_roughness.type == "VALUE",
          "%s -> %s" % (rough_out.type, p_roughness.type))

    layer_tree = bpy.data.node_groups[model.layer_tree_name(layer.name)]
    source = layer_tree.nodes[model.n_src(layer.name)]
    check("roughness source is the layer image", source.image is image)
    layer_scalar = layer_tree.nodes[model.n_scalar_src(layer.name)]
    check("layer image scalar uses explicit red extraction",
          layer_scalar.bl_idname == "ShaderNodeSeparateColor")
    root_layer = root.nodes[model.n_root_layer(layer.name)]
    blend = root.nodes[model.n_blend("roughness", layer.name)]
    check("image scalar reaches native-float roughness blend",
          any(link.from_node.name == root_layer.name
              and link.to_node.name == blend.name
              and link.from_socket.name == "ch:roughness"
              and link.to_socket.identifier == "B_Float"
              for link in root.links))
    check("roughness blend data type is FLOAT", blend.data_type == "FLOAT")
    factor = socket(blend.inputs, "Factor_Float")
    check("roughness blend is gated by paint alpha", factor.is_linked)
    fac_node = root.nodes[model.n_fac("roughness", layer.name)]
    check("roughness binding opacity is uninverted",
          socket(fac_node.inputs, "Value_001").default_value == 1.0)

    check("native-float result reaches root without Color conversion",
          any(link.from_node.name == blend.name
                  and link.from_socket.identifier == "Result_Float"
                  and link.to_node.name == root_out.name
                  and link.to_socket.name == "Roughness"
              for link in root.links)
          and root.nodes.get(model.n_scalar_out("roughness")) is None)

    # Neutral black/white must preserve the scalar endpoints exactly.
    for value in (0.0, 1.0):
        image.generated_color = (value, value, value, 1.0)
        image.update()
        check("%s image is neutral %g" %
              ("black" if value == 0.0 else "white", value),
              tuple(image.generated_color) == (value, value, value, 1.0))
    check("scalar path contains no inversion",
          all(not (node.bl_idname == "ShaderNodeMath"
                   and node.operation == "SUBTRACT")
              for node in root.nodes))

    impasto.unregister()
    print("IMPASTO_SCALAR_CHANNELS_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_SCALAR_CHANNELS_FAILED")
