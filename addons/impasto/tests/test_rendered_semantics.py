# SPDX-License-Identifier: GPL-2.0-or-later
"""Rendered/evaluated PBR channel semantics in Blender 5.1.

Topology-only tests missed regressions in scalar channels.  This suite routes
the generated outputs through emission and measures EXR pixels, proving what
the shader evaluator receives.  It also verifies the derivative semantics of
Height: constant black/mid-gray/white are all flat; spatial variation creates
a perturbed normal.
"""

import math
import os
import statistics
import sys
import tempfile
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


def render_stats(tag):
    path = os.path.join(tempfile.gettempdir(),
                        "impasto_rendered_%s.exr" % tag)
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(path, check_existing=False)
    pixels = list(image.pixels)
    width, height = image.size
    # The orthographic plane fills the center.  Ignore antialiasing/background.
    values = []
    for y in range(height // 4, 3 * height // 4):
        for x in range(width // 4, 3 * width // 4):
            i = 4 * (y * width + x)
            values.append((pixels[i], pixels[i + 1], pixels[i + 2]))
    bpy.data.images.remove(image)
    return (tuple(sum(v[c] for v in values) / len(values)
                  for c in range(3)),
            tuple(statistics.pstdev(v[c] for v in values)
                  for c in range(3)))


def emission_link(tree, stack, principled, output_name):
    socket = compat.find_socket(principled.inputs, "Emission Color")
    for link in list(socket.links):
        tree.links.remove(link)
    source = stack.outputs[output_name]
    if source.type == 'VALUE':
        combine = tree.nodes.get("Impasto Render Probe Combine")
        if combine is None:
            combine = tree.nodes.new("ShaderNodeCombineColor")
            combine.name = "Impasto Render Probe Combine"
            combine.mode = 'RGB'
        for input_socket in combine.inputs[:3]:
            for link in list(input_socket.links):
                tree.links.remove(link)
            tree.links.new(source, input_socket)
        tree.links.new(combine.outputs["Color"], socket)
    else:
        tree.links.new(source, socket)
    compat.find_socket(principled.inputs,
                       "Emission Strength").default_value = 1.0


try:
    impasto.register()
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_EEVEE'
    scene.render.resolution_x = 64
    scene.render.resolution_y = 64
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.view_settings.view_transform = 'Standard'
    scene.world.color = (0.0, 0.0, 0.0)

    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    if not obj.data.uv_layers:
        obj.data.uv_layers.new(name="UVMap")
    bpy.ops.object.camera_add(location=(0.0, 0.0, 3.0))
    camera = bpy.context.object
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = 2.2
    scene.camera = camera
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    camera.select_set(False)

    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("fill add", bpy.ops.impasto.layer_add(
        layer_type="FILL") == {"FINISHED"})
    mat = obj.active_material
    root = engine.find_stack_for_material(mat)
    layer = root.impasto.active_layer()
    principled = compat.find_principled(mat.node_tree)
    stack = mat.node_tree.nodes[model.n_material_stack()]
    compat.find_socket(principled.inputs,
                       "Base Color").default_value = (0.5, 0.5, 0.5, 1.0)

    scalar_renders = {}
    for key in ("roughness", "metallic"):
        with engine.stack_edit_session(root):
            layer.bindings.clear()
            binding = layer.bindings.add()
            binding.name = key
            binding.mode = 'VALUE'
            binding.value = 0.0
        engine.rebuild(root)
        emission_link(mat.node_tree, stack, principled,
                      model.CHANNEL_MAP[key].label)
        endpoint = []
        for value in (0.0, 1.0):
            binding.value = value
            engine.uniform_flush(root)
            # Explicitly tag the dependency graph; production writes should
            # also invalidate shaders, and this keeps headless renders honest.
            root.update_tag()
            mat.node_tree.update_tag()
            mean, _ = render_stats("%s_%d" % (key, value))
            endpoint.append(sum(mean) / 3.0)
        scalar_renders[key] = endpoint
        check("%s rendered endpoint polarity is 0 -> 1" % key,
              endpoint[0] < 0.15 and endpoint[1] > 0.35,
              str(endpoint))
        target = compat.find_socket(principled.inputs,
                                    model.CHANNEL_MAP[key].socket)
        check("%s drives a VALUE Principled socket" % key,
              target.type == 'VALUE' and target.is_linked)

    # Height is a derivative field.  Constant endpoint values must all yield
    # the same flat normal; midpoint is neutral by convention and allows ADD /
    # SUB painting in both directions without clipping immediately.
    with engine.stack_edit_session(root):
        layer.bindings.clear()
        binding = layer.bindings.add()
        binding.name = "height"
        binding.mode = 'VALUE'
    engine.rebuild(root)
    emission_link(mat.node_tree, stack, principled, "Normal")
    flat = []
    for value in (0.0, 0.5, 1.0):
        binding.value = value
        engine.uniform_flush(root)
        root.update_tag()
        mat.node_tree.update_tag()
        mean, deviation = render_stats("height_flat_%s" % value)
        flat.append((mean, deviation))
    check("black/midpoint/white constant Height all remain flat",
          max(abs(flat[i][0][c] - flat[1][0][c])
                  for i in (0, 2) for c in range(3)) < 0.02,
          str(flat))

    check("height detail layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT", channel_key="height") == {"FINISHED"})
    detail = root.impasto.active_layer()
    old_image = bpy.data.images[detail.image_name]
    image = bpy.data.images.new("Impasto Height Render Probe", 32, 32,
                                alpha=True, float_buffer=True)
    image.colorspace_settings.name = compat.resolve_colorspace(
        image, "Non-Color")
    pixels = []
    for y in range(32):
        for x in range(32):
            value = 0.5 + 0.35 * math.sin(2.0 * math.pi * x / 8.0)
            pixels.extend((value, value, value, 1.0))
    image.pixels[:] = pixels
    image.update()
    with engine.stack_edit_session(root):
        # Schema 2: the binding owns the canvas; the layer slot mirrors it.
        detail.image_name = image.name
        detail.bindings["height"].image_name = image.name
    engine.rebuild(root)
    emission_link(mat.node_tree, stack, principled, "Normal")
    varied_mean, varied_deviation = render_stats("height_varied")
    flat_variation = max(flat[1][1])
    detail_variation = max(varied_deviation)
    check("spatial Height detail perturbs the rendered normal",
          detail_variation > 0.0015
          and detail_variation > flat_variation * 3.0,
          "%s vs %s" % (varied_deviation, flat[1][1]))
    check("height source is native float into Bump",
          any(link.to_node.name == model.n_bump()
              and link.to_socket.name == "Height"
              and link.from_socket.type == 'VALUE'
              for link in root.links))
    bpy.data.images.remove(old_image)

    impasto.unregister()
    print("IMPASTO_RENDERED_SEMANTICS_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_RENDERED_SEMANTICS_FAILED")
