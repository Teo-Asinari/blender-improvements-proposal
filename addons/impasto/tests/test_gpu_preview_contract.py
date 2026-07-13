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
check("raw branches use branch-local single-channel samples",
      "raw_normal = straight_sample(normal_tex" in main
      and "raw_height = texture(height_tex" in main)
check("base/scalar PBR samples occur only after diagnostic modes",
      main.index("vec4 base = straight_sample") > neutral_at
      and main.index("vec4 metal_sample = straight_sample") > neutral_at
      and main.index("vec4 rough_sample = straight_sample") > neutral_at)

check("PBR channels resolve transparent pixels to neutral defaults",
      "mix(vec3(0.5), srgb_to_linear(base.rgb), base.a)" in src
      and "mix(0.0, metal_sample.r, metal_sample.a)" in src
      and "mix(0.5, rough_sample.r, rough_sample.a)" in src)
check("normal alpha blends encoded data toward a flat normal",
      "mix(vec3(0.5, 0.5, 1.0), normal_sample.rgb" in src)
check("degenerate and mirrored UVs have explicit handling",
      "abs(uv_det) > 1e-8" in src and "orientation = sign(uv_det)" in src
      and "cross(axis, geometric_n)" in src)
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
