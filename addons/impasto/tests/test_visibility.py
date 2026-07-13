# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure reference tests for projected-paint front-surface visibility."""

import importlib.util
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "visibility.py"
spec = importlib.util.spec_from_file_location("impasto_visibility", MODULE)
visibility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visibility)


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def decision(candidate, center, neighbors, offset=(0.0, 0.0)):
    return visibility.visibility_diagnostics(
        candidate, center, neighbors, offset)


# Flat front face: raster noise inside the numerical floor is accepted, while
# another surface a millimetre behind it is not.
flat = (10.0, 10.0, 10.0, 10.0)
check("flat visible surface survives raster noise",
      decision(10.00008, 10.0, flat)["visible"])
check("flat rear surface is rejected",
      not decision(10.001, 10.0, flat)["visible"])

# Steep plane.  The old fixed comparison rejects 10.036 as hidden relative to
# centre depth 10; the local plane predicts precisely that depth at x=+0.45.
steep = (9.92, 10.08, 10.0, 10.0)
steep_result = decision(10.036, 10.0, steep, (0.45, 0.0))
check("steep continuous surface uses subpixel plane prediction",
      steep_result["visible"], repr(steep_result))
check("steep surface still rejects a rear fragment",
      not decision(10.08, 10.0, steep, (0.45, 0.0))["visible"])

# The depth raster and UV raster do not necessarily evaluate a curved or
# triangulated surface at precisely the same point.  Permit a bounded fraction
# of the local pixel footprint; the former epsilon-capped formula produced
# regular pinholes on steep/high-poly surfaces.
subpixel_result = decision(10.052, 10.0, steep, (0.45, 0.0))
check("subpixel raster mismatch uses local footprint allowance",
      subpixel_result["visible"], repr(subpixel_result))

# Curved surface: locally changing gradients remain crack-free when their
# subpixel residual lies inside the bounded raster floor.
curved = (9.9978, 10.0022, 9.9988, 10.0014)
curved_result = decision(10.00118, 10.0, curved, (0.4, 0.25))
check("curved surface accepts bounded local residual",
      curved_result["visible"], repr(curved_result))

# A thin foreground feature surrounded by a rear shell must not derive a
# giant false gradient from that shell.  Same-sign, discontinuous neighbor
# changes fail both the symmetric-plane and one-sided continuity gates.
thin = (10.05, 10.05, 10.05, 10.05)
thin_result = decision(10.05, 10.0, thin, (0.45, 0.0))
check("thin feature does not bridge to surrounding rear shell",
      not thin_result["visible"] and thin_result["gradient"] == (0.0, 0.0),
      repr(thin_result))

# At a silhouette, clear sentinels are ignored.  The valid inward samples can
# establish tangential slope, but cannot inflate tolerance toward empty space.
clear = 1.0e30
silhouette = (9.99, clear, 10.0, 10.0)
silhouette_result = decision(10.00008, 10.0, silhouette, (0.45, 0.0))
check("silhouette ignores clear depth neighbors",
      silhouette_result["visible"]
      and abs(silhouette_result["gradient"][0]) < 0.02,
      repr(silhouette_result))
check("silhouette rejects a hidden candidate",
      not decision(10.01, 10.0, silhouette, (0.45, 0.0))["visible"])

# Self-occlusion boundary: a rear sample on one side must not contaminate the
# continuous front-face derivative from the other axis.
occluded = (9.999, 10.2, 9.9995, 10.0005)
occ_result = decision(10.0002, 10.0, occluded, (0.2, 0.4))
check("occlusion jump is excluded from gradient",
      occ_result["visible"] and abs(occ_result["gradient"][0]) < 0.002,
      repr(occ_result))
check("occluded rear fragment is rejected",
      not decision(10.2, 10.0, occluded, (0.2, 0.4))["visible"])

# Shader contract: exact texel fetches avoid filtering across silhouettes.
glsl = visibility.GLSL_SOURCE
check("shader samples exact depth texels",
      "texelFetch(depth_tex" in glsl and "texture(depth_tex" not in glsl)
check("shader excludes invalid and clear depth",
      "IMPASTO_CLEAR_DEPTH" in glsl and "impasto_depth_valid" in glsl)
check("shader exposes integration helper",
      "bool impasto_visible_surface" in glsl)

print("IMPASTO_VISIBILITY_PASSED")
