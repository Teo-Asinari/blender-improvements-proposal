# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless unit tests for flowcore (pure logic: heuristics, naming,
path resolution, UV degeneracy math, scale checks, decimate ratio).

Prints CORE_TESTS_PASSED on success.
"""

import os
import sys
import traceback

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
    import numpy as np

    from kiln import flowcore

    # --- purity: flowcore must not import bpy or gpu ------------------------
    src = open(os.path.join(_ADDON_DIR, "flowcore.py")).read()
    check("flowcore.py imports neither bpy nor gpu",
          "import bpy" not in src and "import gpu" not in src)

    # --- bake type table -------------------------------------------------------
    check("BAKE_TYPES has NORMAL only (for now), non-color, suffix",
          set(flowcore.BAKE_TYPES) == {'NORMAL'}
          and flowcore.BAKE_TYPES['NORMAL']["non_color"] is True
          and flowcore.BAKE_TYPES['NORMAL']["suffix"] == "normal")
    check("extension TODO documented at the switch point",
          "TODO: AO, cavity/curvature, displacement" in src)

    # --- auto distance heuristic ------------------------------------------------
    ext, ray = flowcore.auto_distances(10.0)
    check("auto distances: 2% extrusion, 4% max ray of the diagonal",
          abs(ext - 0.2) < 1e-12 and abs(ray - 0.4) < 1e-12,
          "got %r %r" % (ext, ray))
    check("max ray is exactly 2x the extrusion",
          abs(ray - 2.0 * ext) < 1e-12)
    e2, r2 = flowcore.auto_distances(20.0)
    check("heuristic scales linearly with the diagonal",
          abs(e2 - 2 * ext) < 1e-12 and abs(r2 - 2 * ray) < 1e-12)
    check("zero/negative diagonal clamps to zero",
          flowcore.auto_distances(0.0) == (0.0, 0.0)
          and flowcore.auto_distances(-5.0) == (0.0, 0.0))
    check("factors match the documented constants",
          flowcore.EXTRUSION_FACTOR == 0.02
          and flowcore.MAX_RAY_FACTOR == 0.04)

    # --- combined diagonal ----------------------------------------------------
    pts = [(0, 0, 0), (1, 0, 0), (0, 2, 0), (0, 0, 2), (1, 2, 2)]
    check("combined diagonal of a 1x2x2 box",
          abs(flowcore.combined_diagonal(pts) - 3.0) < 1e-12,
          "got %r" % flowcore.combined_diagonal(pts))
    check("diagonal of <2 points is 0",
          flowcore.combined_diagonal([]) == 0.0
          and flowcore.combined_diagonal([(1, 1, 1)]) == 0.0)

    # --- naming ------------------------------------------------------------------
    check("image name is <low>_<suffix>",
          flowcore.image_name("Golem_low", 'NORMAL') == "Golem_low_normal")
    check("default relpath is textures/<low>_normal.png",
          flowcore.default_relpath("Golem_low", 'NORMAL')
          == os.path.join("textures", "Golem_low_normal.png"))

    # --- output path resolution ---------------------------------------------------
    sep = os.sep
    p, err = flowcore.resolve_output_path("", "", "Low", 'NORMAL')
    check("empty path + unsaved blend -> actionable error",
          p is None and err is not None and "unsaved" in err
          and "absolute" in err)
    p, err = flowcore.resolve_output_path("", sep + "proj", "Low", 'NORMAL')
    check("empty path + saved blend -> <blend>/textures/<low>_normal.png",
          err is None
          and p == os.path.normpath(os.path.join(
              sep + "proj", "textures", "Low_normal.png")),
          "got %r" % p)
    p, err = flowcore.resolve_output_path("//maps/n.png", sep + "proj",
                                          "Low", 'NORMAL')
    check("//-relative resolves against the blend dir",
          err is None and p == os.path.normpath(
              os.path.join(sep + "proj", "maps", "n.png")), "got %r" % p)
    p, err = flowcore.resolve_output_path("//maps/n.png", "", "Low",
                                          'NORMAL')
    check("//-relative + unsaved blend -> actionable error",
          p is None and err is not None and "unsaved" in err)
    # NOTE: build a REAL absolute root — on Windows Python 3.13+
    # ntpath.isabs() is False for drive-less rooted paths like "/out".
    root = os.path.abspath(sep)
    abs_in = os.path.join(root, "out", "n.png")
    p, err = flowcore.resolve_output_path(abs_in, "", "Low", 'NORMAL')
    check("absolute path passes through even when unsaved",
          err is None and p == os.path.normpath(abs_in),
          "got %r / %r" % (p, err))
    p, err = flowcore.resolve_output_path(
        os.path.join(root, "out") + sep, "", "Low", 'NORMAL')
    check("trailing slash means directory: default file name appended",
          err is None and p == os.path.normpath(os.path.join(
              root, "out", "Low_normal.png")), "got %r" % p)
    p, err = flowcore.resolve_output_path("rel/n.png", "", "Low", 'NORMAL')
    check("bare relative + unsaved blend -> actionable error",
          p is None and err is not None)
    p, err = flowcore.resolve_output_path("rel/n.png", sep + "proj",
                                          "Low", 'NORMAL')
    check("bare relative resolves against the blend dir",
          err is None and p == os.path.normpath(
              os.path.join(sep + "proj", "rel", "n.png")))

    # --- UV degeneracy math ---------------------------------------------------------
    # Unit quad as two triangles: loops 0..3, tris (0,1,2) (0,2,3).
    quad_uvs = np.array([(0, 0), (1, 0), (1, 1), (0, 1)], dtype=np.float64)
    tris = np.array([(0, 1, 2), (0, 2, 3)], dtype=np.int64)
    check("unit quad UV area is 1.0",
          abs(flowcore.uv_total_area(quad_uvs, tris) - 1.0) < 1e-12)
    check("unit quad extent is (1, 1)",
          flowcore.uv_extent(quad_uvs) == (1.0, 1.0))
    check("unit quad is not degenerate",
          not flowcore.uv_layout_degenerate(quad_uvs, tris))
    zeros = np.zeros((4, 2))
    check("all-zero layout is degenerate",
          flowcore.uv_layout_degenerate(zeros, tris))
    line = np.array([(0, 0), (0.5, 0), (1, 0), (0.25, 0)])
    check("collapsed-to-a-line layout is degenerate (zero area + zero "
          "extent on one axis)",
          flowcore.uv_layout_degenerate(line, tris))
    check("zero-area but spread layout is degenerate (area test)",
          flowcore.uv_layout_degenerate(
              np.array([(0, 0), (1, 1), (2, 2), (0.5, 0.5)]), tris))
    check("empty tris -> zero area",
          flowcore.uv_total_area(quad_uvs, np.empty((0, 3), np.int64))
          == 0.0)
    check("winding does not matter (unsigned area)",
          abs(flowcore.uv_total_area(quad_uvs,
                                     np.array([(2, 1, 0), (3, 2, 0)]))
              - 1.0) < 1e-12)

    # --- scale checks -------------------------------------------------------------------
    check("unit scale is applied", flowcore.scale_applied((1, 1, 1)))
    check("epsilon tolerance honored",
          flowcore.scale_applied((1 + 1e-5, 1, 1))
          and not flowcore.scale_applied((1.01, 1, 1)))
    check("non-uniform scale is not applied",
          not flowcore.scale_applied((2, 1, 1)))
    check("negative scale detected",
          flowcore.scale_negative((1, 1, -1))
          and not flowcore.scale_negative((1, 1, 1)))
    check("negative unit scale is 'not applied' too",
          not flowcore.scale_applied((-1, 1, 1)))

    # --- decimate ratio -------------------------------------------------------------------
    check("decimate ratio = target/current",
          abs(flowcore.decimate_ratio(1000, 250) - 0.25) < 1e-12)
    check("ratio clamps to 1.0 when target >= current",
          flowcore.decimate_ratio(100, 500) == 1.0)
    check("ratio never reaches 0",
          flowcore.decimate_ratio(10**9, 1) > 0.0)
    check("zero current faces -> 1.0 (no-op, caller validates)",
          flowcore.decimate_ratio(0, 100) == 1.0)


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("CORE_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("CORE_TESTS_PASSED")
sys.stdout.flush()
