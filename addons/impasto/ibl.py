# SPDX-License-Identifier: GPL-2.0-or-later
"""Small deterministic studio environment for Impasto's live GPU preview.

Blender does not expose Material Preview's already-prefiltered studio cubemap
as a public Python GPUTexture.  This module therefore builds a compact linear
HDR equirectangular atlas: row 0 is diffuse irradiance and rows 1..5 are
specular radiance at increasing roughness.  Generation is CPU-only, cached,
and independent of bpy/gpu so it remains straightforward to test.
"""

import math


ATLAS_WIDTH = 128
ATLAS_PANEL_HEIGHT = 64
SPECULAR_LEVELS = 5
ATLAS_PANELS = 1 + SPECULAR_LEVELS

_cached_atlas = None


def _normalized(value):
    length = math.sqrt(sum(component * component for component in value))
    return tuple(component / length for component in value)


_LIGHTS = (
    (_normalized((0.45, -0.32, 0.84)), (8.0, 6.3, 4.8), 90.0),
    (_normalized((-0.72, -0.08, 0.68)), (1.8, 2.8, 5.4), 42.0),
    (_normalized((0.05, 0.88, 0.47)), (4.2, 1.7, 0.65), 65.0),
)


def studio_radiance(direction, roughness, diffuse=False):
    """Analytic linear-HDR studio radiance used to populate the image atlas."""
    x, y, z = _normalized(direction)
    roughness = min(1.0, max(0.0, float(roughness)))
    sky = max(0.0, z)
    ground = max(0.0, -z)
    horizon = max(0.0, 1.0 - abs(z))
    color = [
        0.025 + 0.055 * sky + 0.018 * ground + 0.025 * horizon,
        0.028 + 0.075 * sky + 0.014 * ground + 0.030 * horizon,
        0.035 + 0.125 * sky + 0.010 * ground + 0.045 * horizon,
    ]
    for axis, amplitude, sharpness in _LIGHTS:
        dot = max(-1.0, min(1.0, x * axis[0] + y * axis[1] + z * axis[2]))
        blur = roughness * roughness * (0.75 if not diffuse else 1.6)
        effective = sharpness / (1.0 + sharpness * blur)
        # Approximate spherical-Gaussian convolution while retaining energy.
        energy = max(0.055, effective / sharpness)
        lobe = math.exp(effective * (dot - 1.0)) * energy
        for channel in range(3):
            color[channel] += amplitude[channel] * lobe
    if diffuse:
        # Diffuse irradiance is broad and intentionally lower contrast.
        color = [component * 0.82 for component in color]
    return tuple(color)


def build_environment_atlas(width=ATLAS_WIDTH,
                            panel_height=ATLAS_PANEL_HEIGHT):
    """Return float32 RGBA atlas shaped ``(panels*height, width, 4)``."""
    import numpy as np

    global _cached_atlas
    if (width == ATLAS_WIDTH and panel_height == ATLAS_PANEL_HEIGHT
            and _cached_atlas is not None):
        return _cached_atlas
    atlas = np.empty((ATLAS_PANELS * panel_height, width, 4),
                     dtype=np.float32)
    u = (np.arange(width, dtype=np.float32) + 0.5) / width
    v = (np.arange(panel_height, dtype=np.float32) + 0.5) / panel_height
    longitude, latitude = np.meshgrid(
        (u - 0.5) * (2.0 * np.pi), (v - 0.5) * np.pi)
    cos_latitude = np.cos(latitude)
    directions = np.stack((cos_latitude * np.cos(longitude),
                           cos_latitude * np.sin(longitude),
                           np.sin(latitude)), axis=-1)
    z = directions[..., 2]
    sky, ground = np.maximum(z, 0.0), np.maximum(-z, 0.0)
    horizon = np.maximum(1.0 - np.abs(z), 0.0)
    background = np.stack((
        0.025 + 0.055 * sky + 0.018 * ground + 0.025 * horizon,
        0.028 + 0.075 * sky + 0.014 * ground + 0.030 * horizon,
        0.035 + 0.125 * sky + 0.010 * ground + 0.045 * horizon,
    ), axis=-1)
    for panel in range(ATLAS_PANELS):
        diffuse = panel == 0
        roughness = (1.0 if diffuse else
                     (panel - 1) / float(SPECULAR_LEVELS - 1))
        color = background.copy()
        for axis, amplitude, sharpness in _LIGHTS:
            dot = np.clip(np.sum(directions * np.asarray(axis), axis=-1),
                          -1.0, 1.0)
            blur = roughness * roughness * (1.6 if diffuse else 0.75)
            effective = sharpness / (1.0 + sharpness * blur)
            energy = max(0.055, effective / sharpness)
            lobe = np.exp(effective * (dot - 1.0)) * energy
            color += lobe[..., None] * np.asarray(amplitude)
        if diffuse:
            color *= 0.82
        start = panel * panel_height
        atlas[start:start + panel_height, :, :3] = color
        atlas[start:start + panel_height, :, 3] = 1.0
    if width == ATLAS_WIDTH and panel_height == ATLAS_PANEL_HEIGHT:
        _cached_atlas = atlas
    return atlas


def atlas_v(local_v, panel):
    """Pure reference mapping from panel-local V to full-atlas V."""
    return (int(panel) + min(1.0, max(0.0, float(local_v)))) / ATLAS_PANELS
