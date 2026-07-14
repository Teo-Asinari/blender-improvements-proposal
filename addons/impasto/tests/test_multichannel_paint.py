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

    # Native multi-channel uses the same ordered canvases but substitutes the
    # layer's PBR stroke values into Blender's active brush for each replay.
    check("native replay targets match GPU target ordering",
          ops.native_replay_targets(layer) == targets)
    layer.paint_color = (0.1, 0.2, 0.3)
    layer.paint_metallic = 0.8
    layer.paint_roughness = 0.25
    layer.paint_height_strength = 0.07
    layer.paint_height_direction = 'LOWER'
    base_style = ops.native_channel_style(layer, "base_color")
    metallic_style = ops.native_channel_style(layer, "metallic")
    roughness_style = ops.native_channel_style(layer, "roughness")
    height_style = ops.native_channel_style(layer, "height")
    check("native base-color style uses the PBR stroke color",
          base_style[1] == "MIX"
          and all(abs(a - b) < 1e-6 for a, b in
                  zip(base_style[0], (0.1, 0.2, 0.3))))
    check("native scalar styles are grayscale brush colors",
          metallic_style[1] == roughness_style[1] == "MIX"
          and all(abs(v - 0.8) < 1e-6 for v in metallic_style[0])
          and all(abs(v - 0.25) < 1e-6 for v in roughness_style[0]))
    check("native height style preserves signed accumulation",
          height_style[1] == "SUB"
          and all(abs(v - 0.07) < 1e-6 for v in height_style[0]))

    point = paint.native_stroke_point(
        12, 34, 0.6, 48, 0.25, is_start=True, x_tilt=0.1, y_tilt=-0.2)
    check("native stroke capture preserves Blender replay fields",
          point["mouse"] == (12.0, 34.0)
          and point["mouse_event"] == (12.0, 34.0)
          and point["pressure"] == 0.6 and point["size"] == 48.0
          and point["time"] == 0.25 and point["is_start"]
          and point["x_tilt"] == 0.1 and point["y_tilt"] == -0.2)
    resolved_unified = paint.unified_paint_settings(bpy.context)
    image_unified = (bpy.context.scene.tool_settings.image_paint
                     .unified_paint_settings)
    check("Blender 5.1 unified settings use the ImagePaint API",
          resolved_unified is not None
          and resolved_unified.size == image_unified.size
          and resolved_unified.use_unified_size
          == image_unified.use_unified_size)

    # Canvas/mode restoration is testable headlessly; when a Brush asset is
    # active the same helper also restores color, secondary color and blend.
    settings = bpy.context.scene.tool_settings.image_paint
    settings.canvas = images["base_color"]
    settings.mode = 'IMAGE'
    original_occlude = settings.use_occlude
    original_backface = settings.use_backface_culling
    settings.use_occlude = False
    settings.use_backface_culling = False
    unified = paint.unified_paint_settings(bpy.context)
    original_unified_color = tuple(unified.color)
    original_unified_use_color = unified.use_unified_color
    native_state = paint.capture_native_state(bpy.context)
    settings.canvas = images["metallic"]
    settings.mode = 'MATERIAL'
    unified.color = (0.17, 0.29, 0.41)
    unified.use_unified_color = not original_unified_use_color
    paint.configure_front_surface_paint(bpy.context)
    check("native replay enables front-surface-only painting",
          settings.use_occlude and settings.use_backface_culling)
    paint.restore_native_state(bpy.context, native_state)
    check("native replay restores canvas and paint mode",
          settings.canvas is images["base_color"]
          and settings.mode == 'IMAGE')
    check("native replay restores Blender 5.1 unified brush color",
          all(abs(a - b) < 1e-6 for a, b in
              zip(unified.color, original_unified_color))
          and unified.use_unified_color == original_unified_use_color)

    class ReplayBrush:
        color = (0.0, 0.0, 0.0)

    replay_brush = ReplayBrush()
    check("ordinary replay values retain unified color behavior",
          paint.configure_native_replay_color(
              replay_brush, unified, (0.2, 0.4, 0.8), True)
          and unified.use_unified_color
          and all(abs(a - b) < 1e-6 for a, b in
                  zip(unified.color, (0.2, 0.4, 0.8))))
    bounded_unified_color = tuple(unified.color)
    check("HDR replay bypasses bounded unified color without clamping brush",
          not paint.configure_native_replay_color(
              replay_brush, unified, (9.5, 9.5, 9.5), True)
          and not unified.use_unified_color
          and replay_brush.color == (9.5, 9.5, 9.5)
          and tuple(unified.color) == bounded_unified_color)
    unified.color = original_unified_color
    unified.use_unified_color = original_unified_use_color
    check("native replay restores paint occlusion preferences",
          not settings.use_occlude and not settings.use_backface_culling)
    settings.use_occlude = original_occlude
    settings.use_backface_culling = original_backface
    check("native multi-channel operator registered and pollable",
          getattr(bpy.types, "IMPASTO_OT_native_multichannel_paint", None)
          is not None
          and bpy.ops.impasto.native_multichannel_paint.poll())

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
