# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure complete-stack/baseline planning and composition tests."""

import sys
from pathlib import Path

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import gpu_engine, model, preview_stack


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def binding(key, mode="SHARED", image="", value=0.0, color=(0, 0, 0, 1),
            blend="LAYER", opacity=1.0, use_masks=True):
    return model.BindingModel(key=key, mode=mode, image_name=image,
                              value=value, color=color,
                              blend_mode=blend, opacity=opacity,
                              use_masks=use_masks)


def layer(uid, layer_type, bindings, blend="MIX", opacity=1.0,
          masks=(), uv_map="", label=""):
    return model.LayerModel(uid=uid, label=label, layer_type=layer_type,
                            blend_mode=blend, opacity=opacity,
                            bindings=tuple(bindings), masks=tuple(masks),
                            uv_map=uv_map)


# Stored order is top-to-bottom. This stack deliberately includes a nonlinear
# top layer, an active resident Paint layer, a lower Fill, and Kiln's ordinary
# bottom Paint-normal baseline.
top_overlay = layer("top_overlay", "FILL", [
    binding("base_color", mode="COLOR", color=(0.7, 0.2, 0.1, 1.0))
], blend="OVERLAY")
top_scalar = layer("top_scalar", "FILL", [
    binding("roughness", mode="VALUE", value=0.75)
], blend="MULTIPLY", opacity=0.4,
    masks=(model.MaskModel(uid="m1", image_name="Top Mask", uv_map="UV_B",
                           opacity=0.8),))
active = layer("active", "PAINT", [
    binding("base_color", image="Active Base"),
    binding("roughness", image="Active Rough"),
    binding("normal", image="Active Normal"),
], uv_map="UV_A")
lower_fill = layer("lower_fill", "FILL", [
    binding("base_color", mode="COLOR", color=(0.2, 0.3, 0.4, 1.0)),
    binding("roughness", mode="VALUE", value=0.8),
])
kiln = layer("kiln", "PAINT", [
    binding("normal", image="Kiln Normal")
], uv_map="UV_A", label="Kiln Baked Normal")

stack = model.StackModel(
    root_tree_name="Impasto Stack",
    channels=("base_color", "roughness", "normal"),
    layers=(top_overlay, top_scalar, active, lower_fill, kiln),
)

plan = preview_stack.plan_resident_preview(stack, "active")
check("stack partitions below and above active in composition order",
      plan.lower_layer_uids == ("kiln", "lower_fill")
      and plan.upper_layer_uids == ("top_scalar", "top_overlay"), repr(plan))
check("Kiln baseline is an ordinary lower image dependency",
      "Kiln Normal" in plan.image_dependencies)
check("active resident canvases are not uploaded as baseline dependencies",
      all(not name.startswith("Active ") for name in plan.image_dependencies))
check("upper masks are explicit dependencies",
      plan.mask_dependencies == ("Top Mask",))
check("different stack UV sets disable the one-UV fast path",
      not plan.single_uv_fast_path and plan.uv_maps == ("UV_A", "UV_B"))
check("upper Overlay is reported as nonlinear",
      plan.nonlinear_upper == (("top_overlay", "base_color", "OVERLAY"),)
      and not plan.affine_fast_path)
reduced_scope = preview_stack.assess_lower_baseline_scope(plan)
check("reduced baseline scope rejects upper layers and mixed UVs",
      not reduced_scope.supported and len(reduced_scope.reasons) == 2,
      repr(reduced_scope))

# A version without Overlay is collapsible to C*x+D.
affine_stack = model.StackModel(
    root_tree_name="Affine", channels=("roughness",),
    layers=(top_scalar, active, lower_fill))
affine_plan = preview_stack.plan_resident_preview(
    affine_stack, "active", ("roughness",))
check("Mix/Multiply upper stack uses affine fast path",
      affine_plan.affine_fast_path)

implicit_active = model.LayerModel(
    uid="implicit", layer_type="PAINT",
    bindings=(binding("normal", image="Implicit Active"),))
named_lower = model.LayerModel(
    uid="named", layer_type="PAINT", uv_map="NamedUV",
    bindings=(binding("normal", image="Named Lower"),))
mixed_uv_stack = model.StackModel(
    "Mixed UV", ("normal",), (implicit_active, named_lower))
mixed_uv_plan = preview_stack.plan_resident_preview(
    mixed_uv_stack, "implicit", ("normal",))
check("implicit active UV plus named lower UV is conservatively non-fast",
      mixed_uv_plan.has_implicit_uv
      and not mixed_uv_plan.single_uv_fast_path)

same_uv_lower_only = model.StackModel(
    "Lower only", ("normal",),
    (active, kiln))
same_uv_plan = preview_stack.plan_resident_preview(
    same_uv_lower_only, "active", ("normal",))
check("same-UV active-topmost Kiln baseline enters reduced runtime scope",
      preview_stack.assess_lower_baseline_scope(same_uv_plan).supported)

# Runtime seam for the reduced release scope: active Base-only over a lower
# Kiln Normal proves baseline-only channels survive active-target filtering.
active_base_only = layer("active_base", "PAINT", [
    binding("base_color", image="Resident Base")
], uv_map="UV_A")
kiln_runtime_stack = model.StackModel(
    "Runtime", ("base_color", "normal"), (active_base_only, kiln))
runtime = gpu_engine.resident_stack_runtime_spec(
    kiln_runtime_stack, "active_base")
check("runtime accepts same-UV active-topmost lower baseline",
      runtime["enabled"], runtime["status"])
check("runtime preserves Kiln as baseline-only Normal channel",
      runtime["channels"]["normal"]["active"] is None
      and runtime["channels"]["normal"]["lower_steps"][0]["source"]
      == {"kind": "IMAGE", "image_name": "Kiln Normal",
          "use_alpha": False})
check("resident Kiln baseline ignores non-authoritative bake alpha",
      not runtime["channels"]["normal"]["lower_steps"][0]["source"]
      ["use_alpha"])

preview_src = gpu_engine.PREVIEW_FRAG_SRC
check("resolved active alpha is applied exactly once",
      "active_factor * source.a" in preview_src
      and "fragColor = vec4(rgb, preview_opacity)" in preview_src)
check("topmost preview cannot expose alpha seam holes",
      "float coverage = 1.0" not in preview_src
      and "coverage = max(coverage" not in preview_src)
check("active Base decodes once while baseline stays scene-linear",
      "if (decode_active_srgb > 0.5)" in preview_src
      and "source.rgb = srgb_to_linear(source.rgb)" in preview_src
      and "texture(baseline_tex, uvInterp)" in preview_src)
draw_src = __import__("inspect").getsource(gpu_engine._draw_composed_preview)
check("resolved channels remain enabled without active paint targets",
      'resolved or active or (key == "normal" and base_normal_enabled)'
      in draw_src)
baseline_build_src = __import__("inspect").getsource(
    gpu_engine._build_stack_baselines)
check("opaque lower images preserve raw RGB despite bake alpha",
      'image.pixels.foreach_get(pixels)' in baseline_build_src
      and 'pixels.reshape(-1, 4)[:, 3] = 1.0' in baseline_build_src
      and 'if source.get("use_alpha")' in baseline_build_src)

# Verify every affine formula and collapse against direct sequential blending.
steps = ((0.3, 0.25, "MIX"), (0.8, 0.5, "MULTIPLY"),
         (0.1, 0.4, "SCREEN"), (0.2, 0.3, "ADD"),
         (0.05, 0.2, "SUBTRACT"))
c, d = preview_stack.compose_affine_coefficients(steps)
for incoming in (0.0, 0.2, 0.7, 1.3):
    direct = incoming
    for source, factor, mode in steps:
        direct = preview_stack.blend_value(direct, source, factor, mode)
    check("affine collapse matches direct stack at %.1f" % incoming,
          abs(c * incoming + d - direct) < 1e-12)
try:
    preview_stack.affine_coefficients(0.8, 1.0, "OVERLAY")
    check("Overlay refuses affine collapse", False)
except ValueError:
    check("Overlay refuses affine collapse", True)

# Complete roughness composition: lower Fill -> resident active alpha -> top
# masked Multiply. The top mask conversion is supplied as an authoritative
# scalar, matching the compiler's implicit Color-to-Value socket conversion.
samples = {
    "Top Mask": preview_stack.PixelSample(0.25),
    "Active Rough": preview_stack.PixelSample(0.99, 1.0),
}
resident = {"active": {
    "roughness": preview_stack.PixelSample(0.2, 0.5),
}}
got_rough = preview_stack.compose_channel_pixel(
    affine_stack, "roughness", samples, resident)
lower = 0.8
active_result = preview_stack.blend_value(lower, 0.2, 0.5, "MIX")
mask_factor = 0.4 * (0.25 * 0.8 + 1.0 - 0.8)
expected_rough = preview_stack.blend_value(
    active_result, 0.75, mask_factor, "MULTIPLY")
check("resident active roughness composes over Fill and under masked top",
      abs(got_rough - expected_rough) < 1e-12,
      "got=%r expected=%r" % (got_rough, expected_rough))

# Kiln bottom normal -> lower/default-unbound layers -> active resident normal
# proves the baked normal is not erased when entering GPU paint.
kiln_only = model.StackModel(
    root_tree_name="Normal", channels=("normal",),
    layers=(active, kiln))
normal_samples = {
    "Kiln Normal": preview_stack.PixelSample((0.6, 0.4, 1.0, 1.0), 1.0),
}
resident_normal = {"active": {
    "normal": preview_stack.PixelSample((0.8, 0.5, 1.0, 1.0), 0.25),
}}
got_normal = preview_stack.compose_channel_pixel(
    kiln_only, "normal", normal_samples, resident_normal)
expected_normal = preview_stack.blend_tangent_normals_rnm(
    preview_stack.blend_tangent_normals_rnm(
        (0.5, 0.5, 1.0, 1.0), (0.6, 0.4, 1.0, 1.0), 1.0),
    (0.8, 0.5, 1.0, 1.0), 0.25)
check("resident normal layers over Kiln baked baseline",
      all(abs(a - b) < 1e-12 for a, b in
          zip(got_normal, expected_normal)), repr(got_normal))

# A newly active top normal canvas is transparent away from its strokes.  In
# those pixels Raw Tangent Normal, Neutral Normal Lighting, and Lit PBR must
# all receive the lower/Kiln result rather than the active canvas's neutral
# RGB.  The three modes branch only after the common normal_sample resolve.
transparent_active = {"active": {
    "normal": preview_stack.PixelSample((0.5, 0.5, 1.0, 1.0), 0.0),
}}
lower_only_normal = preview_stack.compose_channel_pixel(
    kiln_only, "normal", normal_samples, transparent_active)
resolved_kiln = preview_stack.blend_tangent_normals_rnm(
    (0.5, 0.5, 1.0, 1.0), normal_samples["Kiln Normal"].value, 1.0)
check("transparent active normal preserves Kiln normal for every preview",
      all(abs(a - b) < 1e-12 for a, b in zip(
          lower_only_normal, resolved_kiln)),
      repr(lower_only_normal))

opaque_active = {"active": {
    "normal": preview_stack.PixelSample((0.8, 0.5, 1.0, 1.0), 1.0),
}}
top_only_normal = preview_stack.compose_channel_pixel(
    kiln_only, "normal", normal_samples, opaque_active)
check("opaque active normal remains authoritative over Kiln normal",
      all(abs(a - b) < 1e-12 for a, b in zip(
          top_only_normal, preview_stack.blend_tangent_normals_rnm(
              resolved_kiln,
              opaque_active["active"]["normal"].value, 1.0))),
      repr(top_only_normal))

neutral = (0.5, 0.5, 1.0, 1.0)
tilt_x = (0.8, 0.5, 0.9, 1.0)
tilt_y = (0.5, 0.8, 0.9, 1.0)
combined_rnm = preview_stack.blend_tangent_normals_rnm(
    tilt_x, tilt_y, 1.0)
check("RNM preserves neutral identity and combines orthogonal detail",
      all(abs(a - b) < 1e-7 for a, b in zip(
          preview_stack.blend_tangent_normals_rnm(
              tilt_x, neutral, 1.0), tilt_x))
      and combined_rnm[0] > 0.5 and combined_rnm[1] > 0.5,
      repr(combined_rnm))
check("RNM zero factor preserves the lower normal",
      all(abs(a - b) < 1e-7 for a, b in zip(
          preview_stack.blend_tangent_normals_rnm(
              tilt_x, tilt_y, 0.0), tilt_x)))

normal_resolve_at = preview_src.index("vec4 normal_sample = resolve_stack_normal")
raw_at = preview_src.index("if (preview_mode == 1)")
decode_at = preview_src.index("if (has_normal > 0.5)")
neutral_at = preview_src.index("if (preview_mode == 2)")
pbr_at = preview_src.index("vec3 v = normalize(camera_position - worldPos)")
check("Raw, Neutral, and Lit all consume the same resolved normal stack",
      normal_resolve_at < decode_at < raw_at < neutral_at < pbr_at
      and "vec3 encoded_n = normal_sample.rgb" in
          preview_src[decode_at:raw_at]
      and "active_tangent_n" in preview_src[raw_at:neutral_at])

# Inverted mask and use_masks=False follow compiler factor semantics.
inverted = model.MaskModel(uid="inv", image_name="Inv", opacity=0.5,
                           invert=True)
masked_fill = layer("masked", "FILL", [
    binding("roughness", mode="VALUE", value=1.0)
], masks=(inverted,))
unmasked_fill = layer("unmasked", "FILL", [
    binding("roughness", mode="VALUE", value=1.0, use_masks=False)
], masks=(inverted,))
mask_samples = {"Inv": preview_stack.PixelSample(0.8)}
masked_stack = model.StackModel("M", ("roughness",), (masked_fill,))
unmasked_stack = model.StackModel("U", ("roughness",), (unmasked_fill,))
check("inverted mask applies opacity-folded factor",
      abs(preview_stack.compose_channel_pixel(
          masked_stack, "roughness", mask_samples) - 0.8) < 1e-12)
check("use_masks false bypasses image masks",
      preview_stack.compose_channel_pixel(
          unmasked_stack, "roughness", mask_samples) == 1.0)

# A Paint binding may use a constant VALUE while retaining its image as the
# per-channel paint-alpha gate. The compiler reads that image's Alpha even
# though its Color is not the channel source.
value_paint = layer("value_paint", "PAINT", [
    binding("roughness", mode="VALUE", image="Value Gate", value=1.0)
])
value_stack = model.StackModel("V", ("roughness",), (value_paint,))
value_samples = {"Value Gate": preview_stack.PixelSample(0.1, 0.25)}
check("Paint VALUE mode still uses its canvas alpha gate",
      abs(preview_stack.compose_channel_pixel(
          value_stack, "roughness", value_samples) - 0.625) < 1e-12)

print("IMPASTO_PREVIEW_STACK_PASSED")
