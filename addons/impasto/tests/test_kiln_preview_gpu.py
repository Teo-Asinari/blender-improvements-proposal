# SPDX-License-Identifier: GPL-2.0-or-later
"""Foreground GPU reproduction for Kiln normal preview upload/wiring."""

import sys
from pathlib import Path

import bpy
import numpy as np

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import gpu_engine, model


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def binding(image_name):
    return model.BindingModel(
        key="normal", mode="SHARED", image_name=image_name)


def layer(uid, label, image_name):
    return model.LayerModel(
        uid=uid, label=label, layer_type="PAINT", uv_map="UVMap",
        bindings=(binding(image_name),))


def pixels(texture):
    return np.asarray(texture.read().to_list(), dtype=np.float32).reshape(-1, 4)


impasto.register()
try:
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    size = 4
    encoded = (0.7, 0.3, 1.0, 0.0)
    kiln_image = bpy.data.images.new("Kiln GPU Repro", size, size, alpha=True)
    kiln_image.colorspace_settings.name = "Non-Color"
    kiln_image.pixels.foreach_set(encoded * (size * size))
    active_image = bpy.data.images.new("Active GPU Repro", size, size,
                                       alpha=True)
    active_image.pixels.foreach_set((0.5, 0.5, 1.0, 0.0) * (size * size))

    top = layer("top", "Detail", active_image.name)
    kiln = layer("kiln", "Kiln Baked Normal", kiln_image.name)
    top_stack = model.StackModel(
        "Top active", ("normal",), (top, kiln))
    check("top-active session starts", gpu_engine.start_session(
        obj, [active_image], None,
        payloads=gpu_engine.stroke_payloads(
            ("normal",), {"normal": (0.5, 0.5, 1.0)}),
        settings={"channel_keys": ("normal",),
                  "stack_model": top_stack, "active_layer_uid": "top"}))
    session = gpu_engine._session
    check("top-active stack is resolved before allocation",
          session.stack_spec["enabled"], repr(session.stack_spec))
    gpu_engine._ensure_gpu(session)
    baseline = pixels(session.baseline_texs["normal"])[0]
    check("Kiln baseline GPU texture preserves encoded RGB",
          np.allclose(baseline, (0.7, 0.3, 1.0, 1.0), atol=3e-3),
          repr(baseline))
    check("resolved top preview has normal input",
          session.stack_spec["enabled"]
          and "normal" in session.stack_spec["channels"])
    gpu_engine.stop_session()

    # Selecting the lower Kiln layer intentionally falls back because a
    # participating layer is above it. Its active canvas must still retain the
    # authoritative normal RGB rather than being erased by zero bake alpha.
    check("lower-active session starts", gpu_engine.start_session(
        obj, [kiln_image], None,
        payloads=gpu_engine.stroke_payloads(
            ("normal",), {"normal": (0.5, 0.5, 1.0)}),
        settings={"channel_keys": ("normal",),
                  "opaque_channel_keys": ("normal",),
                  "stack_model": top_stack, "active_layer_uid": "kiln"}))
    session = gpu_engine._session
    check("lower-active status explicitly reports upper-layer fallback",
          not session.stack_spec["enabled"]
          and "above the active layer" in session.stack_spec["status"],
          repr(session.stack_spec))
    gpu_engine._ensure_gpu(session)
    active_pixel = pixels(session.paint_texs[0])[0]
    check("fallback active Kiln texture preserves normal and opaque coverage",
          np.allclose(active_pixel, (0.7, 0.3, 1.0, 1.0), atol=3e-3),
          repr(active_pixel))
    check("fallback preview still binds has_normal",
          "normal" in session.settings["channel_keys"])
    print("IMPASTO_KILN_PREVIEW_GPU_PASSED")
finally:
    gpu_engine.stop_session()
    impasto.unregister()

bpy.ops.wm.quit_blender()
