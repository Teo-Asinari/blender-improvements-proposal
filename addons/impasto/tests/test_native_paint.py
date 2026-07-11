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


def activate():
    return bpy.ops.impasto.paint_activate()


def active_source_node(layer):
    tree = bpy.data.node_groups.get(model.layer_tree_name(layer.name))
    return tree, tree.nodes.get(model.n_src(layer.name)) if tree else None


try:
    impasto.register()
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
