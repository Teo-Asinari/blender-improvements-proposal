# SPDX-License-Identifier: GPL-2.0-or-later
"""GPU multi-channel paint path — everything testable headless.

GPU object creation raises SystemError in --background (probed on
5.1.2), so this suite exercises what surrounds the draw callback: MRT
shader-source generation per channel count, stroke payload planning,
blend-batch grouping, brush/dab/dirty-rect math, the harmless headless
session round-trip, and operator registration/poll. Real strokes are
GUI-checklist territory (README).
"""

import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import gpu_engine, model, ops


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


for target in (0.2, 0.8):
    spacing = 0.1
    per_dab = gpu_engine.overlap_compensated_opacity(target, spacing)
    accumulated = 1.0 - (1.0 - per_dab) ** round(1.0 / spacing)
    check("pressure opacity survives dense dab overlap at %.1f" % target,
          abs(accumulated - target) < 1e-6, repr(accumulated))


try:
    impasto.register()

    # ---- registry contract behind stroke_payloads --------------------
    keys = gpu_engine.GPU_PAINT_CHANNEL_KEYS
    check("paintable keys all exist in the registry",
          all(k in model.CHANNEL_MAP for k in keys))
    check("paintable keys are in registry order",
          list(keys) == sorted(keys, key=lambda k: model.CHANNEL_ORDER[k]))
    check("only display colors use sRGB paint canvases",
          {k for k in keys if model.CHANNEL_MAP[k].colorspace == "sRGB"}
          == {"base_color", "emission_color"})

    # ---- MRT fragment source generation -------------------------------
    one = gpu_engine.dab_frag_src(1)
    four = gpu_engine.dab_frag_src(4)
    check("N=1 source assigns exactly the baseline output",
          "fragColor =" in one and "fragColor1" not in one)
    check("N=4 source assigns four distinct outputs",
          all(("fragColor%d =" % i) in four for i in (1, 2, 3))
          and all(("brush_values[%d]" % i) in four for i in range(4)))
    additive = gpu_engine.dab_frag_src(2, additive=True)
    check("additive variant premultiplies the signed payload",
          "brush_values[0].rgb" in additive
          and "brush_values[0].a" in additive
          and "brush_values[1].rgb" in additive)
    check("alpha-blend variant deposits the raw payload color",
          "vec4(dab_params.brush_values[0].rgb," in one)
    try:
        gpu_engine.dab_frag_src(gpu_engine.MAX_CHANNELS + 1)
        check("channel-count ceiling enforced", False)
    except ValueError:
        check("channel-count ceiling enforced", True)
    info = gpu_engine.dab_shader_create_info(4)
    check("create-info population works headless (pure bookkeeping)",
          info is not None)
    check("dab shader uses a std140-friendly UBO contract",
          "struct ImpastoDabParams" in gpu_engine.DAB_UBO_TYPEDEF
          and "dab_params.model_matrix" in gpu_engine.DAB_VERT_SRC
          and "dab_params.view_proj_matrix" in
          gpu_engine._DAB_FRAG_PRELUDE)
    matrix = ((1.0, 2.0, 3.0, 4.0),
              (5.0, 6.0, 7.0, 8.0),
              (9.0, 10.0, 11.0, 12.0),
              (13.0, 14.0, 15.0, 16.0))
    packed = gpu_engine.dab_uniform_data(
        matrix, matrix, (0.0, 0.0, 1.0, 2.0), (800, 600), (20, 30),
        40, 0.5, 1e-4, 2e-5, True, 0.75, True, True, False, 0.6,
        (0.25, 0.75), (1.2, 0.8), 0.4,
        ((0.1, 0.2, 0.3, 0.9),))
    check("dab UBO pack is contiguous vec4 std140 data",
          packed.shape == (gpu_engine.DAB_UBO_VEC4_COUNT, 4)
          and packed.dtype.name == "float32"
          and packed.flags.c_contiguous)
    check("dab UBO pack transposes matrices to GLSL column-major order",
          tuple(packed[gpu_engine.DAB_UBO_MODEL]) == (1.0, 5.0, 9.0, 13.0))
    check("dab UBO pack preserves dynamic and MRT values",
          tuple(packed[gpu_engine.DAB_UBO_REGION_CENTER]) ==
          (800.0, 600.0, 20.0, 30.0)
          and abs(packed[gpu_engine.DAB_UBO_PAINT_FLAGS, 1] - 0.75) < 1e-6
          and all(abs(float(a) - b) < 1e-6 for a, b in zip(
              packed[gpu_engine.DAB_UBO_BRUSH_VALUES],
              (0.1, 0.2, 0.3, 0.9))))
    preview_info = gpu_engine.preview_shader_create_info()
    check("composed preview create-info covers every PBR paint channel",
          preview_info is not None
          and all((key + "_tex") in gpu_engine.PREVIEW_FRAG_SRC
                  for key in gpu_engine.GPU_PAINT_CHANNEL_KEYS))
    check("live preview is composed rather than raw Base Color",
          "metallic" in gpu_engine.PREVIEW_FRAG_SRC
          and "roughness" in gpu_engine.PREVIEW_FRAG_SRC
          and "normal_sample" in gpu_engine.PREVIEW_FRAG_SRC
          and "height" in gpu_engine.PREVIEW_FRAG_SRC)
    check("Lit preview has roughness-sensitive studio keys",
          "preview_key_light" in gpu_engine.PREVIEW_FRAG_SRC
          and "roughness * roughness" in gpu_engine.PREVIEW_FRAG_SRC)
    check("preview display mode identifiers are stable",
          gpu_engine.PREVIEW_MODES == (
              "LIT_PBR", "RAW_TANGENT_NORMAL",
              "NEUTRAL_NORMAL_LIGHTING", "HEIGHT_GRAYSCALE"))
    check("preview shader has explicit diagnostic branches",
          "preview_mode == 1" in gpu_engine.PREVIEW_FRAG_SRC
          and "normal_sample.rgb" in gpu_engine.PREVIEW_FRAG_SRC
          and "preview_mode == 2" in gpu_engine.PREVIEW_FRAG_SRC
          and "preview_mode == 3" in gpu_engine.PREVIEW_FRAG_SRC)
    check("unknown preview mode safely normalizes to Lit PBR",
          gpu_engine.normalize_preview_mode("not-a-mode") == "LIT_PBR"
          and gpu_engine.preview_mode_index("HEIGHT_GRAYSCALE") == 3)

    # ---- payload planning ---------------------------------------------
    brush = {"color": (0.5, 0.25, 1.0), "roughness": 0.7,
             "metallic": 0.2, "normal": (0.5, 0.5, 1.0),
             "height_strength": 0.05, "height_direction": "RAISE"}
    payloads = gpu_engine.stroke_payloads(keys, brush)
    by_key = dict(zip(keys, payloads))
    srgb = gpu_engine.linear_to_srgb
    check("linear_to_srgb endpoints exact",
          srgb(0.0) == 0.0 and abs(srgb(1.0) - 1.0) < 1e-9)
    check("linear_to_srgb midpoint matches IEC 61966-2-1",
          abs(srgb(0.5) - 0.7353569) < 1e-4, repr(srgb(0.5)))
    check("base color payload is sRGB-encoded",
          all(abs(v - srgb(c)) < 1e-9 for v, c in
              zip(by_key["base_color"]["value"], brush["color"])))
    check("scalar payloads are raw grayscale triples",
          by_key["roughness"]["value"] == (0.7, 0.7, 0.7)
          and by_key["metallic"]["value"] == (0.2, 0.2, 0.2))
    check("normal payload passes encoded RGB through",
          by_key["normal"]["value"] == (0.5, 0.5, 1.0))
    check("only height is additive",
          by_key["height"]["blend"] == "ADD"
          and all(by_key[k]["blend"] == "MIX" for k in keys
                  if k != "height"))
    check("GPU occlusion compares linear view-space depth",
          "impasto_visible_surface" in gpu_engine._DAB_FRAG_PRELUDE
          and "viewDepth" in gpu_engine.PREPASS_FRAG_SRC
          and "clipPos.z / clipPos.w" not in
          gpu_engine.PREPASS_FRAG_SRC)
    check("raise deposits a positive height step",
          by_key["height"]["value"] == (0.05, 0.05, 0.05))
    lower = gpu_engine.stroke_payloads(
        ("height",), dict(brush, height_direction="LOWER"))[0]
    check("lower deposits a negative height step",
          lower["value"] == (-0.05, -0.05, -0.05)
          and lower["blend"] == "ADD")
    expanded_brush = dict(
        brush, emission_color=(0.25, 0.5, 1.0), emission_strength=8.0,
        sss_weight=0.7, sss_radius=(1.0, 0.25, 0.1), sss_scale=0.03)
    expanded = dict(zip(
        ("emission_color", "emission_strength", "sss_weight",
         "sss_radius", "sss_scale"),
        gpu_engine.stroke_payloads(
            ("emission_color", "emission_strength", "sss_weight",
             "sss_radius", "sss_scale"), expanded_brush)))
    check("emission color uses the same sRGB storage boundary as Base",
          expanded["emission_color"]["value"] == tuple(
              srgb(c) for c in expanded_brush["emission_color"]))
    check("HDR emission strength remains unclipped",
          expanded["emission_strength"]["value"] == (8.0, 8.0, 8.0))
    check("SSS factor/vector/distance remain raw Non-Color values",
          expanded["sss_weight"]["value"] == (0.7, 0.7, 0.7)
          and expanded["sss_radius"]["value"] == (1.0, 0.25, 0.1)
          and expanded["sss_scale"]["value"] == (0.03, 0.03, 0.03))
    try:
        gpu_engine.stroke_payloads(("sss_ior",), brush)
        check("specialized non-brush SSS channels are rejected", False)
    except ValueError:
        check("specialized non-brush SSS channels are rejected", True)

    # ---- straight <-> premultiplied canvas boundary conversions -------
    # gpu 'ALPHA' blending accumulates premultiplied; canvases are
    # straight alpha and the compiled chains mix VALUE by alpha, so both
    # boundaries must convert (the Material Preview scalar/normal
    # regression).
    import numpy as np
    straight = np.array([1.0, 0.5, 0.25, 0.5,     # half-covered texel
                         0.3, 0.6, 0.9, 0.0,      # uncovered (rgb junk)
                         0.9, 0.8, 0.7, 1.0],     # opaque texel
                        dtype=np.float32)
    pm = gpu_engine.premultiply_canvas(straight.copy())
    check("premultiply scales rgb by alpha and preserves alpha",
          np.allclose(pm.reshape(-1, 4)[:, :3],
                      [[0.5, 0.25, 0.125], [0.0, 0.0, 0.0],
                       [0.9, 0.8, 0.7]])
          and np.allclose(pm.reshape(-1, 4)[:, 3], [0.5, 0.0, 1.0]))
    rt = gpu_engine.unpremultiply_readback(pm)
    check("readback un-premultiply restores straight values "
          "(rgb zeroed where a=0)",
          np.allclose(rt.reshape(-1, 4),
                      [[1.0, 0.5, 0.25, 0.5], [0.0, 0.0, 0.0, 0.0],
                       [0.9, 0.8, 0.7, 1.0]], atol=1e-6))
    check("readback conversion copies (mirrors stay in fb space)",
          rt is not pm and np.allclose(pm.reshape(-1, 4)[0, :3],
                                       [0.5, 0.25, 0.125]))
    kiln_upload = gpu_engine.prepare_canvas_upload(
        np.array([0.7, 0.3, 1.0, 0.0], dtype=np.float32), opaque=True)
    check("authoritative zero-alpha normal survives active upload",
          np.allclose(kiln_upload, [0.7, 0.3, 1.0, 1.0]),
          repr(kiln_upload))
    # One source-over dab at coverage a onto a transparent canvas must
    # round-trip to (value, a) — NOT (value*a, a), which was the bug.
    dab_v, dab_a = 0.8, 0.5
    fb = np.zeros(4, dtype=np.float32)             # premult accumulator
    fb[:3] = dab_v * dab_a + fb[:3] * (1.0 - dab_a)
    fb[3] = dab_a + fb[3] * (1.0 - dab_a)
    synced = gpu_engine.unpremultiply_readback(fb).reshape(4)
    check("soft dab syncs back the painted value at its coverage",
          abs(synced[0] - dab_v) < 1e-6 and abs(synced[3] - dab_a) < 1e-6,
          str(synced.tolist()))

    # ---- blend-batch grouping (one blend mode per MRT draw) -----------
    mixed = [{"blend": "MIX"}, {"blend": "ADD"}, {"blend": "MIX"},
             {"blend": "MIX"}, {"blend": "MIX"}, {"blend": "MIX"}]
    batches = gpu_engine.plan_target_batches(mixed)
    check("equal-blend targets pack into framebuffer-sized batches",
          batches == (("MIX", (0, 2, 3, 4)), ("MIX", (5,)),
                      ("ADD", (1,))), str(batches))
    check("every target lands in exactly one batch",
          sorted(i for _b, idx in batches for i in idx)
          == list(range(len(mixed))))

    # ---- brush / dab / dirty-rect math ---------------------------------
    check("falloff is 1 inside the hardness core and 0 at the rim",
          gpu_engine.brush_falloff(0.3, 0.5) == 1.0
          and gpu_engine.brush_falloff(1.0, 0.5) == 0.0
          and 0.0 < gpu_engine.brush_falloff(0.75, 0.5) < 1.0)
    dabs, leftover = gpu_engine.interpolate_dabs(0.0, 0.0, 10.0, 0.0, 4.0)
    check("dab interpolation spaces evenly and carries leftover",
          [round(d[0], 6) for d in dabs] == [4.0, 8.0]
          and abs(leftover - 2.0) < 1e-9)
    check("tablet pressure rejects transient zero and invalid samples",
          gpu_engine.sanitize_pressure(0.0, 0.4) == 0.4
          and gpu_engine.sanitize_pressure(float("nan"), 0.6) == 0.6
          and gpu_engine.sanitize_pressure(2.0) == 1.0)
    rect = gpu_engine.dab_rect_union([(10.0, 20.0), (30.0, 5.0)], 4.0)
    check("dab union rect covers every disc",
          rect == (6.0, 1.0, 34.0, 24.0), str(rect))
    check("uv bbox to texel rect clamps and pads",
          gpu_engine.uv_bbox_to_pixel_rect((0.0, 0.0, 0.5, 0.25), 64,
                                           pad=2)
          == (0, 0, 34, 18))
    check("union_bbox tolerates None",
          gpu_engine.union_bbox(None, (0, 0, 1, 1)) == (0, 0, 1, 1)
          and gpu_engine.union_bbox((0, 0, 1, 1), None) == (0, 0, 1, 1))

    # ---- headless session round-trip (harmless no-op contract) --------
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    images = [bpy.data.images.new("Impasto GPU Test %d" % i, 64, 64,
                                  alpha=True) for i in range(2)]
    started = gpu_engine.start_session(
        obj, images, None,
        payloads=gpu_engine.stroke_payloads(("base_color", "height"),
                                            brush),
        settings={"radius": 40.0, "hardness": 0.5})
    check("headless session starts as a logical no-op", started)
    check("session reports active", gpu_engine.session_active())
    check("GPU session defaults to Lit PBR preview",
          gpu_engine.current_preview_mode() == "LIT_PBR")
    check("preview mode changes without session restart",
          gpu_engine.set_preview_mode("RAW_TANGENT_NORMAL")
          and gpu_engine.current_preview_mode() == "RAW_TANGENT_NORMAL")
    gpu_engine.set_cursor(21, 37)
    check("GPU reticle tracks viewport mouse coordinates",
          gpu_engine.cursor_position() == (21.0, 37.0))
    refreshed_brush = dict(brush)
    refreshed_brush["color"] = (0.2, 0.4, 0.6)
    refreshed_brush["height_strength"] = 0.125
    refreshed = gpu_engine.stroke_payloads(
        ("base_color", "height"), refreshed_brush)
    check("GPU values refresh between strokes without restart",
          gpu_engine.update_stroke_settings(
              refreshed, radius=73.0, hardness=0.25))
    current_payloads, current_settings = \
        gpu_engine.stroke_settings_snapshot()
    check("GPU session uses refreshed payload, radius and hardness",
          current_payloads == refreshed
          and current_settings["radius"] == 73.0
          and current_settings["hardness"] == 0.25)
    gpu_engine.begin_stroke(10.0, 10.0, 0.2)
    gpu_engine.move_stroke(30.0, 10.0, 0.8, 40.0)
    queued_pressures = [dab[2] for dab in gpu_engine._session.dab_queue]
    check("tablet pressure interpolates across generated dabs",
          queued_pressures[0] == 0.2
          and queued_pressures[-1] == 0.8
          and queued_pressures == sorted(queued_pressures),
          repr(queued_pressures))
    check("stroke state tracks headlessly", gpu_engine.stroke_active())
    gpu_engine.end_stroke()
    check("pen-up does not queue blocking Image synchronization",
          gpu_engine.take_pending_pixels() is None)
    check("explicit GPU flush can be queued independently of pen-up",
          gpu_engine.request_flush() and gpu_engine.busy())
    check("no error latched headlessly",
          gpu_engine.last_error() is None)
    gpu_engine.stop_session()
    check("session stops cleanly", not gpu_engine.session_active())

    # The stack plan must exist before lazy GPU allocation; otherwise the
    # first draw cannot build lower/Kiln baseline textures.
    active_model = model.LayerModel(
        uid="active", label="Detail", layer_type="PAINT", uv_map="UVMap",
        bindings=(model.BindingModel(
            key="normal", mode="SHARED", image_name=images[0].name),))
    kiln_model = model.LayerModel(
        uid="kiln", label="Kiln Baked Normal", layer_type="PAINT",
        uv_map="UVMap", bindings=(model.BindingModel(
            key="normal", mode="SHARED", image_name="Kiln Runtime Normal"),))
    resident_model = model.StackModel(
        root_tree_name="Runtime", channels=("normal",),
        layers=(active_model, kiln_model))
    check("resident stack session starts",
          gpu_engine.start_session(
              obj, [images[0]], None,
              payloads=gpu_engine.stroke_payloads(("normal",), brush),
              settings={"channel_keys": ("normal",),
                        "stack_model": resident_model,
                        "active_layer_uid": "active"}))
    check("lower/Kiln stack plan exists before first GPU draw",
          gpu_engine._session.stack_spec["enabled"]
          and gpu_engine._session.stack_spec["channels"]["normal"]
          ["lower_steps"][0]["source"]["image_name"]
          == "Kiln Runtime Normal",
          repr(gpu_engine._session.stack_spec))
    gpu_engine.stop_session()

    # ---- operator surface ----------------------------------------------
    check("gpu paint operator registered",
          getattr(bpy.types, "IMPASTO_OT_gpu_paint", None) is not None)
    check("explicit GPU flush operator registered",
          getattr(bpy.types, "IMPASTO_OT_gpu_flush", None) is not None)
    check("poll requires an Impasto paint layer",
          not bpy.ops.impasto.gpu_paint.poll())
    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {"FINISHED"})
    check("bind height", bpy.ops.impasto.binding_add(
        channel_key="height") == {"FINISHED"})
    check("poll accepts the multi-channel paint layer",
          bpy.ops.impasto.gpu_paint.poll())
    layer = bpy.data.node_groups[
        obj.active_material.node_tree.nodes[
            model.n_material_stack()].node_tree.name].impasto.active_layer()
    check("operator target planning matches the layer's bindings",
          [key for key, _img in ops.gpu_paint_targets(layer)]
          == ["base_color", "height"])
    try:
        # Headless there is no window/event: Blender either refuses the
        # call outright (PASS_THROUGH + "Invalid operator call"), or the
        # invoke's own area guard cancels. Both are graceful declines.
        result = bpy.ops.impasto.gpu_paint('INVOKE_DEFAULT')
        check("headless invoke declines gracefully",
              result in ({'CANCELLED'}, {'PASS_THROUGH'}), str(result))
    except RuntimeError:
        # bpy.ops raises on {'ERROR'} reports — an equally graceful no.
        check("headless invoke declines gracefully", True)
    check("no session leaked by the declined invoke",
          not gpu_engine.session_active())

    # Reload/disable safety: an active session must not outlive the add-on.
    check("reload-safety session starts", gpu_engine.start_session(
        obj, images, None,
        payloads=gpu_engine.stroke_payloads(("base_color", "height"),
                                            brush),
        settings={"radius": 40.0, "hardness": 0.5}))
    check("reload-safety precondition", gpu_engine.session_active())
    impasto.unregister()
    check("unregister tears down the GPU session",
          not gpu_engine.session_active())
    print("IMPASTO_GPU_PAINT_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_GPU_PAINT_FAILED")
