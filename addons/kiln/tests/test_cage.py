# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless geometry/lifecycle tests for Kiln's explicit cage guide."""

import os
import sys
import traceback

import bpy

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    import kiln
    from kiln import cage
    kiln.register()
    try:
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        low = bpy.context.active_object
        low.name = "CageLow"

        outer, inner = cage.build_guides(
            bpy.context, low, 0.2, 0.7, False)
        check("outer guide created without a fictitious inner shell",
              outer is not None and inner is None)
        check("outer topology exactly matches low",
              len(outer.data.vertices) == len(low.data.vertices)
              and len(outer.data.polygons) == len(low.data.polygons))
        check("guides share the low transform",
              outer.matrix_world == low.matrix_world)
        check("guides are wire, in-front, non-rendering",
              outer.display_type == 'WIRE' and outer.show_in_front
              and outer.hide_render)
        for source, out in zip(low.data.vertices, outer.data.vertices):
            delta = out.co - source.co
            check("outer vertex offset is extrusion along normal",
                  abs(delta.dot(source.normal) - 0.2) < 1e-5)
            break
        try:
            cage.build_guides(bpy.context, low, 0.0, 0.7, False)
            zero_rejected = False
        except cage.CageError as exc:
            zero_rejected = "greater than zero" in str(exc)
        check("zero-distance explicit cage is rejected actionably",
              zero_rejected)

        group = cage.ensure_paint_group(low)
        check("paint group initialized neutral at weight 0.5",
              group.name == cage.PAINT_GROUP
              and all(abs(group.weight(v.index) - 0.5) < 1e-6
                      for v in low.data.vertices))
        factors = cage.painted_factors(low, True)
        check("neutral painted weights mean 1x", all(
            abs(f - 1.0) < 1e-6 for f in factors))
        group.add([0], 0.0, 'REPLACE')
        factors = cage.painted_factors(low, True)
        check("paint weight zero retains non-degenerate 0.05x floor",
              abs(factors[0] - 0.05) < 1e-6)
        group.add([0], 1.0, 'REPLACE')
        outer2, _ = cage.build_guides(
            bpy.context, low, 0.2, 0.7, True)
        delta = outer2.data.vertices[0].co - low.data.vertices[0].co
        check("paint weight 1 doubles local extrusion",
              abs(delta.dot(low.data.vertices[0].normal) - 0.4) < 1e-5)
        check("refresh reuses the stable outer object", outer2 == outer)

        cage.hide_guides(low)
        check("hide guide preserves but hides objects",
              not cage.guides_visible(low)
              and cage._find(low, cage.OUTER_ROLE) is not None)
        cage.remove_all_guides()
        check("guide cleanup removes derived objects",
              cage._find(low, cage.OUTER_ROLE) is None
              and cage._find(low, cage.INNER_ROLE) is None)
    finally:
        kiln.unregister()


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

if FAILURES:
    print("CAGE_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("CAGE_TESTS_PASSED")
