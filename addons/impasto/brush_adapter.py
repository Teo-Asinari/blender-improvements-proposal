# SPDX-License-Identifier: GPL-2.0-or-later
"""Translate accessible Blender image-brush state into GPU stamp data.

This module deliberately has no ``bpy`` or ``gpu`` dependency.  The caller
passes Blender RNA objects (or test doubles) and receives immutable plain
data that can safely be consumed by the GPU paint session.  It does not try
to emulate tools whose result depends on existing canvas or clone state.
"""

from dataclasses import dataclass


SUPPORTED_TOOLS = frozenset({"DRAW"})
UNSUPPORTED_TOOLS = {
    "CLONE": "clone-source transforms require a dedicated GPU tool",
    "SMEAR": "smear samples and transports existing canvas pixels",
    "SOFTEN": "soften requires neighborhood filtering of the canvas",
    "FILL": "fill is a region operation, not a sequence of stamps",
    "GRADIENT": "gradient is a two-point region operation",
    "MASK": "mask painting needs a separate mask target and semantics",
}


@dataclass(frozen=True)
class TextureStampData:
    """Stable description of Blender's exposed brush texture slot."""

    texture_name: str
    texture_type: str
    image_name: str
    mapping: str
    angle: float
    offset: tuple
    scale: tuple


@dataclass(frozen=True)
class GPUStampData:
    """Blender brush inputs needed to generate pressure-aware GPU dabs."""

    supported: bool
    tool: str
    unsupported_reason: str
    radius_px: float
    strength: float
    alpha: float
    spacing_ratio: float
    color: tuple
    secondary_color: tuple
    blend: str
    use_pressure_size: bool
    use_pressure_strength: bool
    falloff_curve: tuple
    texture: object

    def values_at_pressure(self, pressure):
        """Return ``(radius_px, opacity)`` for a normalized pen pressure."""
        pressure = min(1.0, max(0.0, float(pressure)))
        radius = self.radius_px * (pressure if self.use_pressure_size else 1.0)
        opacity = self.strength * self.alpha
        if self.use_pressure_strength:
            opacity *= pressure
        return max(0.5, radius), min(1.0, max(0.0, opacity))

    @property
    def spacing_px(self):
        """Nominal center-to-center spacing for a full-pressure stamp."""
        return max(1.0, 2.0 * self.radius_px * self.spacing_ratio)


def _value(owner, name, default):
    value = getattr(owner, name, default) if owner is not None else default
    return default if value is None else value


def _vector(value, length, default):
    try:
        result = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return default
    return result[:length] if len(result) >= length else default


def _uses_unified(unified, flag):
    return bool(unified is not None and getattr(unified, flag, False))


def _effective(brush, unified, flag, name, default):
    owner = unified if _uses_unified(unified, flag) else brush
    return _value(owner, name, default)


def identify_tool(brush, tool_id=""):
    """Return Blender's normalized image-paint tool identifier.

    Blender versions/assets expose this either through ``image_tool`` or the
    active workspace tool id.  Unknown values are intentionally unsupported.
    """
    value = _value(brush, "image_tool", "") or tool_id
    value = str(value).upper()
    aliases = {
        "BUILTIN.BRUSH": "DRAW",
        "PAINT.TEXTURE_PAINT": "DRAW",
        "DRAW_BRUSH": "DRAW",
    }
    value = aliases.get(value, value or "UNKNOWN")
    # Workspace/asset identifiers vary across Blender releases.  Normalize a
    # semantic suffix where one is actually exposed, but never infer a tool
    # from the brush's display name.
    for semantic in tuple(SUPPORTED_TOOLS) + tuple(UNSUPPORTED_TOOLS):
        if value == semantic or value.endswith("." + semantic):
            return semantic
    return value


def _falloff_points(brush):
    curve = (_value(brush, "curve_distance_falloff", None)
             or _value(brush, "curve", None))
    points = getattr(curve, "points", ()) if curve is not None else ()
    curves = getattr(curve, "curves", ()) if curve is not None else ()
    if not points and curves:
        points = getattr(curves[0], "points", ())
    result = []
    for point in points:
        location = getattr(point, "location", point)
        xy = _vector(location, 2, None)
        if xy is not None:
            result.append(xy)
    # The empty tuple means "use the engine's standard smooth falloff".
    return tuple(result)


def _texture_data(brush):
    slot = _value(brush, "texture_slot", None)
    texture = _value(slot, "texture", None)
    if texture is None:
        texture = _value(brush, "texture", None)
    if texture is None:
        return None
    image = _value(texture, "image", None)
    return TextureStampData(
        texture_name=str(_value(texture, "name", "")),
        texture_type=str(_value(texture, "type", "UNKNOWN")),
        image_name=str(_value(image, "name", "")),
        mapping=str(_value(slot, "map_mode", "VIEW_PLANE")),
        angle=float(_value(slot, "angle", 0.0)),
        offset=_vector(_value(slot, "offset", (0.0, 0.0, 0.0)), 3,
                       (0.0, 0.0, 0.0)),
        scale=_vector(_value(slot, "scale", (1.0, 1.0, 1.0)), 3,
                      (1.0, 1.0, 1.0)),
    )


def brush_to_gpu_stamp(brush, unified=None, tool_id=""):
    """Convert Blender 5.1 brush RNA into an immutable GPU stamp plan.

    ``size`` is Blender's diameter, while Impasto's shaders consume radius.
    Blender spacing is a percentage of brush diameter and is normalized here.
    Texture metadata is mapped, but the integration layer remains responsible
    for uploading/sampling the referenced image or procedural texture.
    """
    tool = identify_tool(brush, tool_id)
    supported = tool in SUPPORTED_TOOLS
    reason = "" if supported else UNSUPPORTED_TOOLS.get(
        tool, "the active Blender brush tool has no GPU stamp adapter")
    size = float(_effective(brush, unified, "use_unified_size", "size", 50.0))
    strength = float(_effective(
        brush, unified, "use_unified_strength", "strength", 1.0))
    color_owner = unified if _uses_unified(unified, "use_unified_color") else brush
    color = _vector(_value(color_owner, "color", (1.0, 1.0, 1.0)), 3,
                    (1.0, 1.0, 1.0))
    secondary = _vector(
        _value(color_owner, "secondary_color", (0.0, 0.0, 0.0)), 3,
        (0.0, 0.0, 0.0))
    spacing = min(10.0, max(0.001, float(_value(brush, "spacing", 10.0)) / 100.0))
    return GPUStampData(
        supported=supported,
        tool=tool,
        unsupported_reason=reason,
        radius_px=max(0.5, size * 0.5),
        strength=min(1.0, max(0.0, strength)),
        alpha=min(1.0, max(0.0, float(_value(brush, "alpha", 1.0)))),
        spacing_ratio=spacing,
        color=color,
        secondary_color=secondary,
        blend=str(_value(brush, "blend", "MIX")),
        use_pressure_size=bool(_value(brush, "use_pressure_size", False)),
        use_pressure_strength=bool(
            _value(brush, "use_pressure_strength", False)),
        falloff_curve=_falloff_points(brush),
        texture=_texture_data(brush),
    )
