"""Print UV-health results for IMPASTO_DIAG_OBJECT in the opened .blend."""

import os
import sys

import bpy

ADDONS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from uv_island_overlay import health

name = os.environ.get("IMPASTO_DIAG_OBJECT", "Monster_Center_Crown")
obj = bpy.data.objects.get(name)
if obj is None:
    raise RuntimeError("Object not found: %r" % name)
result = health.analyze_object(obj, texture_size=4096,
                               low_density_ratio=0.5,
                               minimum_island_span_px=8.0)
for field, value in vars(result).items():
    if isinstance(value, frozenset):
        value = len(value)
    print("UV_HEALTH %s=%s" % (field, value))
print("UV_HEALTH_COMPLETE")
