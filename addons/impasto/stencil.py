# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure contract for GPU brush alpha images and viewport stencils.

The GPU engine consumes the normalized dictionary returned here. Keeping the
transform math pure makes the exact projection semantics testable headlessly.
"""

from dataclasses import dataclass
import math


PROJECTION_ITEMS = (
    ('VIEW_STENCIL', "Planar Viewport",
     "Fix the image in viewport space; brush strokes reveal it wherever "
     "they cross the stencil"),
    ('BRUSH_ALPHA', "Brush Footprint",
     "Center the image on every dab as a textured brush tip"),
)
PROJECTION_IDS = frozenset(item[0] for item in PROJECTION_ITEMS)

INTERPRETATION_ITEMS = (
    ('ALPHA', "Alpha Channel",
     "Use variation in the image alpha channel; a fully opaque image is "
     "flat, even when its visible RGB pixels are grayscale"),
    ('LUMINANCE', "Grayscale",
     "Use visible RGB brightness; choose this for an opaque grayscale image"),
)
INTERPRETATION_IDS = frozenset(item[0] for item in INTERPRETATION_ITEMS)

USAGE_ITEMS = (
    ('COVERAGE', "Paint Coverage",
     "Multiply the shared opacity of every painted channel"),
    ('NORMAL_PROFILE', "Normal Relief",
     "Treat image intensity as height and derive tangent-normal detail "
     "from its gradients; Alpha Channel requires varying transparency, while "
     "opaque grayscale images require Grayscale; Normal receives relief and "
     "the image still controls coverage for every other enabled channel"),
)
USAGE_IDS = frozenset(item[0] for item in USAGE_ITEMS)


@dataclass(frozen=True)
class StencilSettings:
    enabled: bool = False
    image_name: str = ""
    projection: str = 'VIEW_STENCIL'
    interpretation: str = 'ALPHA'
    usage: str = 'COVERAGE'
    opacity: float = 1.0
    position: tuple = (0.5, 0.5)
    scale: tuple = (0.35, 0.35)
    rotation: float = 0.0
    profile_strength: float = 1.0
    profile_invert: bool = False

    @property
    def active(self):
        return bool(self.enabled and self.image_name)

    def as_gpu_settings(self):
        return {
            "stencil_enabled": self.active,
            "stencil_image_name": self.image_name,
            "stencil_projection": self.projection,
            "stencil_interpretation": self.interpretation,
            "stencil_usage": self.usage,
            "stencil_opacity": self.opacity,
            "stencil_position": self.position,
            "stencil_scale": self.scale,
            "stencil_rotation": self.rotation,
            "stencil_profile_strength": self.profile_strength,
            "stencil_profile_invert": self.profile_invert,
        }


def _pair(value, default):
    try:
        value = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return default
    return value[:2] if len(value) >= 2 else default


def normalized(enabled=False, image_name="", projection='VIEW_STENCIL',
               interpretation='ALPHA', opacity=1.0, position=(0.5, 0.5),
               scale=(0.35, 0.35), rotation=0.0, usage='COVERAGE',
               profile_strength=1.0, profile_invert=False):
    """Return a clamped immutable stencil contract."""
    projection = str(projection).upper()
    if projection not in PROJECTION_IDS:
        projection = 'VIEW_STENCIL'
    interpretation = str(interpretation).upper()
    if interpretation not in INTERPRETATION_IDS:
        interpretation = 'ALPHA'
    usage = str(usage).upper()
    if usage not in USAGE_IDS:
        usage = 'COVERAGE'
    position = _pair(position, (0.5, 0.5))
    scale = _pair(scale, (0.35, 0.35))
    return StencilSettings(
        enabled=bool(enabled),
        image_name=str(image_name or ""),
        projection=projection,
        interpretation=interpretation,
        usage=usage,
        opacity=min(1.0, max(0.0, float(opacity))),
        position=position,
        scale=(max(0.001, abs(scale[0])), max(0.001, abs(scale[1]))),
        rotation=float(rotation),
        profile_strength=max(0.0, float(profile_strength)),
        profile_invert=bool(profile_invert),
    )


def image_uv(fragment_px, brush_center_px, brush_radius_px, region_size,
             settings):
    """Image UV for one viewport fragment, or ``None`` outside the image.

    VIEW_STENCIL position and scale are normalized viewport coordinates.
    BRUSH_ALPHA ignores position; scale multiplies the brush diameter on X/Y.
    Rotation is counter-clockwise in radians. Image UV origin is bottom-left,
    matching Blender GPU textures and the viewport region.
    """
    if not settings.active:
        return None
    px = _pair(fragment_px, (0.0, 0.0))
    center = _pair(brush_center_px, (0.0, 0.0))
    region = _pair(region_size, (1.0, 1.0))
    if settings.projection == 'VIEW_STENCIL':
        center = (settings.position[0] * region[0],
                  settings.position[1] * region[1])
        half = (0.5 * settings.scale[0] * region[0],
                0.5 * settings.scale[1] * region[1])
    else:
        radius = max(0.5, float(brush_radius_px))
        half = (radius * settings.scale[0], radius * settings.scale[1])
    dx, dy = px[0] - center[0], px[1] - center[1]
    c, s = math.cos(settings.rotation), math.sin(settings.rotation)
    # Inverse rotation: transform viewport point into image-local space.
    local_x = c * dx + s * dy
    local_y = -s * dx + c * dy
    uv = (local_x / (2.0 * half[0]) + 0.5,
          local_y / (2.0 * half[1]) + 0.5)
    if uv[0] < 0.0 or uv[0] > 1.0 or uv[1] < 0.0 or uv[1] > 1.0:
        return None
    return uv


def interpreted_mask(rgba, interpretation='ALPHA', opacity=1.0):
    """CPU mirror of shader mask interpretation for deterministic tests."""
    rgba = tuple(float(v) for v in rgba)
    if str(interpretation).upper() == 'LUMINANCE':
        value = (0.2126 * rgba[0] + 0.7152 * rgba[1]
                 + 0.0722 * rgba[2])
    else:
        value = rgba[3] if len(rgba) > 3 else 1.0
    return min(1.0, max(0.0, value * float(opacity)))


def profile_tangent_normal(left, right, down, up, strength=1.0,
                           invert=False, image_size=(1.0, 1.0)):
    """Encoded tangent normal derived from a sampled height profile.

    This mirrors the GPU shader's central-difference polarity and
    normalization for 3DCoat-style normal-detail alphas.
    """
    sign = -1.0 if invert else 1.0
    size = _pair(image_size, (1.0, 1.0))
    # The samples are one source texel apart on either side. Multiplying the
    # central difference by each axis' texel count converts it from height per
    # texel to height per normalized image UV. This keeps a resampled stencil's
    # relief strength stable and treats non-square images correctly.
    dx = ((float(right) - float(left)) * 0.5 * max(1.0, size[0])
          * float(strength) * sign)
    dy = ((float(up) - float(down)) * 0.5 * max(1.0, size[1])
          * float(strength) * sign)
    x, y, z = -dx, -dy, 1.0
    length = math.sqrt(x * x + y * y + z * z)
    return (x / length * 0.5 + 0.5,
            y / length * 0.5 + 0.5,
            z / length * 0.5 + 0.5)
