# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure emission/subsurface paint-value contract."""

import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "channel_paint.py"
spec = importlib.util.spec_from_file_location("impasto_channel_paint", path)
paint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paint)


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  ok  " + label)


brush = {
    "color": (0.5, 0.25, 0.1),
    "emission_color": (0.25, 0.5, 1.0),
    "emission_strength": 12.5,
    "sss_weight": 0.75,
    "sss_radius": (1.2, 0.35, 0.08),
    "sss_scale": 0.025,
    "strength": 0.4,
}
by_key = dict(zip(paint.PAINTABLE_CHANNEL_KEYS,
                  paint.resident_payloads(
                      paint.PAINTABLE_CHANNEL_KEYS, brush)))

check("ten paintable PBR channels are stable",
      len(paint.PAINTABLE_CHANNEL_KEYS) == 10)
check("emission color crosses the sRGB storage boundary once",
      by_key["emission_color"]["value"] == tuple(
          paint.linear_to_srgb(c) for c in brush["emission_color"]))
check("HDR emission strength is not clipped to display white",
      by_key["emission_strength"]["value"] == (12.5, 12.5, 12.5))
check("SSS weight stays a raw Non-Color factor",
      by_key["sss_weight"]["value"] == (0.75, 0.75, 0.75))
check("SSS radius preserves independent Non-Color vector components",
      by_key["sss_radius"]["value"] == (1.2, 0.35, 0.08))
check("SSS scale preserves scene-distance magnitude",
      by_key["sss_scale"]["value"] == (0.025, 0.025, 0.025))
check("all expanded channels use MIX and share stroke opacity",
      all(by_key[k]["blend"] == "MIX" and
          by_key[k]["strength"] == 0.4
          for k in paint.PAINTABLE_CHANNEL_KEYS if k != "height"))
check("native emission color remains linear for Blender color management",
      paint.native_style("emission_color", brush)
      == ((0.25, 0.5, 1.0), "MIX"))

print("IMPASTO_CHANNEL_PAINT_PASSED")
