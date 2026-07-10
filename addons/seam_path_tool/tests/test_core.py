# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for core.py (run inside `blender --background --python`).

Prints CORE_TESTS_PASSED on success. Blender exits 0 even on unhandled
exceptions in --python scripts, so the wrapper greps for the sentinel.
"""

import math
import os
import sys
import traceback

import bmesh
from mathutils import Matrix, Vector

# Make the add-on package importable from its source location.
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from seam_path_tool import core  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def vert_at(bm, co, tol=1e-5):
    """Find the vertex closest to a coordinate (must be within tol)."""
    best, best_d = None, tol
    for v in bm.verts:
        d = (v.co - Vector(co)).length
        if d < best_d:
            best, best_d = v, d
    assert best is not None, "no vertex near %r" % (co,)
    return best


def path_verts(v_from, edges):
    verts = [v_from]
    v = v_from
    for e in edges:
        v = e.other_vert(v)
        verts.append(v)
    return verts


# ---------------------------------------------------------------------------

def test_grid():
    print("test_grid")
    seg = 4
    bm = bmesh.new()
    # create_grid: x_segments/y_segments are edge counts per side;
    # grid spans [-size, +size] in x and y.
    bmesh.ops.create_grid(bm, x_segments=seg, y_segments=seg, size=1.0)

    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    ys = sorted({round(v.co.y, 6) for v in bm.verts})
    corner_a = vert_at(bm, (xs[0], ys[0], 0.0))
    corner_b = vert_at(bm, (xs[-1], ys[-1], 0.0))

    edges = core.shortest_path(bm, corner_a, corner_b, mode='LENGTH')
    check("grid path found", edges is not None and len(edges) > 0)
    # On an axis-aligned grid the shortest path is any monotone staircase:
    # edge count = seg + seg, total length = Manhattan distance.
    check("grid edge count == 2*seg", len(edges) == 2 * seg,
          "got %d" % len(edges))
    manhattan = abs(corner_b.co.x - corner_a.co.x) + \
        abs(corner_b.co.y - corner_a.co.y)
    total = core.path_length(edges)
    check("grid path length == Manhattan distance",
          math.isclose(total, manhattan, rel_tol=1e-6),
          "got %f expected %f" % (total, manhattan))
    # Path is connected and starts/ends at the right verts.
    chain = path_verts(corner_a, edges)
    check("grid path endpoints", chain[0] is corner_a and chain[-1] is corner_b)

    # Seam marking: path edges True, all others untouched (False).
    marked = core.mark_seam_path(bm, corner_a, corner_b, mode='LENGTH')
    marked_set = set(marked)
    check("grid all path edges seamed", all(e.seam for e in marked))
    check("grid non-path edges untouched",
          all(not e.seam for e in bm.edges if e not in marked_set))

    # clear=True unmarks exactly that path again.
    cleared = core.mark_seam_path(bm, corner_a, corner_b, mode='LENGTH',
                                  clear=True)
    check("grid clear returns same-length path", len(cleared) == len(marked))
    check("grid clear removed seams", all(not e.seam for e in bm.edges))

    # Degenerate: same vertex.
    check("grid same-vertex returns []",
          core.shortest_path(bm, corner_a, corner_a) == [])
    bm.free()


def test_cube():
    print("test_cube")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)  # verts at (+-1,+-1,+-1), edge len 2

    # Two corners of adjacent faces, diagonal across their shared face:
    # shortest path = 2 edges along that face, total length 4.
    va = vert_at(bm, (-1, -1, -1))
    vb = vert_at(bm, (1, 1, -1))
    edges = core.shortest_path(bm, va, vb, mode='LENGTH')
    check("cube diagonal path 2 edges", len(edges) == 2,
          "got %d" % len(edges))
    check("cube diagonal path length 4",
          math.isclose(core.path_length(edges), 4.0, rel_tol=1e-6))

    # Directly connected corners: exactly that 1 edge.
    vc = vert_at(bm, (-1, -1, 1))
    edges1 = core.shortest_path(bm, va, vc, mode='LENGTH')
    check("cube adjacent verts 1 edge", len(edges1) == 1)
    check("cube adjacent edge is the connecting edge",
          set(edges1[0].verts) == {va, vc})

    # Opposite corners of the cube: 3 edges, length 6.
    vd = vert_at(bm, (1, 1, 1))
    edges3 = core.shortest_path(bm, va, vd, mode='LENGTH')
    check("cube opposite corners 3 edges", len(edges3) == 3)
    check("cube opposite corners length 6",
          math.isclose(core.path_length(edges3), 6.0, rel_tol=1e-6))
    bm.free()


def test_sphere_meridian():
    print("test_sphere_meridian")
    bm = bmesh.new()
    useg, vseg = 8, 8
    bmesh.ops.create_uvsphere(bm, u_segments=useg, v_segments=vseg,
                              radius=1.0)
    north = max(bm.verts, key=lambda v: v.co.z)
    south = min(bm.verts, key=lambda v: v.co.z)
    check("sphere poles found",
          math.isclose(north.co.z, 1.0, rel_tol=1e-5)
          and math.isclose(south.co.z, -1.0, rel_tol=1e-5))

    edges = core.shortest_path(bm, north, south, mode='LENGTH')
    check("sphere pole-to-pole edge count == v_segments",
          len(edges) == vseg, "got %d" % len(edges))

    # All intermediate verts must lie on a single meridian: identical
    # azimuth angle (atan2 of y, x), poles excluded (azimuth undefined).
    chain = path_verts(north, edges)
    azimuths = [math.atan2(v.co.y, v.co.x)
                for v in chain[1:-1]]
    same_azimuth = all(
        math.isclose(a, azimuths[0], abs_tol=1e-5) for a in azimuths)
    check("sphere path follows one meridian", same_azimuth,
          "azimuths %r" % azimuths)
    # z strictly decreasing along the way down.
    zs = [v.co.z for v in chain]
    check("sphere path z monotonically decreasing",
          all(z1 > z2 for z1, z2 in zip(zs, zs[1:])))
    bm.free()


def test_disconnected():
    print("test_disconnected")
    bm = bmesh.new()
    ret1 = bmesh.ops.create_cube(bm, size=1.0)
    v_in_first = ret1["verts"][0]
    ret2 = bmesh.ops.create_cube(bm, size=1.0)
    # Move the second cube far away so it's unambiguous.
    bmesh.ops.translate(bm, verts=ret2["verts"], vec=(10.0, 0.0, 0.0))
    v_in_second = ret2["verts"][0]

    edges = core.shortest_path(bm, v_in_first, v_in_second, mode='LENGTH')
    check("disconnected returns None", edges is None)
    marked = core.mark_seam_path(bm, v_in_first, v_in_second)
    check("disconnected mark returns None, no crash", marked is None)
    check("disconnected mark set no seams",
          all(not e.seam for e in bm.edges))
    bm.free()


def test_topology_vs_length():
    print("test_topology_vs_length")
    # (a) Stretched (non-uniformly scaled) grid: both modes must still
    # return optimal paths under their own metric (Manhattan distance /
    # Manhattan edge count). Note that on a pure quad grid every monotone
    # staircase is simultaneously optimal in BOTH metrics, so the two modes
    # cannot be *forced* to differ here — that needs routes with different
    # edge granularity, tested in (b).
    seg = 4
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=seg, y_segments=seg, size=1.0)
    bmesh.ops.transform(bm, matrix=Matrix.Diagonal((5.0, 1.0, 1.0, 1.0)),
                        verts=bm.verts[:])
    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    ys = sorted({round(v.co.y, 6) for v in bm.verts})
    a = vert_at(bm, (xs[0], ys[0], 0.0))
    b = vert_at(bm, (xs[-1], ys[-1], 0.0))
    manhattan = abs(b.co.x - a.co.x) + abs(b.co.y - a.co.y)
    e_len = core.shortest_path(bm, a, b, mode='LENGTH')
    e_top = core.shortest_path(bm, a, b, mode='TOPOLOGY')
    check("stretched grid LENGTH optimal",
          math.isclose(core.path_length(e_len), manhattan, rel_tol=1e-6))
    check("stretched grid TOPOLOGY optimal edge count",
          len(e_top) == 2 * seg)
    bm.free()

    # (b) Two routes with different granularity: a fine 5-edge route of
    # total length 5 vs a coarse 3-edge detour of total length 25.
    # TOPOLOGY must take the coarse route (3 edges), LENGTH the fine one.
    bm = bmesh.new()
    a = bm.verts.new((0.0, 0.0, 0.0))
    b = bm.verts.new((5.0, 0.0, 0.0))
    prev = a
    for i in range(1, 5):  # fine route: 5 unit edges along x
        v = bm.verts.new((float(i), 0.0, 0.0))
        bm.edges.new((prev, v))
        prev = v
    bm.edges.new((prev, b))
    t1 = bm.verts.new((0.0, 10.0, 0.0))   # coarse detour: 3 long edges
    t2 = bm.verts.new((5.0, 10.0, 0.0))
    bm.edges.new((a, t1))
    bm.edges.new((t1, t2))
    bm.edges.new((t2, b))

    e_len = core.shortest_path(bm, a, b, mode='LENGTH')
    e_top = core.shortest_path(bm, a, b, mode='TOPOLOGY')
    check("LENGTH takes fine route (5 edges, length 5)",
          len(e_len) == 5 and math.isclose(core.path_length(e_len), 5.0,
                                           rel_tol=1e-6))
    check("TOPOLOGY takes coarse route (3 edges, length 25)",
          len(e_top) == 3 and math.isclose(core.path_length(e_top), 25.0,
                                           rel_tol=1e-6))
    check("modes give different paths", set(e_len) != set(e_top))
    bm.free()


def test_dijkstra_tree_matches_shortest_path():
    print("test_dijkstra_tree_matches_shortest_path")
    # The preview machinery (single-source tree + tree walk) must produce
    # exactly the path that shortest_path/mark_seam_path commit.
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=6, v_segments=6, radius=1.0)
    bm.verts.ensure_lookup_table()
    src = bm.verts[0]
    tree = core.dijkstra_tree(bm, src, mode='LENGTH')
    dist, _ = tree
    all_match = True
    for v in bm.verts:
        p_tree = core.path_from_tree(tree, src, v)
        p_direct = core.shortest_path(bm, src, v, mode='LENGTH')
        if p_tree is None or p_direct is None:
            all_match = False
            break
        # Paths may tie-break differently in theory, but costs must match
        # and here both use identical traversal order, so compare lengths.
        if not math.isclose(core.path_length(p_tree),
                            core.path_length(p_direct), rel_tol=1e-9,
                            abs_tol=1e-12):
            all_match = False
            break
        if v is not src and not math.isclose(
                dist[v], core.path_length(p_tree), rel_tol=1e-9):
            all_match = False
            break
    check("tree walk matches direct shortest_path for every vertex",
          all_match)
    check("same-vertex tree walk returns []",
          core.path_from_tree(tree, src, src) == [])
    bm.free()


def test_clear_and_apply_seams():
    print("test_clear_and_apply_seams")
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=3, y_segments=3, size=1.0)
    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    ys = sorted({round(v.co.y, 6) for v in bm.verts})
    a = vert_at(bm, (xs[0], ys[0], 0.0))
    b = vert_at(bm, (xs[-1], ys[-1], 0.0))

    marked = core.mark_seam_path(bm, a, b)
    check("marked seams present", any(e.seam for e in bm.edges))
    # apply_seams returns prior state usable for exact restore.
    prior = core.apply_seams(marked, clear=True)
    check("apply_seams cleared", all(not e.seam for e in bm.edges))
    check("apply_seams prior state recorded",
          all(was is True for _, was in prior))
    for e, was in prior:
        e.seam = was
    check("prior state restore re-marks", all(e.seam for e in marked))
    bm.free()


def main():
    tests = [
        test_grid,
        test_cube,
        test_sphere_meridian,
        test_disconnected,
        test_topology_vs_length,
        test_dijkstra_tree_matches_shortest_path,
        test_clear_and_apply_seams,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            traceback.print_exc()
            FAILURES.append("%s raised" % t.__name__)

    sys.stdout.flush()
    if FAILURES:
        print("CORE_TESTS_FAILED: %d failure(s): %s"
              % (len(FAILURES), ", ".join(FAILURES)))
    else:
        print("CORE_TESTS_PASSED")
    sys.stdout.flush()


main()
