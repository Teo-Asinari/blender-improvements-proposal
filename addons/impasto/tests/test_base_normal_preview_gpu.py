# SPDX-License-Identifier: GPL-2.0-or-later
"""Foreground GPU smoke test for the preview-only Base Normal Map bridge."""

import sys
from pathlib import Path

import bpy
import numpy as np

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import gpu_engine


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


impasto.register()
try:
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    active_uv = obj.data.uv_layers.new(name="PaintUV")
    base_uv = obj.data.uv_layers.new(name="BaseUV")
    # Deliberately rotate the fallback UV orientation relative to paint UV.
    for loop, datum in enumerate(base_uv.data):
        u, v = active_uv.data[loop].uv
        datum.uv = (v, 1.0 - u)

    size = 4
    encoded = (0.7, 0.2, 0.95, 0.0)
    base_image = bpy.data.images.new("Base Normal GPU Test", size, size,
                                     alpha=True)
    base_image.colorspace_settings.name = "Non-Color"
    base_image.pixels.foreach_set(encoded * (size * size))
    paint_image = bpy.data.images.new("Detail Normal GPU Test", size, size,
                                      alpha=True)
    paint_image.colorspace_settings.name = "Non-Color"
    paint_image.pixels.foreach_set((0.5, 0.5, 1.0, 0.0) * (size * size))

    settings = {
        "channel_keys": ("normal",),
        "base_normal_image_name": base_image.name,
        "base_normal_uv_map": "BaseUV",
        "base_normal_strength": 1.0,
        "base_normal_invert_green": False,
    }
    check("preview-only base-normal session starts", gpu_engine.start_session(
        obj, [paint_image], None,
        payloads=gpu_engine.stroke_payloads(
            ("normal",), {"normal": (0.5, 0.5, 1.0)}),
        settings=settings))
    session = gpu_engine._session
    check("named fallback UV is independent from paint UV",
          not np.allclose(session.base_normal_uvs, session.uvs))
    # This creates/compiles the real preview shader and uploads the image.
    gpu_engine._ensure_gpu(session)
    uploaded = np.asarray(
        session.base_normal_tex.read().to_list(), dtype=np.float32).reshape(-1, 4)[0]
    check("known normal RGB survives preview upload with opaque coverage",
          np.allclose(uploaded, (0.7, 0.2, 0.95, 1.0), atol=3e-3),
          repr(uploaded))

    check("strength and green inversion update without paint synchronization",
          gpu_engine.set_preview_base_normal({
              **settings, "base_normal_strength": 0.0,
              "base_normal_invert_green": True})
          and gpu_engine.take_pending_pixels() is None)
    _payloads, updated = gpu_engine.stroke_settings_snapshot()
    check("strength zero and green inversion reach resident draw settings",
          updated["base_normal_strength"] == 0.0
          and updated["base_normal_invert_green"] is True)

    gpu_engine.stop_session()
    missing = dict(settings)
    missing["base_normal_image_name"] = "Missing Base Normal"
    check("missing base image does not prevent session startup",
          gpu_engine.start_session(
              obj, [paint_image], None,
              payloads=gpu_engine.stroke_payloads(
                  ("normal",), {"normal": (0.5, 0.5, 1.0)}),
              settings=missing))
    gpu_engine._ensure_gpu(gpu_engine._session)
    check("missing base image disables its GPU texture safely",
          gpu_engine._session.base_normal_tex is None)
    print("IMPASTO_BASE_NORMAL_PREVIEW_GPU_PASSED")
finally:
    gpu_engine.stop_session()
    impasto.unregister()

bpy.ops.wm.quit_blender()
