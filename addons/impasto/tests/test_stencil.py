# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless stencil transform, mask, state, and shader-contract tests."""

import math
import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import engine, gpu_engine, ops, stencil


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


try:
    centered = stencil.normalized(
        True, "Mask", 'VIEW_STENCIL', position=(0.5, 0.5),
        scale=(0.5, 0.25))
    check("viewport stencil center maps to image center",
          stencil.image_uv((400, 300), (0, 0), 25, (800, 600), centered)
          == (0.5, 0.5))
    check("viewport stencil respects independent normalized scale",
          stencil.image_uv((600, 300), (0, 0), 25, (800, 600), centered)
          == (1.0, 0.5))
    check("viewport points outside stencil are rejected",
          stencil.image_uv((601, 300), (0, 0), 25, (800, 600), centered)
          is None)
    rotated = stencil.normalized(
        True, "Mask", 'VIEW_STENCIL', position=(0.5, 0.5),
        scale=(0.5, 0.25), rotation=math.pi * 0.5)
    uv = stencil.image_uv((400, 400), (0, 0), 25, (800, 600), rotated)
    check("rotation is inverse-applied into image space",
          uv is not None and abs(uv[0] - 0.75) < 1e-6
          and abs(uv[1] - 0.5) < 1e-6, repr(uv))
    tip = stencil.normalized(True, "Tip", 'BRUSH_ALPHA', scale=(1.0, 0.5))
    check("brush alpha follows dab center and radius",
          stencil.image_uv((120, 100), (100, 100), 20, (800, 600), tip)
          == (1.0, 0.5))
    check("alpha interpretation uses image alpha",
          abs(stencil.interpreted_mask((1.0, 0.0, 0.0, 0.25),
                                       'ALPHA', 0.5) - 0.125) < 1e-9)
    check("luminance interpretation uses linear Rec.709 weights",
          abs(stencil.interpreted_mask((1.0, 0.0, 0.0, 0.0),
                                       'LUMINANCE', 1.0) - 0.2126) < 1e-9)
    profile = stencil.profile_tangent_normal(0.0, 1.0, 0.5, 0.5,
                                             strength=2.0)
    inverse = stencil.profile_tangent_normal(0.0, 1.0, 0.5, 0.5,
                                             strength=2.0, invert=True)
    flat = stencil.profile_tangent_normal(0.5, 0.5, 0.5, 0.5,
                                          strength=10.0)
    check("normal-profile contract derives tangent detail from gradients",
          profile[0] < 0.5 < inverse[0]
          and abs(profile[1] - 0.5) < 1e-9
          and flat == (0.5, 0.5, 1.0))
    check("normal-profile invert reverses relief without changing magnitude",
          abs((profile[0] - 0.5) + (inverse[0] - 0.5)) < 1e-9
          and abs(profile[2] - inverse[2]) < 1e-9)
    invalid = stencil.normalized(True, "", 'UNKNOWN', 'UNKNOWN', 4.0,
                                 scale=(0.0, -2.0))
    check("invalid settings normalize safely and missing image disables",
          invalid.projection == 'VIEW_STENCIL'
          and invalid.interpretation == 'ALPHA'
          and invalid.opacity == 1.0 and not invalid.active
          and invalid.scale == (0.001, 2.0))

    impasto.register()
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {'FINISHED'})
    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {'FINISHED'})
    tree = engine.find_stack_for_material(obj.active_material)
    layer = tree.impasto.active_layer()
    mask = bpy.data.images.new("Impasto Stencil Test", 16, 8, alpha=True)
    layer.brush_stencil_enabled = True
    layer.brush_stencil_image = mask
    layer.brush_stencil_projection = 'BRUSH_ALPHA'
    layer.brush_stencil_interpretation = 'LUMINANCE'
    layer.brush_stencil_opacity = 0.6
    layer.brush_stencil_position = (0.25, 0.75)
    check("Brush Alpha defaults to one full brush diameter",
          tuple(layer.brush_stencil_brush_scale) == (1.0, 1.0)
          and ops.gpu_stencil_settings(layer).scale == (1.0, 1.0))
    layer.brush_stencil_brush_scale = (1.2, 0.8)
    layer.brush_stencil_rotation = 0.4
    settings = ops.gpu_stencil_settings(layer)
    check("persistent layer state maps to plain GPU settings",
          settings.active and settings.image_name == mask.name
          and settings.projection == 'BRUSH_ALPHA'
          and settings.interpretation == 'LUMINANCE'
          and settings.opacity > 0.59
          and settings.position == (0.25, 0.75)
          and all(abs(a - b) < 1e-6 for a, b in
                  zip(settings.scale, (1.2, 0.8))))
    gpu_settings = settings.as_gpu_settings()
    check("runtime settings contain no Blender RNA objects",
          gpu_settings['stencil_image_name'] == mask.name
          and all(not isinstance(value, bpy.types.ID)
                  for value in gpu_settings.values()))

    source = gpu_engine.dab_frag_src(4)
    check("dab shader samples one shared stencil factor",
          source.count("texture(stencil_tex") == 1
          and "f *= stencil_factor" in source)
    check("every MRT output uses the same modulated falloff",
          all(("pressure * f" in line) for line in source.splitlines()
              if "fragColor" in line and "=" in line))
    info = gpu_engine.dab_shader_create_info(4)
    check("shader create-info exposes stencil sampler and transform",
          info is not None)

    targets = ops.gpu_paint_targets(layer)
    keys = tuple(key for key, _image in targets)
    images = [image for _key, image in targets]
    payloads = gpu_engine.stroke_payloads(keys, ops._gpu_brush(layer))
    runtime = {"channel_keys": keys, "radius": 40.0, "hardness": 0.5}
    runtime.update(gpu_settings)
    check("resident stencil session starts without image readback",
          gpu_engine.start_session(obj, images, None, payloads=payloads,
                                   settings=runtime))
    gpu_engine.begin_stroke(10.0, 10.0, 1.0)
    gpu_engine.end_stroke()
    check("stenciled pen-up queues no CPU image synchronization",
          gpu_engine.take_pending_pixels() is None)
    changed = stencil.normalized(True, mask.name, 'VIEW_STENCIL',
                                 'ALPHA', 0.25, (0.6, 0.4), (0.2, 0.3), 0.1)
    check("stencil transform refresh preserves resident session",
          gpu_engine.update_stroke_settings(
              payloads, stencil_settings=changed.as_gpu_settings())
          and gpu_engine.session_active()
          and gpu_engine.take_pending_pixels() is None)
    _payloads, refreshed = gpu_engine.stroke_settings_snapshot()
    check("refreshed stencil state is visible to next stroke",
          refreshed['stencil_projection'] == 'VIEW_STENCIL'
          and refreshed['stencil_position'] == (0.6, 0.4)
          and refreshed['stencil_opacity'] == 0.25)
    gpu_engine.stop_session()

    impasto.unregister()
    print("IMPASTO_STENCIL_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_STENCIL_FAILED")
