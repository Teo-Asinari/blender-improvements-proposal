# SPDX-License-Identifier: GPL-2.0-or-later
"""Independent contract tests for the GPU-resident preview modes.

These are intentionally headless: they validate shader structure, mode-state
purity, and the numerical direction of diagnostic normal/height responses.
Actual framebuffer output remains a foreground acceptance test.
"""

import inspect
import math
import sys
from pathlib import Path

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import gpu_engine


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def normalize(v):
    length = math.sqrt(sum(x * x for x in v))
    return tuple(x / length for x in v)


def encoded_normal(rgb, alpha):
    neutral = (0.5, 0.5, 1.0)
    encoded = tuple(n + (c - n) * alpha for n, c in zip(neutral, rgb))
    return normalize(tuple(c * 2.0 - 1.0 for c in encoded))


def height_normal(dhdx, dhdy, scale=8.0):
    # Python mirror of cross((1,0,s*dhdx), (0,1,s*dhdy)).
    return normalize((-scale * dhdx, -scale * dhdy, 1.0))


expected = ("LIT_PBR", "RAW_TANGENT_NORMAL",
            "NEUTRAL_NORMAL_LIGHTING", "HEIGHT_GRAYSCALE")
check("preview mode identifiers and shader indices are stable",
      gpu_engine.PREVIEW_MODES == expected
      and [gpu_engine.preview_mode_index(m) for m in expected]
      == [0, 1, 2, 3])
check("invalid preview mode safely normalizes to Lit PBR",
      gpu_engine.normalize_preview_mode("unknown") == "LIT_PBR")

src = gpu_engine.PREVIEW_FRAG_SRC
main = src.split("void main()", 1)[1]
raw_normal_at = main.index("if (preview_mode == 1)")
raw_height_at = main.index("if (preview_mode == 3)")
detail_at = main.index("vec3 dpdx")
neutral_at = main.index("if (preview_mode == 2)")
pbr_at = main.index("vec3 v = normalize(camera_position - worldPos)")

check("raw diagnostics return before detail and PBR work",
      raw_normal_at < detail_at and raw_height_at < detail_at)
check("neutral detail mode returns before microfacet lighting",
      detail_at < neutral_at < pbr_at)
check("all diagnostic channels use resolved lower-plus-active samples",
      "resolve_stack_channel" in main
      and "vec3 encoded = normal_sample.rgb" in main
      and "float h = height_sample.r" in main)
check("base/scalar PBR channels reuse the resolved samples",
      "? base.rgb : vec3(0.5)" in main
      and "? metal_sample.r : 0.0" in main
      and "? rough_sample.r : 0.5" in main)
check("Lit PBR adds normal- and roughness-sensitive studio keys",
      "preview_key_light" in src
      and "distribution = a2" in src
      and "n, v, normalize(vec3" in main)
check("Lit PBR lighting is driven by compact live preview uniforms",
      'push_constant(\'VEC4\', "preview_lighting")' in
      inspect.getsource(gpu_engine.preview_shader_create_info)
      and "environment_intensity = exp2(preview_lighting.x)" in main
      and "rotate_around_z(reflection, preview_lighting.y)" in main
      and "preview_lighting.z" in main
      and "preview_fill.x" in main)
check("preview lighting updates do not rebuild or synchronize textures",
      "set_preview_lighting" in dir(gpu_engine)
      and "request_flush" not in
      inspect.getsource(gpu_engine.set_preview_lighting)
      and "environment_tex" not in
      inspect.getsource(gpu_engine.set_preview_lighting))

check("resident alpha gates the active layer exactly once",
      "active_factor * source.a" in src)
check("normal is decoded only after encoded-domain stack composition",
      "vec3 encoded_n = normal_sample.rgb" in src)
check("emission color and HDR strength remain independently resolved",
      "active_emission_color_blend, 1.0" in src
      and "active_emission_strength_blend, 0.0" in src
      and "rgb += emission_color * emission_strength" in src)
check("subsurface preview uses Weight and Radius-times-Scale distance",
      "vec3 scatter_distance = sss_radius * sss_scale" in src
      and "sss_weight * scatter_extent" in src
      and "sample_environment_panel(-environment_n, 0.0)" in src)
check("degenerate and mirrored UVs have explicit handling",
      "abs(uv_det) > 1e-8" in src and "orientation = sign(uv_det)" in src
      and "cross(axis, geometric_n)" in src)
check("Lit preview uses Blender corner normals instead of triangle normals",
      "surfaceNormal" in gpu_engine.PREVIEW_VERT_SRC
      and "geometric_n = normalize(surfaceNormal)" in src
      and "cross(dpdx, dpdy)" not in src)
check("resident preview rejects rear self-occluded fragments",
      "impasto_visible_surface(preview_depth_tex" in src
      and 'uniform_sampler("preview_depth_tex"' in
      inspect.getsource(gpu_engine._draw_composed_preview))
check("height uses screen derivatives rather than four neighbor taps",
      "dFdx(height)" in src and "dFdy(height)" in src
      and "uvInterp + vec2" not in src and "uvInterp - vec2" not in src)

# Normal alpha must be a strength interpolation, not a binary presence test.
tilt = (1.0, 0.5, 1.0)
flat = encoded_normal(tilt, 0.0)
quarter = encoded_normal(tilt, 0.25)
full = encoded_normal(tilt, 1.0)
check("zero-alpha encoded normal is exactly flat",
      flat == (0.0, 0.0, 1.0), repr(flat))
check("partial normal alpha produces intermediate tilt",
      0.0 < quarter[0] < full[0] and full[2] < quarter[2] < 1.0,
      "quarter=%r full=%r" % (quarter, full))

# Height directions should be symmetric, with 0.5/constant height neutral.
height_flat = height_normal(0.0, 0.0)
height_raise_x = height_normal(0.05, 0.0)
height_lower_x = height_normal(-0.05, 0.0)
check("constant height leaves the geometric normal unchanged",
      height_flat == (0.0, 0.0, 1.0))
check("opposite height derivatives produce opposite equal tilts",
      height_raise_x[0] == -height_lower_x[0]
      and abs(height_raise_x[2] - height_lower_x[2]) < 1e-12)

# Mode switching and drawing must remain resident-state operations. Looking at
# these narrow functions avoids false positives from explicit flush routines
# elsewhere in the module.
mode_source = inspect.getsource(gpu_engine.set_preview_mode)
draw_source = inspect.getsource(gpu_engine._draw_composed_preview)
forbidden = ("read_color", ".read(", "foreach_set", "flush_gpu",
             "pending_pixels")
check("mode switch performs no synchronization",
      not any(word in mode_source for word in forbidden), mode_source)
check("preview draw performs no synchronization",
      not any(word in draw_source for word in forbidden), draw_source)
check("preview mode is a draw-time integer uniform",
      'uniform_int("preview_mode"' in draw_source)

print("IMPASTO_GPU_PREVIEW_CONTRACT_PASSED")
