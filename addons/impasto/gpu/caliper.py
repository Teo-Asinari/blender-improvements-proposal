# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure layout calculations for the subsurface-scattering caliper."""


def sss_caliper_layout(scale, radius_rgb, pixels_per_unit, bbox_diagonal,
                       warning_mesh_fraction=0.001):
    """Return literal effective distances, pixels, percentages and warning."""
    scale = max(0.0, float(scale))
    effective = tuple(scale * max(0.0, float(v)) for v in radius_rgb[:3])
    largest = max(effective, default=0.0)
    pixels = tuple(v * max(0.0, pixels_per_unit) for v in effective)
    pct = tuple((100.0 * v / bbox_diagonal) if bbox_diagonal > 0.0 else 0.0
                for v in effective)
    too_small = bool(largest > 0.0 and bbox_diagonal > 0.0
                     and largest / bbox_diagonal
                     < max(0.0, warning_mesh_fraction))
    return effective, pixels, pct, too_small

