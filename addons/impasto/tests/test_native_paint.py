# SPDX-License-Identifier: GPL-2.0-or-later
"""Native texture-paint target activation in real Blender.

This is deliberately a black-box operator test: it checks the Blender state a
brush uses, rather than private implementation details in ``impasto.paint``.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

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


def activate():
    return bpy.ops.impasto.paint_activate()


def active_source_node(layer):
    """The generated image node for the layer's first binding: the
    per-binding node when the binding owns its canvas (schema 2), the
    legacy layer-canvas node otherwise."""
    tree = bpy.data.node_groups.get(model.layer_tree_name(layer.name))
    if tree is None:
        return None, None
    binding = layer.bindings[0] if len(layer.bindings) else None
    name = (model.n_binding_src(layer.name, binding.name)
            if binding is not None and binding.image_name
            else model.n_src(layer.name))
    return tree, tree.nodes.get(name)


try:
    impasto.register()
    fake_shading = SimpleNamespace(type="SOLID")
    fake_area = SimpleNamespace(
        type="VIEW_3D",
        spaces=SimpleNamespace(active=SimpleNamespace(shading=fake_shading)))
    check("active Solid viewport switches to Material Preview",
          paint.maybe_switch_material_preview(
              SimpleNamespace(area=fake_area), enabled=True)
          and fake_shading.type == "MATERIAL")
    fake_shading.type = "SOLID"
    check("material-preview opt-out preserves Solid shading",
          not paint.maybe_switch_material_preview(
              SimpleNamespace(area=fake_area), enabled=False)
          and fake_shading.type == "SOLID")
    other_shading = SimpleNamespace(type="SOLID")
    check("non-3D areas are never changed",
          not paint.maybe_switch_material_preview(
              SimpleNamespace(area=SimpleNamespace(
                  type="IMAGE_EDITOR", spaces=SimpleNamespace(
                      active=SimpleNamespace(shading=other_shading)))),
              enabled=True)
          and other_shading.type == "SOLID")
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    uv = obj.data.uv_layers.new(name="PaintUV")
    obj.data.uv_layers.active = uv

    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("add paint", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {"FINISHED"})

    mat = obj.active_material
    tree = engine.find_stack_for_material(mat)
    paint_layer = tree.impasto.active_layer()
    paint_uid = paint_layer.name
    image = bpy.data.images.get(paint_layer.image_name)
    check("paint image exists", image is not None)
    check("paint layer remembers active UV", paint_layer.uv_map == "PaintUV")

    # Exercise repair, not just the happy path. Base Color is a color channel.
    wrong = compat.resolve_colorspace(image, "Non-Color")
    image.colorspace_settings.name = wrong
    check("colorspace deliberately damaged",
          image.colorspace_settings.name == wrong)

    material_before = obj.active_material
    result = activate()
    check("paint activation finishes", result == {"FINISHED"}, str(result))
    settings = bpy.context.scene.tool_settings.image_paint
    check("image paint uses image mode", settings.mode == "IMAGE")
    check("active canvas is paint image", settings.canvas is image)
    check("base-color colorspace repaired",
          image.colorspace_settings.name
          == compat.resolve_colorspace(image, "sRGB"),
          image.colorspace_settings.name)
    check("active material preserved", obj.active_material is material_before)
    check("named layer UV made active",
          obj.data.uv_layers.active is not None
          and obj.data.uv_layers.active.name == "PaintUV")

    layer_tree, source = active_source_node(paint_layer)
    check("paint source node exists", source is not None)
    check("paint source node selects layer image", source.image is image)
    check("paint source node selected", source.select)
    # Blender 5.1 background mode does not retain nodes.active on a node tree
    # that is not displayed by an editor.  The GUI checklist covers that
    # editor-specific state; selected+image is deterministic headlessly.

    check("operator enters texture paint", obj.mode == "TEXTURE_PAINT",
          obj.mode)
    check("context reports paint texture mode",
          bpy.context.mode == "PAINT_TEXTURE", bpy.context.mode)
    check("mesh has no missing paint prerequisites",
          not settings.missing_uvs and not settings.missing_materials
          and not settings.missing_texture)

    check("add fill", bpy.ops.impasto.layer_add(
        layer_type="FILL") == {"FINISHED"})
    fill_layer = tree.impasto.active_layer()
    check("fill is active", fill_layer.layer_type == "FILL")
    canvas_before = settings.canvas
    check("non-paint activation is unavailable",
          not bpy.ops.impasto.paint_activate.poll())
    check("non-paint selection preserves canvas", settings.canvas is canvas_before)

    # Dedicated Height Detail layers start from neutral mid-gray and expose
    # additive Raise/Lower brush modes so repeated strokes build relief.
    check("add dedicated height detail", bpy.ops.impasto.layer_add(
        layer_type="PAINT", channel_key="height") == {"FINISHED"})
    detail_layer = tree.impasto.active_layer()
    detail_image = bpy.data.images[detail_layer.image_name]
    check("height detail is single-channel",
          [b.name for b in detail_layer.bindings] == ["height"])
    check("height detail image is neutral opaque mid-gray",
          tuple(detail_image.generated_color) == (0.5, 0.5, 0.5, 1.0))
    check("height detail image is Non-Color",
          detail_image.colorspace_settings.name
          == compat.resolve_colorspace(detail_image, "Non-Color"))
    check("raise detail activates", bpy.ops.impasto.detail_paint(
        direction="RAISE") == {"FINISHED"})
    brush = settings.brush
    check("raise detail configures accumulating ADD brush",
          brush is not None and brush.blend == "ADD"
          and tuple(brush.color) == (1.0, 1.0, 1.0))
    check("lower detail activates", bpy.ops.impasto.detail_paint(
        direction="LOWER") == {"FINISHED"})
    check("lower detail configures accumulating SUB brush",
          brush.blend == "SUB")

    # One logical layer, separate per-channel canvases: a second channel
    # binding creates its own image (never a shared multi-channel canvas).
    check("second channel binding accepted",
          bpy.ops.impasto.binding_add(channel_key="roughness")
          == {"FINISHED"})
    rough_binding = detail_layer.bindings.get("roughness")
    check("second channel owns a dedicated canvas",
          rough_binding is not None and rough_binding.image_name
          and rough_binding.image_name != detail_layer.image_name)
    rough_image = bpy.data.images.get(rough_binding.image_name)
    check("dedicated canvas matches the layer's resolution",
          rough_image is not None
          and tuple(rough_image.size) == tuple(detail_image.size))
    check("dedicated scalar canvas is Non-Color",
          rough_image.colorspace_settings.name
          == compat.resolve_colorspace(rough_image, "Non-Color"))

    # Re-select by stable uid and prove the target can be restored from stored
    # layer/image/UV state after a real .blend save and reopen.
    paint_index = next(i for i, layer in enumerate(tree.impasto.layers)
                       if layer.name == paint_uid)
    tree.impasto.active_index = paint_index
    tree_name = tree.name
    image_name = image.name
    mat_name = mat.name
    path = os.path.join(tempfile.gettempdir(),
                        "impasto_native_paint_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False)
    check("paint blend saved", os.path.exists(path))
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)

    obj = bpy.context.object
    mat = bpy.data.materials.get(mat_name)
    tree = bpy.data.node_groups.get(tree_name)
    image = bpy.data.images.get(image_name)
    check("paint target data persisted",
          obj is not None and mat is not None and tree is not None
          and image is not None)
    check("active paint uid persisted",
          tree.impasto.active_layer() is not None
          and tree.impasto.active_layer().image_name == image_name)
    result = activate()
    check("paint target reactivates after reopen",
          result == {"FINISHED"}, str(result))
    check("reopened canvas restored",
          bpy.context.scene.tool_settings.image_paint.canvas is image)

    impasto.unregister()
    print("IMPASTO_NATIVE_PAINT_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_NATIVE_PAINT_FAILED")
