# SPDX-License-Identifier: GPL-2.0-or-later
"""Front-surface visibility policy for projected GPU painting.

The depth prepass and UV-space paint pass sample the same surface at different
screen positions.  Comparing their linear depths with a near-zero constant
epsilon creates cracks on sloped surfaces.  Merely increasing that epsilon can
admit a nearby rear shell.  This module instead predicts front depth from an
exact, local texel neighborhood and only trusts gradients that look continuous.

``visible_surface`` is the pure Python reference used by tests.
``GLSL_SOURCE`` exposes the matching shader helper.  The GPU engine can prepend
it to a fragment source and replace its depth comparison with::

    if (!impasto_visible_surface(scene_depth_tex, suv, view_depth,
                                 depth_epsilon, depth_relative_epsilon)) {
        discard;
    }

The texture must contain positive linear view-space depth and use a large
clear sentinel (the current prepass uses ``1e30``).
"""

from dataclasses import dataclass
import math


CLEAR_DEPTH = 1.0e29


@dataclass(frozen=True)
class VisibilityPolicy:
    """Numerical policy shared by the reference and generated shader.

    ``continuity_relative`` is a hard ceiling for trusting a one-sided
    gradient.  Symmetric, sign-opposed samples may exceed it because they
    provide strong evidence for a steep continuous plane.
    """

    absolute_epsilon: float = 1.0e-4
    relative_epsilon: float = 1.0e-5
    continuity_relative: float = 2.0e-3
    continuity_floor_scale: float = 32.0
    symmetry_ratio: float = 4.0
    footprint_padding: float = 0.25


DEFAULT_POLICY = VisibilityPolicy()


def _valid(depth):
    return math.isfinite(depth) and 0.0 < depth < CLEAR_DEPTH


def _axis_gradient(center, negative, positive, base, policy):
    """Return a conservative depth-per-pixel gradient for one screen axis."""
    neg_valid = _valid(negative)
    pos_valid = _valid(positive)
    dn = negative - center if neg_valid else 0.0
    dp = positive - center if pos_valid else 0.0

    if neg_valid and pos_valid:
        an, ap = abs(dn), abs(dp)
        small = min(an, ap)
        large = max(an, ap)
        # A smooth plane straddles its centre.  The magnitude check stops an
        # occlusion jump on one side from masquerading as a steep slope.
        straddles = dn * dp <= 0.0
        balanced = large <= max(base, small) * policy.symmetry_ratio
        if straddles and balanced:
            return 0.5 * (dp - dn)

    limit = max(base * policy.continuity_floor_scale,
                abs(center) * policy.continuity_relative)
    choices = []
    if neg_valid and abs(dn) <= limit:
        choices.append(-dn)
    if pos_valid and abs(dp) <= limit:
        choices.append(dp)
    return min(choices, key=abs) if choices else 0.0


def visibility_diagnostics(candidate_depth, center_depth, neighbors,
                           subpixel=(0.0, 0.0), policy=DEFAULT_POLICY):
    """Return the policy decision and intermediate values.

    ``neighbors`` is ``(left, right, down, up)``. ``subpixel`` is the
    candidate position relative to the centre of the fetched depth texel,
    conventionally in ``[-0.5, 0.5]`` on each axis.
    """
    if not _valid(center_depth) or not _valid(candidate_depth):
        return {"visible": False, "predicted": center_depth,
                "tolerance": 0.0, "gradient": (0.0, 0.0)}

    base = max(policy.absolute_epsilon,
               abs(center_depth) * policy.relative_epsilon)
    left, right, down, up = neighbors
    gx = _axis_gradient(center_depth, left, right, base, policy)
    gy = _axis_gradient(center_depth, down, up, base, policy)
    ox = max(-0.5, min(0.5, float(subpixel[0])))
    oy = max(-0.5, min(0.5, float(subpixel[1])))
    predicted = center_depth + gx * ox + gy * oy
    # Prediction removes the first-order slope.  The residual allowance is
    # bounded to a fraction of one pixel, enough for raster/sample mismatch
    # without bridging a full depth discontinuity.
    tolerance = base + policy.footprint_padding * (abs(gx) + abs(gy))
    return {"visible": candidate_depth <= predicted + tolerance,
            "predicted": predicted, "tolerance": tolerance,
            "gradient": (gx, gy)}


def visible_surface(candidate_depth, center_depth, neighbors,
                    subpixel=(0.0, 0.0), policy=DEFAULT_POLICY):
    """Whether a projected fragment belongs to the front visible surface."""
    return visibility_diagnostics(candidate_depth, center_depth, neighbors,
                                  subpixel, policy)["visible"]


GLSL_SOURCE = r"""
const float IMPASTO_CLEAR_DEPTH = 1e29;

bool impasto_depth_valid(float z)
{
    /* Both comparisons are false for NaN; +infinity fails the upper bound. */
    return z > 0.0 && z < IMPASTO_CLEAR_DEPTH;
}

float impasto_axis_gradient(float center, float negative, float positive,
                            float base)
{
    bool nv = impasto_depth_valid(negative);
    bool pv = impasto_depth_valid(positive);
    float dn = nv ? negative - center : 0.0;
    float dp = pv ? positive - center : 0.0;
    if (nv && pv) {
        float small = min(abs(dn), abs(dp));
        float large = max(abs(dn), abs(dp));
        bool straddles = dn * dp <= 0.0;
        bool balanced = large <= max(base, small) * 4.0;
        if (straddles && balanced) {
            return 0.5 * (dp - dn);
        }
    }
    float limit = max(base * 32.0, abs(center) * 2e-3);
    float best = 0.0;
    float best_abs = 1e30;
    if (nv && abs(dn) <= limit && abs(dn) < best_abs) {
        best = -dn;
        best_abs = abs(dn);
    }
    if (pv && abs(dp) <= limit && abs(dp) < best_abs) {
        best = dp;
    }
    return best;
}

bool impasto_visible_surface(sampler2D depth_tex, vec2 suv,
                             float candidate_depth, float absolute_epsilon,
                             float relative_epsilon)
{
    ivec2 size = textureSize(depth_tex, 0);
    vec2 texel_pos = suv * vec2(size) - vec2(0.5);
    ivec2 p = clamp(ivec2(floor(suv * vec2(size))),
                    ivec2(0), size - ivec2(1));
    vec2 offset = clamp(texel_pos - vec2(p), vec2(-0.5), vec2(0.5));
    float c = texelFetch(depth_tex, p, 0).r;
    if (!impasto_depth_valid(c) || !impasto_depth_valid(candidate_depth)) {
        return false;
    }
    ivec2 lo = ivec2(0);
    ivec2 hi = size - ivec2(1);
    float l = texelFetch(depth_tex, clamp(p + ivec2(-1, 0), lo, hi), 0).r;
    float r = texelFetch(depth_tex, clamp(p + ivec2( 1, 0), lo, hi), 0).r;
    float d = texelFetch(depth_tex, clamp(p + ivec2( 0,-1), lo, hi), 0).r;
    float u = texelFetch(depth_tex, clamp(p + ivec2( 0, 1), lo, hi), 0).r;
    float base = max(absolute_epsilon, abs(c) * relative_epsilon);
    float gx = impasto_axis_gradient(c, l, r, base);
    float gy = impasto_axis_gradient(c, d, u, base);
    float predicted = c + gx * offset.x + gy * offset.y;
    float tolerance = base + 0.25 * (abs(gx) + abs(gy));
    return candidate_depth <= predicted + tolerance;
}
"""
