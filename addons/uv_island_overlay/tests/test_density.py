# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for density.py and the DENSITY-mode build paths
(run inside `blender --background --python`).

Covers the pure texel-density math (sqrt(UV area / 3D area) convention,
degenerate-face exclusions, median, deviation tints) on hand-constructed
inputs, then drives overlay._build_density on real meshes with known
density ratios — including the Object-Mode numpy fast path vs the
Edit-Mode bmesh fallback equivalence.

Prints DENSITY_TESTS_PASSED on success (sentinel-grepped by run_tests.sh
because Blender exits 0 even on unhandled --python exceptions).
"""

import os
import sys
import traceback

import bpy
import numpy as np

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from uv_island_overlay import density  # noqa: E402
from uv_island_overlay import overlay  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def make_mesh_object(name, verts, faces, uvs_per_loop=None):
    """Mesh object from raw data with hand-assigned per-loop UVs (loops
    follow face vertex order in from_pydata meshes)."""
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    if uvs_per_loop is not None:
        layer = me.uv_layers.new(name="UVMap")
        layer.data.foreach_set("uv", [c for uv in uvs_per_loop for c in uv])
    obj = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

def test_triangle_areas():
    print("test_triangle_areas")
    tri3 = [
        [(0, 0, 0), (2, 0, 0), (0, 2, 0)],     # area 2
        [(0, 0, 0), (1, 0, 0), (2, 0, 0)],     # colinear: area 0
        [(1, 1, 1), (1, 1, 1), (1, 1, 1)],     # degenerate: area 0
    ]
    a3 = density.triangle_areas_3d(tri3)
    check("3D areas exact", list(a3) == [2.0, 0.0, 0.0], "got %r" % a3)

    triuv = [
        [(0, 0), (1, 0), (0, 1)],              # area 0.5
        [(0, 0), (0, 1), (1, 0)],              # flipped winding: still 0.5
        [(0.5, 0.5), (0.5, 0.5), (0.5, 0.5)],  # degenerate: 0
    ]
    auv = density.triangle_areas_uv(triuv)
    check("UV areas exact, winding-insensitive",
          list(auv) == [0.5, 0.5, 0.0], "got %r" % auv)


def test_island_densities_pure():
    print("test_island_densities_pure")
    # One island: UV area 0.0625 over 3D area 1 -> density 0.25 exactly.
    d = density.island_densities([0, 0], [0.03125, 0.03125], [0.5, 0.5], 1)
    check("density = sqrt(UV area / 3D area), exact",
          d[0] == 0.25, "got %r" % d)

    # 2x-scaled UVs (4x UV area) -> exactly 2x density.
    d = density.island_densities([0, 1], [0.0625, 0.25], [1.0, 1.0], 2)
    check("2x-scaled-UV island reports exactly 2x density",
          d[1] == 2.0 * d[0] and d[0] == 0.25, "got %r" % d)

    # Degenerate triangles excluded from BOTH sums: a zero-3D-area tri
    # with UV area must not inflate the island's UV sum.
    d = density.island_densities([0, 0], [0.0625, 0.1], [1.0, 0.0], 1)
    check("zero-3D-area triangle excluded from both sums",
          d[0] == 0.25, "got %r" % d)
    d = density.island_densities([0, 0], [0.0625, 0.0], [1.0, 0.5], 1)
    check("zero-UV-area triangle excluded from both sums",
          d[0] == 0.25, "got %r" % d)

    # Islands with no valid triangle: NaN (undefined).
    d = density.island_densities([0, 1], [0.5, 0.0], [1.0, 1.0], 2)
    check("all-degenerate island is NaN",
          not np.isnan(d[0]) and np.isnan(d[1]), "got %r" % d)

    # Empty inputs.
    d = density.island_densities([], [], [], 0)
    check("empty input -> empty result", d.shape == (0,))
    d = density.island_densities([], [], [], 3)
    check("islands with no triangles at all -> all NaN",
          d.shape == (3,) and np.all(np.isnan(d)))


def test_median_and_units():
    print("test_median_and_units")
    check("median over defined densities",
          density.median_density([0.25, 0.5, np.nan]) == 0.375)
    check("median excludes NaN",
          density.median_density([np.nan, 1.0, np.nan]) == 1.0)
    check("median of nothing is None",
          density.median_density([np.nan, np.nan]) is None)
    check("median of empty is None", density.median_density([]) is None)
    check("px/unit convention: density * texture edge (default 1024)",
          density.density_px_per_unit(0.5) == 512.0
          and density.density_px_per_unit(0.5, 2048) == 1024.0)


def test_deviation():
    print("test_deviation")
    dev = density.deviation_octaves([0.5, 1.0, 2.0], 1.0)
    check("deviation sign: -1 octave below, 0 at median, +1 above",
          list(dev) == [-1.0, 0.0, 1.0], "got %r" % dev)
    dev = density.deviation_octaves([4.0, 8.0, 16.0, 1.0 / 16.0], 1.0)
    check("deviation clamps at +/-2 octaves",
          list(dev) == [2.0, 2.0, 2.0, -2.0], "got %r" % dev)
    dev = density.deviation_octaves([np.nan, 1.0], 1.0)
    check("NaN density -> NaN deviation",
          np.isnan(dev[0]) and dev[1] == 0.0)
    dev = density.deviation_octaves([1.0], None)
    check("no median -> NaN deviation", np.isnan(dev[0]))

    tints = density.deviation_tints([1.0], 1.0, alpha=0.4)
    check("tint at the median is neutral, alpha plumbed",
          tuple(tints[0]) == (1.0, 1.0, 1.0, 0.4), "got %r" % tints)
    tints = density.deviation_tints([0.5, 2.0], 1.0)
    check("tint direction: blue below the median, red above",
          tints[0][2] > tints[0][0] and tints[1][0] > tints[1][2],
          "got %r" % tints)
    t_clamped = density.deviation_tints([4.0, 8.0], 1.0)
    check("tint saturates at the clamp (4x == 8x median)",
          np.array_equal(t_clamped[0], t_clamped[1]), "got %r" % t_clamped)
    check("fully-saturated tints hit the endpoints",
          np.allclose(t_clamped[0][:3], density.TINT_ABOVE, atol=1e-12)
          and np.allclose(density.deviation_tints([0.25], 1.0)[0][:3],
                          density.TINT_BELOW, atol=1e-12))
    tints = density.deviation_tints([np.nan, 1.0], 1.0)
    check("NaN density -> neutral tint",
          tuple(tints[0][:3]) == density.TINT_NEUTRAL)
    tints = density.deviation_tints([0.5, 2.0], None)
    check("no median -> all neutral",
          np.all(tints[:, :3] == 1.0))


# ---------------------------------------------------------------------------
# overlay._build_density on real meshes
# ---------------------------------------------------------------------------

def _two_quads_2x():
    """One mesh, two disjoint unit quads; quad B's UVs are quad A's
    scaled by exactly 2 (A: 0.25x0.25 chart -> density 0.25; B: 0.5x0.5
    chart -> density 0.5)."""
    return make_mesh_object(
        "DensityTwoQuads",
        verts=[(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
               (2, 0, 0), (3, 0, 0), (3, 1, 0), (2, 1, 0)],
        faces=[(0, 1, 2, 3), (4, 5, 6, 7)],
        uvs_per_loop=[(0.0, 0.0), (0.25, 0.0), (0.25, 0.25), (0.0, 0.25),
                      (0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)])


def test_build_density_object_mode():
    print("test_build_density_object_mode (numpy fast path)")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = _two_quads_2x()

    coords, colors, uvs, count, dens, median, no_uvs = \
        overlay._build_density(obj)
    check("two UV islands detected", count == 2, "got %d" % count)
    check("not flagged as no-UVs", no_uvs is False)
    check("soup: 4 tris = 12 vertices, uv per vertex, color per vertex",
          len(coords) == 12 and len(uvs) == 12 and len(colors) == 12)
    check("densities exact: 0.25 and 0.5",
          list(dens) == [0.25, 0.5], "got %r" % dens)
    check("2x-scaled-UV island reports exactly 2x density",
          dens[1] == 2.0 * dens[0])
    check("median exact: 0.375", median == 0.375, "got %r" % median)

    # Deviation tint direction, through the real pipeline: island A is
    # below the median (blue-ward), island B above (red-ward). Island
    # membership recovered via the UV soup (A's chart is u <= 0.25).
    cols = np.asarray(colors)
    uva = np.asarray(uvs)
    a_rows = cols[uva[:, 0] <= 0.26]
    b_rows = cols[uva[:, 0] >= 0.49]
    check("tint direction on mesh: sparser island blue-ward, denser "
          "red-ward",
          len(a_rows) == 6 and len(b_rows) == 6
          and a_rows[0][2] > a_rows[0][0]
          and b_rows[0][0] > b_rows[0][2],
          "a %r b %r" % (a_rows[:1], b_rows[:1]))
    # Since v1.3.1 the fragment stage ignores the baked vertex alpha
    # (opacity is a push constant), but the bake itself must stay
    # stable: the soup layout and the pure color helpers are unchanged.
    check("tint alpha still baked as the overlay ALPHA (vestigial but "
          "layout-stable since v1.3.1)",
          np.all(cols[:, 3] == np.float32(overlay.ALPHA)))


def test_build_density_edit_mode_equivalence():
    print("test_build_density_edit_mode_equivalence (bmesh fallback)")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = _two_quads_2x()

    ref = overlay._build_density(obj)           # Object Mode: numpy path
    bpy.ops.object.mode_set(mode='EDIT')
    alt = overlay._build_density(obj)           # Edit Mode: bmesh path
    bpy.ops.object.mode_set(mode='OBJECT')

    check("island count agrees across paths", ref[3] == alt[3] == 2)
    check("densities agree exactly across paths",
          np.array_equal(ref[4], alt[4]),
          "numpy %r bmesh %r" % (ref[4], alt[4]))
    check("median agrees across paths", ref[5] == alt[5])
    check("soup sizes agree across paths",
          len(ref[0]) == len(alt[0]) and len(ref[2]) == len(alt[2]))
    # Same multiset of (position, uv) corners regardless of triangle
    # order differences between Mesh.loop_triangles and bmesh.
    def corner_set(res):
        return sorted(map(tuple, np.hstack([np.asarray(res[0]),
                                            np.asarray(res[2])]).tolist()))
    check("soup corners (pos+uv) identical across paths",
          corner_set(ref) == corner_set(alt))


def test_degenerate_faces_on_mesh():
    print("test_degenerate_faces_on_mesh")
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Island A: unit quad, 0.25x0.25 chart (density 0.25) PLUS a
    # zero-3D-area triangle in the SAME island (shares edge v1-v2 with
    # matching UVs; its third vertex sits on that edge -> colinear).
    # Its nonzero UV area must NOT leak into the island's density.
    # Island B: valid 3D triangle whose UVs are all one point (zero UV
    # area -> undefined density, excluded from stats, tinted neutral).
    obj = make_mesh_object(
        "DensityDegenerate",
        verts=[(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
               (1, 0.5, 0),                     # on edge v1-v2
               (5, 0, 0), (6, 0, 0), (5, 1, 0)],
        faces=[(0, 1, 2, 3), (1, 4, 2), (5, 6, 7)],
        uvs_per_loop=[
            (0.0, 0.0), (0.25, 0.0), (0.25, 0.25), (0.0, 0.25),
            (0.25, 0.0), (0.4, 0.1), (0.25, 0.25),   # matches shared edge
            (0.9, 0.9), (0.9, 0.9), (0.9, 0.9),      # zero UV area
        ])

    coords, colors, uvs, count, dens, median, no_uvs = \
        overlay._build_density(obj)
    check("degenerate mesh: 2 islands", count == 2, "got %d" % count)
    check("zero-3D-area triangle excluded: island density still exactly "
          "0.25", dens[0] == 0.25, "got %r" % dens)
    check("zero-UV-area island has undefined density (NaN)",
          np.isnan(dens[1]), "got %r" % dens)
    check("median over defined islands only", median == 0.25)
    cols = np.asarray(colors)
    uva = np.asarray(uvs)
    nan_rows = cols[uva[:, 0] >= 0.89]
    check("undefined-density island rendered with the neutral tint "
          "(checker as-is)",
          len(nan_rows) == 3 and np.all(nan_rows[:, :3] == np.float32(1.0)),
          "got %r" % nan_rows)
    # The single defined island IS the median -> neutral there too.
    check("median island tinted neutral",
          np.all(cols[:, :3] == np.float32(1.0)))


def test_no_uv_layer():
    print("test_no_uv_layer")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = make_mesh_object(
        "DensityNoUV",
        verts=[(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        faces=[(0, 1, 2, 3)],
        uvs_per_loop=None)
    check("mesh really has no UV layer", len(obj.data.uv_layers) == 0)
    coords, colors, uvs, count, dens, median, no_uvs = \
        overlay._build_density(obj)
    check("no-UV mesh: flagged, empty geometry, no stats",
          no_uvs is True and coords is None and uvs is None
          and count == 0 and dens is None and median is None)
    # Edit Mode path agrees.
    bpy.ops.object.mode_set(mode='EDIT')
    res = overlay._build_density(obj)
    bpy.ops.object.mode_set(mode='OBJECT')
    check("no-UV mesh: edit-mode path agrees",
          res[6] is True and res[0] is None)


def test_hidden_faces_excluded():
    print("test_hidden_faces_excluded")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = _two_quads_2x()
    obj.data.polygons[1].hide = True
    coords, colors, uvs, count, dens, median, no_uvs = \
        overlay._build_density(obj)
    check("hidden face dropped from the density soup (1 quad = 6 verts)",
          len(coords) == 6, "got %d" % len(coords))
    check("hidden island keeps its id but has no triangles -> NaN",
          count == 2 and dens[0] == 0.25 and np.isnan(dens[1]),
          "got %r" % dens)


# ---------------------------------------------------------------------------

try:
    test_triangle_areas()
    test_island_densities_pure()
    test_median_and_units()
    test_deviation()
    test_build_density_object_mode()
    test_build_density_edit_mode_equivalence()
    test_degenerate_faces_on_mesh()
    test_no_uv_layer()
    test_hidden_faces_excluded()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("DENSITY_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("DENSITY_TESTS_PASSED")
sys.stdout.flush()
