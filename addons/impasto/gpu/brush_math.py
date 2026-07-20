# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure brush sampling mathematics used by the GPU painting runtime."""

import math


def brush_falloff(t, hardness):
    """Return brush alpha at normalized distance ``t`` from the center."""
    h = min(max(hardness, 0.0), 0.999)
    if t <= h:
        return 1.0
    if t >= 1.0:
        return 0.0
    u = (t - h) / (1.0 - h)
    return 1.0 - (u * u * (3.0 - 2.0 * u))


def interpolate_dabs(x0, y0, x1, y1, spacing, leftover=0.0,
                     max_dabs=256):
    """Return evenly spaced dab positions and carried segment distance."""
    dx = x1 - x0
    dy = y1 - y0
    dist = math.hypot(dx, dy)
    if dist <= 0.0 or spacing <= 0.0:
        return [], leftover
    out = []
    s = spacing - leftover
    while s <= dist and len(out) < max_dabs:
        t = s / dist
        out.append((x0 + dx * t, y0 + dy * t, t))
        s += spacing
    return out, dist - (s - spacing)


def dab_spacing(radius_px, spacing_factor=0.25, minimum_px=2.0):
    return max(minimum_px, radius_px * spacing_factor)


def sanitize_pressure(value, fallback=1.0):
    """Clamp pressure and replace transient zero or invalid samples."""
    try:
        pressure = float(value)
    except (TypeError, ValueError):
        pressure = float(fallback)
    if not math.isfinite(pressure) or pressure <= 0.0:
        pressure = float(fallback)
    return min(1.0, max(0.001, pressure))


def overlap_compensated_opacity(target, spacing_ratio):
    """Return per-dab alpha whose repeated source-over approaches target."""
    target = min(1.0, max(0.0, float(target)))
    if target >= 1.0:
        return 1.0
    spacing = min(1.0, max(1e-4, float(spacing_ratio)))
    return 1.0 - (1.0 - target) ** spacing

