# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for the pure estimator (run inside
``blender --background --python``; the module has no bpy import, so this
also runs under bare python3 during development).

Covers the research doc's list: tiny and huge objects, zero extents,
empty mesh, unapplied uniform scale, non-uniform scale, negative scale,
shear, overflow saturation, world-target conversion, band thresholds.

Prints CALIPERS_ESTIMATE_TESTS_PASSED on success.
"""

import math
import os
import sys
import traceback

# Import estimate.py directly (not through the calipers package): the
# package __init__ imports bpy, this module deliberately does not — so
# the estimator tests stay runnable under bare python3 too.
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

import estimate as E

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def expect_error(name, fn):
    try:
        fn()
    except E.EstimateError:
        check(name, True)
    except Exception as exc:
        check(name, False, "wrong exception %r" % exc)
    else:
        check(name, False, "no EstimateError raised")


def main():
    # --- reference cube (default 2m cube at the datablock default 0.1) ----
    e = E.estimate((-1, -1, -1), (1, 1, 1), 0.1, surface_area=24.0)
    check("cube longest_axis_cells 20", e.longest_axis_cells == 20)
    check("cube axis_cells padded (21,21,21)", e.axis_cells == (21, 21, 21))
    check("cube bounding_cells 21^3", e.bounding_cells == 21 ** 3)
    check("cube surface_cells area/v^2", abs(e.surface_cells - 2400.0) < 1e-6)
    check("cube risk GREEN", e.risk == E.RISK_GREEN)
    check("cube not saturated", not e.saturated)
    check("cube no scale warnings", e.scale_warnings == ())
    check("coordinate space is OBJECT", e.coordinate_space == 'OBJECT')
    check("default confidence EXACT", e.confidence == E.CONF_EXACT)
    check("voxel size verbatim", e.voxel_size == 0.1)

    # frozen result
    try:
        e.risk = 'PURPLE'
        check("Estimate frozen", False, "assignment succeeded")
    except AttributeError:
        check("Estimate frozen", True)

    # --- confidence / source labels ---------------------------------------
    a = E.estimate((0, 0, 0), (1, 1, 1), 0.1, source="base mesh",
                   exact=False)
    check("approximate confidence", a.confidence == E.CONF_APPROXIMATE)
    check("source label carried", a.source == "base mesh")

    # --- tiny object -------------------------------------------------------
    tiny = E.estimate((-0.0005,) * 3, (0.0005,) * 3, 0.1,
                      surface_area=6e-6)
    check("tiny: 1 cell longest axis", tiny.longest_axis_cells == 1)
    check("tiny: risk GREEN", tiny.risk == E.RISK_GREEN)
    # coarse-side reading: a cell far bigger than the object shows up as
    # a tiny surface score (the UI flags 'too coarse' from cells <= 1).
    check("tiny: surface_cells << 1", tiny.surface_cells < 0.001)

    # --- huge object at a mismatched small voxel size ----------------------
    huge = E.estimate((-5000,) * 3, (5000,) * 3, 0.1)
    check("huge: longest axis 100000", huge.longest_axis_cells == 100000)
    check("huge: risk RED", huge.risk == E.RISK_RED)
    check("huge: not saturated yet", not huge.saturated)

    # --- zero extents (flat plane — probed VALID on 5.1.2, never an error) -
    plane = E.estimate((-1, -1, 0), (1, 1, 0), 0.1, surface_area=4.0)
    check("plane: zero z-extent accepted", plane.dimensions[2] == 0.0)
    check("plane: z axis_cells = padding only",
          plane.axis_cells[2] == E.DEFAULT_PADDING)
    check("plane: longest axis from x/y", plane.longest_axis_cells == 20)

    # --- empty mesh (caller passes degenerate zero bounds) -----------------
    empty = E.estimate((0, 0, 0), (0, 0, 0), 0.1)
    check("empty: zero cells", empty.longest_axis_cells == 0)
    check("empty: GREEN", empty.risk == E.RISK_GREEN)

    # --- swapped bounds are normalized, not rejected ------------------------
    sw = E.estimate((1, 1, 1), (-1, -1, -1), 0.1)
    check("swapped bounds normalized", sw.dimensions == (2.0, 2.0, 2.0))

    # --- unapplied uniform scale --------------------------------------------
    m2 = [[2, 0, 0, 0], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]]
    u = E.estimate((-1,) * 3, (1,) * 3, 0.1, matrix_world=m2)
    check("uniform scale: UNAPPLIED warned",
          E.WARN_UNAPPLIED_SCALE in u.scale_warnings)
    check("uniform scale: NOT non-uniform",
          E.WARN_NON_UNIFORM_SCALE not in u.scale_warnings)
    check("uniform scale: not sheared",
          E.WARN_SHEARED not in u.scale_warnings)
    check("uniform scale: world sizes 0.2 each",
          all(abs(s - 0.2) < 1e-9 for s in u.world_axis_sizes))
    check("uniform scale: cells UNCHANGED (object space — probed: "
          "scaled cube remeshes identically)",
          u.longest_axis_cells == 20)

    # --- non-uniform scale ---------------------------------------------------
    mn = [[3, 0, 0], [0, 1, 0], [0, 0, 0.5]]
    n = E.estimate((-1,) * 3, (1,) * 3, 0.1, matrix_world=mn)
    check("non-uniform: warned",
          E.WARN_NON_UNIFORM_SCALE in n.scale_warnings)
    check("non-uniform: world sizes (0.3, 0.1, 0.05)",
          all(abs(a - b) < 1e-9
              for a, b in zip(n.world_axis_sizes, (0.3, 0.1, 0.05))))
    check("non-uniform: effective scales descending (3, 1, 0.5)",
          all(abs(a - b) < 1e-9
              for a, b in zip(n.effective_axis_scales, (3.0, 1.0, 0.5))))

    # --- negative scale --------------------------------------------------------
    neg = [[-1, 0, 0], [0, 1, 0], [0, 0, 1]]
    g = E.estimate((-1,) * 3, (1,) * 3, 0.1, matrix_world=neg)
    check("negative scale: warned",
          E.WARN_NEGATIVE_SCALE in g.scale_warnings)
    check("negative scale: |scale|=1 so not UNAPPLIED",
          E.WARN_UNAPPLIED_SCALE not in g.scale_warnings)

    # --- pure rotation: no warnings --------------------------------------------
    c, s = math.cos(0.7), math.sin(0.7)
    rot = [[c, -s, 0], [s, c, 0], [0, 0, 1]]
    r = E.estimate((-1,) * 3, (1,) * 3, 0.1, matrix_world=rot)
    check("pure rotation: no warnings", r.scale_warnings == ())

    # --- shear: singular values, not column norms -------------------------------
    sh = [[1, 1, 0], [0, 1, 0], [0, 0, 1]]     # unit shear in xy
    warnings, sv, norms = E.analyze_scale(sh)
    check("shear: WARN_SHEARED", E.WARN_SHEARED in warnings)
    golden = math.sqrt((3.0 + math.sqrt(5.0)) / 2.0)   # exact for this M
    check("shear: largest singular value exact (golden ratio)",
          abs(sv[0] - golden) < 1e-9, "%r vs %r" % (sv[0], golden))
    check("shear: sv[0]*sv[2] == det == 1",
          abs(sv[0] * sv[2] - 1.0) < 1e-9)
    check("shear: column norms disagree with singular values",
          abs(sorted(norms, reverse=True)[0] - sv[0]) > 0.1)

    # --- overflow saturation ------------------------------------------------------
    o = E.estimate((0, 0, 0), (1e30, 1e30, 1e30), 1e-10)
    check("overflow: saturated flag", o.saturated)
    check("overflow: bounding capped",
          o.bounding_cells == E.SATURATION_CAP)
    check("overflow: axis counts capped",
          all(nn == E.SATURATION_CAP for nn in o.axis_cells))
    check("overflow: RED", o.risk == E.RISK_RED)
    # product-only saturation: each axis fits, the product does not
    p = E.estimate((0, 0, 0), (1e7, 1e7, 1e7), 1e-4)
    check("product saturation: axes unsaturated",
          all(nn < E.SATURATION_CAP for nn in p.axis_cells))
    check("product saturation: product capped",
          p.bounding_cells == E.SATURATION_CAP and p.saturated)

    # --- world-target conversion ----------------------------------------------------
    check("world target identity", E.world_target_to_object(0.1, None) == 0.1)
    check("world target uniform 2x -> /2",
          abs(E.world_target_to_object(0.1, m2) - 0.05) < 1e-12)
    check("world target non-uniform -> largest axis (conservative /3)",
          abs(E.world_target_to_object(0.3, mn) - 0.1) < 1e-12)
    check("world target shear uses singular value",
          abs(E.world_target_to_object(golden, sh) - 1.0) < 1e-9)
    expect_error("world target rejects 0",
                 lambda: E.world_target_to_object(0.0, m2))
    expect_error("world target rejects negative",
                 lambda: E.world_target_to_object(-0.1, m2))
    expect_error("world target rejects NaN",
                 lambda: E.world_target_to_object(float('nan'), m2))
    zero_m = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    expect_error("world target rejects degenerate matrix",
                 lambda: E.world_target_to_object(0.1, zero_m))
    # round-trip: converted object-space value, re-transformed, hits the
    # world target on the largest axis
    v_obj = E.world_target_to_object(0.12, mn)
    _, sv_mn, _ = E.analyze_scale(mn)
    check("world target round-trip on largest axis",
          abs(v_obj * sv_mn[0] - 0.12) < 1e-12)

    # --- band thresholds ------------------------------------------------------------
    check("band: below yellow GREEN",
          E.risk_band(10 ** 6) == E.RISK_GREEN)
    check("band: at yellow YELLOW",
          E.risk_band(10 ** 7) == E.RISK_YELLOW)
    check("band: at red RED", E.risk_band(10 ** 9) == E.RISK_RED)
    check("band: custom exponents",
          E.risk_band(1000, yellow_exp=2.0, red_exp=3.0) == E.RISK_RED)
    check("band: inverted config normalizes (red wins)",
          E.risk_band(10 ** 5, yellow_exp=9.0, red_exp=4.0) == E.RISK_RED)
    check("band: zero cells GREEN", E.risk_band(0) == E.RISK_GREEN)
    # threshold config flows through estimate()
    tb = E.estimate((0, 0, 0), (10, 10, 10), 0.1,
                    yellow_exp=2.0, red_exp=4.0)
    check("band config via estimate()", tb.risk == E.RISK_RED)

    # --- invalid input rejection (the ONLY rejections, per the doc) -----------------
    for bad in (0.0, -0.1, float('nan'), float('inf')):
        expect_error("reject voxel size %r" % bad,
                     lambda bad=bad: E.estimate((0,) * 3, (1,) * 3, bad))
    expect_error("reject non-finite bounds",
                 lambda: E.estimate((0, 0, float('nan')), (1, 1, 1), 0.1))
    expect_error("reject inf bounds",
                 lambda: E.estimate((0, 0, 0), (1, 1, float('inf')), 0.1))
    expect_error("reject negative surface area",
                 lambda: E.estimate((0,) * 3, (1,) * 3, 0.1,
                                    surface_area=-1.0))
    expect_error("reject non-finite matrix",
                 lambda: E.estimate((0,) * 3, (1,) * 3, 0.1,
                                    matrix_world=[[float('nan'), 0, 0],
                                                  [0, 1, 0], [0, 0, 1]]))
    expect_error("reject malformed matrix",
                 lambda: E.estimate((0,) * 3, (1,) * 3, 0.1,
                                    matrix_world=[[1, 0], [0, 1]]))

    # --- padding configurability ------------------------------------------------------
    pad0 = E.estimate((-1,) * 3, (1,) * 3, 0.1, padding=0)
    check("padding 0", pad0.axis_cells == (20, 20, 20))
    pad3 = E.estimate((-1,) * 3, (1,) * 3, 0.1, padding=3)
    check("padding 3", pad3.axis_cells == (23, 23, 23))
    check("padding never changes longest_axis_cells (raw per the doc)",
          pad0.longest_axis_cells == pad3.longest_axis_cells == 20)

    print()
    if FAILURES:
        print("FAILED: %d checks: %s" % (len(FAILURES), FAILURES))
    else:
        print("CALIPERS_ESTIMATE_TESTS_PASSED")


try:
    main()
except Exception:
    traceback.print_exc()
    print("CALIPERS_ESTIMATE_TESTS_CRASHED")
