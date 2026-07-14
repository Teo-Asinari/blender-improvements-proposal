# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure multi-channel brush value contract.

No bpy/gpu imports: resident GPU painting and native Blender replay consume the
same semantic brush dictionary while applying the appropriate color boundary.
"""

PAINTABLE_CHANNEL_KEYS = (
    "base_color", "metallic", "roughness", "normal", "height",
    "emission_color", "emission_strength",
    "sss_weight", "sss_radius", "sss_scale",
)


def linear_to_srgb(value):
    value = max(0.0, float(value))
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * value ** (1.0 / 2.4) - 0.055


def _scalar(value):
    value = float(value)
    return (value, value, value)


def resident_payload(channel_key, brush):
    """Return raw resident-canvas ``value`` and framebuffer ``blend``.

    sRGB canvases receive encoded values because resident GPU textures bypass
    Blender's Image Texture color transform. Non-Color scalar/vector values
    remain in their native domains, including HDR emission strength and scene
    distance for SSS Scale.
    """
    key = str(channel_key)
    if key not in PAINTABLE_CHANNEL_KEYS:
        raise ValueError("unpaintable channel %r" % key)
    if key == "base_color":
        value = tuple(linear_to_srgb(c) for c in
                      brush.get("color", (0.8, 0.2, 0.1)))
    elif key == "emission_color":
        value = tuple(linear_to_srgb(c) for c in
                      brush.get("emission_color", (1.0, 1.0, 1.0)))
    elif key == "roughness":
        value = _scalar(brush.get("roughness", 0.5))
    elif key == "metallic":
        value = _scalar(brush.get("metallic", 0.0))
    elif key == "normal":
        value = tuple(float(c) for c in
                      brush.get("normal", (0.5, 0.5, 1.0)))
    elif key == "height":
        step = float(brush.get("height_strength", 0.05))
        if brush.get("height_direction", "RAISE") == "LOWER":
            step = -step
        value = _scalar(step)
    elif key == "emission_strength":
        value = _scalar(brush.get("emission_strength", 0.0))
    elif key == "sss_weight":
        value = _scalar(brush.get("sss_weight", 0.0))
    elif key == "sss_radius":
        value = tuple(float(c) for c in
                      brush.get("sss_radius", (1.0, 0.2, 0.1)))
    else:  # sss_scale: Blender NodeSocketFloatDistance, scene units
        value = _scalar(brush.get("sss_scale", 0.05))
    return {"value": value,
            "strength": float(brush.get("strength", 1.0)),
            "blend": "ADD" if key == "height" else "MIX"}


def resident_payloads(channel_keys, brush):
    return [resident_payload(key, brush) for key in channel_keys]


def native_style(channel_key, brush):
    """Blender brush color/blend; sRGB UI colors stay scene-linear here."""
    key = str(channel_key)
    payload = resident_payload(key, brush)
    if key == "base_color":
        value = tuple(float(c) for c in
                      brush.get("color", (0.8, 0.2, 0.1)))
    elif key == "emission_color":
        value = tuple(float(c) for c in
                      brush.get("emission_color", (1.0, 1.0, 1.0)))
    elif key == "height":
        # Blender's SUB blend supplies the sign; its brush color is a
        # positive magnitude. The resident ADD framebuffer instead carries a
        # signed payload, hence this native boundary conversion.
        value = _scalar(abs(float(brush.get("height_strength", 0.05))))
    else:
        value = payload["value"]
    blend = ("ADD" if key == "height"
             and brush.get("height_direction", "RAISE") == "RAISE"
             else "SUB" if key == "height" else "MIX")
    return value, blend
