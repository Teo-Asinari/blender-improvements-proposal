# SPDX-License-Identifier: GPL-2.0-or-later
"""One logical layer, separate per-channel canvases — real Blender.

Covers the schema-2 form: layer/binding image creation and colorspaces,
per-binding graph wiring, native per-channel canvas activation, GPU
target planning, and the 1 -> 2 legacy-canvas migration.
"""

import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import compat, engine, model, ops, paint


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def colorspace_is(image, wanted):
    return (image.colorspace_settings.name
            == compat.resolve_colorspace(image, wanted))


try:
    impasto.register()
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    uv = obj.data.uv_layers.new(name="PaintUV")
    obj.data.uv_layers.active = uv

    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {"FINISHED"})
    mat = obj.active_material
    tree = engine.find_stack_for_material(mat)
    state = tree.impasto
    check("new stacks stamp schema 2",
          state.schema_version == 2, str(state.schema_version))

    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {"FINISHED"})
    layer = state.active_layer()
    base = layer.bindings.get("base_color")
    check("initial binding owns its canvas explicitly",
          base is not None and base.image_name
          and base.image_name == layer.image_name)
    base_img = bpy.data.images[base.image_name]
    check("base color canvas is sRGB at the default size",
          colorspace_is(base_img, "sRGB")
          and tuple(base_img.size) == (ops.DEFAULT_IMAGE_SIZE,) * 2)

    # One logical layer, one image per channel.
    for key in ("roughness", "metallic", "height"):
        check("bind %s" % key, bpy.ops.impasto.binding_add(
            channel_key=key) == {"FINISHED"})
    images = {b.name: bpy.data.images[b.image_name]
              for b in layer.bindings}
    check("every binding owns a distinct canvas",
          len({img.name for img in images.values()}) == len(images),
          str({k: v.name for k, v in images.items()}))
    check("channel canvases share one resolution",
          {tuple(img.size) for img in images.values()}
          == {(ops.DEFAULT_IMAGE_SIZE,) * 2})
    for key in ("roughness", "metallic", "height"):
        check("%s canvas is Non-Color" % key,
              colorspace_is(images[key], "Non-Color"))
    check("height canvas seeds opaque neutral mid-gray",
          tuple(images["height"].generated_color) == (0.5, 0.5, 0.5, 1.0))
    check("color canvas seeds transparent",
          tuple(images["base_color"].generated_color)
          == (0.0, 0.0, 0.0, 0.0))

    # Compile/reconcile: each channel chain samples its own image node.
    layer_tree = bpy.data.node_groups[model.layer_tree_name(layer.name)]
    for key, img in images.items():
        src = layer_tree.nodes.get(model.n_binding_src(layer.name, key))
        check("per-binding image node drives %s" % key,
              src is not None and src.image is img)
    for key in ("base_color", "roughness", "metallic"):
        blend = tree.nodes.get(model.n_blend(key, layer.name))
        check("root %s chain taps the layer group" % key,
              blend is not None and any(
                  link.to_node.name == blend.name
                  and link.from_node.name == model.n_root_layer(layer.name)
                  and link.from_socket.name == "ch:%s" % key
                  for link in tree.links))
    check("height chain feeds Bump",
          any(link.to_node.name == model.n_bump()
              and link.to_socket.name == "Height"
              for link in tree.links))
    second = engine.reconcile_stack(tree)
    check("multi-channel graph reconciles to zero deltas",
          second.total() == 0 and not second.errors, str(second))

    # Native painting stays per-channel: activation picks the binding's
    # canvas and repairs its registry colorspace.
    images["roughness"].colorspace_settings.name = \
        compat.resolve_colorspace(images["roughness"], "sRGB")
    check("roughness channel activates natively",
          bpy.ops.impasto.paint_activate(channel_key="roughness")
          == {"FINISHED"})
    settings = bpy.context.scene.tool_settings.image_paint
    check("native canvas is the roughness image",
          settings.canvas is images["roughness"])
    check("roughness colorspace repaired on activation",
          colorspace_is(images["roughness"], "Non-Color"))
    check("default activation picks the first channel in registry order",
          bpy.ops.impasto.paint_activate(channel_key="") == {"FINISHED"}
          and settings.canvas is images["base_color"])
    check("unbound channel activation is refused",
          paint.paint_binding(layer, "normal") is None)

    # GPU target planning follows registry order with per-binding images.
    targets = ops.gpu_paint_targets(layer)
    check("gpu targets in registry order",
          [key for key, _img in targets]
          == ["base_color", "metallic", "roughness", "height"],
          str([key for key, _img in targets]))
    check("gpu targets carry the per-binding canvases",
          all(img is images[key] for key, img in targets))

    # Legacy migration (schema 1 -> 2): a single-canvas layer's SHARED
    # bindings inherit the layer canvas; no images are created.
    legacy_img = bpy.data.images.new("Impasto Legacy Canvas", 8, 8,
                                     alpha=True)
    with engine.stack_edit_session(tree):
        legacy = state.layers.add()
        legacy.name = "1e9ac1e5"
        legacy.label = "Legacy Paint"
        legacy.layer_type = 'PAINT'
        legacy.image_name = legacy_img.name
        binding = legacy.bindings.add()
        binding.name = "base_color"
        constant = legacy.bindings.add()
        constant.name = "roughness"
        constant.mode = 'VALUE'
        state.schema_version = 1
    image_count = len(bpy.data.images)
    check("legacy compile falls back to the layer canvas",
          bpy.data.node_groups[model.layer_tree_name(legacy.name)]
          .nodes.get(model.n_src(legacy.name)) is not None)
    check("migration re-stamps schema 2",
          engine.run_migrations(tree) == 2)
    check("legacy SHARED binding inherited the layer canvas",
          legacy.bindings["base_color"].image_name == legacy_img.name)
    check("constant bindings untouched by migration",
          legacy.bindings["roughness"].image_name == "")
    check("migration creates no images",
          len(bpy.data.images) == image_count)
    before = legacy.bindings["base_color"].image_name
    engine.run_migrations(tree)
    check("migration is idempotent",
          state.schema_version == 2
          and legacy.bindings["base_color"].image_name == before)

    impasto.unregister()
    print("IMPASTO_MULTICHANNEL_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_MULTICHANNEL_FAILED")
