# SPDX-License-Identifier: GPL-2.0-or-later
"""Rendered semantics of per-binding PBR paint canvases (EEVEE).

Regression coverage for the v0.4 Material Preview bug: dedicated
Metallic / Roughness / Tangent Normal canvases (schema 2, binding_add)
rendered wrong for any texel the GPU brush deposited with alpha < 1.
The MIX dab framebuffer accumulates PREMULTIPLIED alpha (source-over),
but canvases are STRAIGHT alpha and the compiled chains mix the RGB
value by the canvas alpha — syncing the framebuffer raw stored
``value*a`` where ``value`` was painted.  gpu_engine now converts at
both boundaries (premultiply_canvas / unpremultiply_readback); this
test drives that exact math on the CPU (GPU objects cannot exist in
--background) and measures EEVEE-rendered EXR pixels — Material
Preview shading is EEVEE.

Bands on each canvas: unpainted (transparent seed) | native-style
straight alpha=1 | GPU-stroke-simulated coverage a=0.5 through the
fixed seed->composite->readback pipeline.
"""

import math
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
import numpy as np

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import compat, engine, gpu_engine, model


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def band_stats(tag):
    """Mean RGB of three 7x8-pixel windows at the plane bands
    (u = 1/6, 1/2, 5/6; the size-2 plane spans 2/2.2 of the frame)."""
    path = os.path.join(tempfile.gettempdir(),
                        "impasto_pbr_canvas_%s.exr" % tag)
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(path, check_existing=False)
    px = list(image.pixels)
    w, h = image.size
    out = {}
    for name, u in (("unpainted", 1.0 / 6.0), ("straight", 0.5),
                    ("gpu_sim", 5.0 / 6.0)):
        cx = int((0.5 + (u - 0.5) * (2.0 / 2.2)) * w)
        acc = [0.0, 0.0, 0.0]
        count = 0
        for y in range(h // 2 - 4, h // 2 + 4):
            for x in range(cx - 3, cx + 4):
                i = 4 * (y * w + x)
                for c in range(3):
                    acc[c] += px[i + c]
                count += 1
        out[name] = tuple(a / count for a in acc)
    bpy.data.images.remove(image)
    return out


def emission_link(tree, stack, principled, output_name):
    socket = compat.find_socket(principled.inputs, "Emission Color")
    for link in list(socket.links):
        tree.links.remove(link)
    source = stack.outputs[output_name]
    if source.type == 'VALUE':
        combine = tree.nodes.get("Impasto PBR Probe Combine")
        if combine is None:
            combine = tree.nodes.new("ShaderNodeCombineColor")
            combine.name = "Impasto PBR Probe Combine"
            combine.mode = 'RGB'
        for s in combine.inputs[:3]:
            for link in list(s.links):
                tree.links.remove(link)
            tree.links.new(source, s)
        tree.links.new(combine.outputs["Color"], socket)
    else:
        tree.links.new(source, socket)
    compat.find_socket(principled.inputs,
                       "Emission Strength").default_value = 1.0


def alpha_over_premult(dst, src_rgb, src_a):
    """The MIX framebuffer blend (gpu 'ALPHA': rgb SRC_ALPHA /
    ONE_MINUS_SRC_ALPHA, a ONE / ONE_MINUS_SRC_ALPHA) — dst is
    premultiplied, src is the dab shader's straight output."""
    r, g, b = src_rgb
    dst[..., 0] = r * src_a + dst[..., 0] * (1.0 - src_a)
    dst[..., 1] = g * src_a + dst[..., 1] * (1.0 - src_a)
    dst[..., 2] = b * src_a + dst[..., 2] * (1.0 - src_a)
    dst[..., 3] = src_a + dst[..., 3] * (1.0 - src_a)


def write_bands(image, value_rgb):
    """Middle band: native-style straight alpha=1 paint.  Right band:
    a coverage-0.5 GPU dab pushed through the FIXED pipeline —
    straight seed -> premultiply -> source-over composite ->
    unpremultiply_readback -> Image.pixels."""
    w, h = image.size
    mirror = np.zeros(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(mirror)
    gpu_engine.premultiply_canvas(mirror)          # seed upload
    view = mirror.reshape(h, w, 4)
    x1, x2 = w // 3, (2 * w) // 3
    alpha_over_premult(view[:, x1:x2], value_rgb, 1.0)   # opaque core
    alpha_over_premult(view[:, x2:], value_rgb, 0.5)     # soft rim / pressure
    out = gpu_engine.unpremultiply_readback(mirror)      # sync-back
    image.pixels.foreach_set(out)
    image.update()


try:
    impasto.register()
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_EEVEE'
    scene.render.resolution_x = 96
    scene.render.resolution_y = 96
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.view_settings.view_transform = 'Standard'
    scene.world.use_nodes = False
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
    bpy.ops.object.light_add(type='SUN', location=(0.0, 0.0, 5.0))
    sun = bpy.context.object
    sun.data.energy = 2.0
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT", canvas_size='1024') == {"FINISHED"})
    mat = obj.active_material
    root = engine.find_stack_for_material(mat)
    layer = root.impasto.active_layer()
    principled = compat.find_principled(mat.node_tree)
    stack = mat.node_tree.nodes[model.n_material_stack()]

    ref = band_stats("reference")

    for key in ("metallic", "roughness", "normal"):
        check("bind %s" % key, bpy.ops.impasto.binding_add(
            channel_key=key) == {"FINISHED"})
    engine.rebuild(root)
    root.update_tag()
    mat.node_tree.update_tag()

    bound = band_stats("bound_unpainted")
    drift = max(abs(bound[band][c] - ref[band][c])
                for band in ref for c in range(3))
    check("transparent channel canvases leave the lit render unchanged",
          drift < 0.02, "drift %.4f" % drift)

    # Emission probes must see ONLY the stack output signal.
    bpy.data.objects.remove(sun, do_unlink=True)

    nz = math.sqrt(0.5)
    tilt_enc = ((nz + 1.0) / 2.0, 0.5, (nz + 1.0) / 2.0)  # 45 deg +X tilt
    for key, rgb in (("metallic", (1.0, 1.0, 1.0)),
                     ("roughness", (1.0, 1.0, 1.0)),
                     ("normal", tilt_enc)):
        write_bands(bpy.data.images[layer.bindings[key].image_name], rgb)
    root.update_tag()
    mat.node_tree.update_tag()

    # unpainted = channel default; straight = painted value; gpu_sim =
    # value mixed at coverage 0.5 over the default.
    for key, want in (("metallic", (0.0, 1.0, 0.5)),
                      ("roughness", (0.5, 1.0, 0.75))):
        emission_link(mat.node_tree, stack, principled,
                      model.CHANNEL_MAP[key].label)
        root.update_tag()
        mat.node_tree.update_tag()
        got = band_stats("probe_%s" % key)
        for band, expected in zip(("unpainted", "straight", "gpu_sim"),
                                  want):
            mean = sum(got[band]) / 3.0
            check("%s %s band renders %.2f" % (key, band, expected),
                  abs(mean - expected) < 0.06,
                  "got %.4f" % mean)

    emission_link(mat.node_tree, stack, principled, "Normal")
    root.update_tag()
    mat.node_tree.update_tag()
    got = band_stats("probe_normal")
    # Encoded mix at coverage 0.5: lerp(flat, tilt_enc, 0.5) decoded and
    # normalized by the Normal Map node.
    half = tuple(0.5 * f + 0.5 * t for f, t in zip((0.5, 0.5, 1.0),
                                                   tilt_enc))
    vec = tuple(2.0 * c - 1.0 for c in half)
    length = math.sqrt(sum(c * c for c in vec))
    half_world = tuple(c / length for c in vec)
    for band, want in (("unpainted", (0.0, 0.0, 1.0)),
                       ("straight", (nz, 0.0, nz)),
                       ("gpu_sim", half_world)):
        err = max(abs(got[band][c] - want[c]) for c in range(3))
        check("normal %s band decodes to %s" %
              (band, "(%.2f %.2f %.2f)" % want),
              err < 0.08, "got (%.3f %.3f %.3f)" % got[band])

    impasto.unregister()
    print("IMPASTO_PBR_CANVAS_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_PBR_CANVAS_FAILED")
