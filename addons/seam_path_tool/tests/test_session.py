# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for session.py + picking.py (run inside
`blender --background --python`).

These are the regression tests for the two 1.2.0 bug fixes:

- Bug 2 (erase didn't erase): the modal's erase recomputed a shortest
  path from the opposite end; on tie-rich meshes (any quad grid) that is
  a *different* equal-length path than the marked one, so the seam
  survived. `test_erase_regression_bug2` commits a mark segment through
  SeamSession, erases back over it the way the modal now does
  (seam-preferring tie-break), and asserts the seams are actually gone
  from the mesh, plus the overlay/anchor model and exact undo semantics
  at every step.
- Bug 1 (picking through the mesh): the pure occlusion logic is tested
  against a constructed BVHTree — front geometry wins, occluded
  candidates are rejected, and a vertex's own face does not self-occlude
  (epsilon tolerance).

Prints SESSION_TESTS_PASSED on success.
"""

import math
import os
import sys
import traceback

import bmesh
from mathutils import Vector
from mathutils.bvhtree import BVHTree

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from seam_path_tool import core, picking, session  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def vert_at(bm, co, tol=1e-5):
    best, best_d = None, tol
    for v in bm.verts:
        d = (v.co - Vector(co)).length
        if d < best_d:
            best, best_d = v, d
    assert best is not None, "no vertex near %r" % (co,)
    return best


def make_grid(seg=4):
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=seg, y_segments=seg, size=1.0)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    ys = sorted({round(v.co.y, 6) for v in bm.verts})
    a = vert_at(bm, (xs[0], ys[0], 0.0))
    b = vert_at(bm, (xs[-1], ys[-1], 0.0))
    return bm, a, b


def tree_path(bm, v_from, v_to, prefer_seams=False):
    tree = core.dijkstra_tree(bm, v_from, mode='LENGTH',
                              prefer_seams=prefer_seams)
    return core.path_from_tree(tree, v_from, v_to)


# ---------------------------------------------------------------------------
# Bug 2 regression: session commit / erase / undo
# ---------------------------------------------------------------------------

def test_erase_regression_bug2():
    print("test_erase_regression_bug2")
    bm, a, b = make_grid()
    sess = session.SeamSession()

    # First click: anchor only.
    sess.add_anchor(a.index)
    check("first anchor is current", sess.current_anchor == a.index)

    # Commit a MARK segment a->b exactly like the modal (tree rooted at a).
    edges_ab = tree_path(bm, a, b)
    marked = {e.index for e in edges_ab}
    sess.commit_segment(a, edges_ab, clearing=False)
    check("mark commit sets seams on the mesh",
          {e.index for e in bm.edges if e.seam} == marked)
    check("mark commit advances anchor", sess.current_anchor == b.index)
    polys = sess.overlay_polylines()
    check("mark overlay is one non-erase polyline",
          len(polys) == 1 and polys[0][1] is False
          and polys[0][0][0] == a.index and polys[0][0][-1] == b.index)

    # The Bug 2 hazard must exist on this mesh: WITHOUT seam-preferring
    # tie-breaking, the reverse path (tree rooted at b, as the modal has
    # after the mark) is a DIFFERENT equal-length path — the erase used to
    # clear the wrong edges. (If Dijkstra's tie-breaking ever changes so
    # these coincide, this check flags that the regression scenario needs
    # a new mesh.)
    plain_ba = {e.index for e in tree_path(bm, b, a, prefer_seams=False)}
    check("bug 2 hazard present: plain reverse path differs from marked",
          plain_ba != marked,
          "paths coincide; regression scenario is vacuous")

    # THE FIX: erase path is computed with prefer_seams, so retracing the
    # marked segment from the new anchor follows the marked edges exactly.
    edges_ba = tree_path(bm, b, a, prefer_seams=True)
    check("seam-preferring reverse path retraces the marked edges",
          {e.index for e in edges_ba} == marked)

    # Commit the ERASE segment b->a: seams must actually be gone (Bug 2).
    sess.commit_segment(b, edges_ba, clearing=True)
    check("BUG2: erase commit removes the seams from the mesh",
          not any(e.seam for e in bm.edges),
          "left: %r" % sorted(e.index for e in bm.edges if e.seam))
    check("erase commit advances anchor back to a",
          sess.current_anchor == a.index)

    # Overlay semantics: the erased mark segment's overlay is gone, and no
    # grey line replaces it (there was no pre-existing seam here) — no
    # stacked grey-over-red, no lingering polylines at all.
    check("overlay after erase shows nothing (mark removed, no grey)",
          sess.overlay_polylines() == [],
          "got %r" % sess.overlay_polylines())
    check("anchor dots pruned to just the current anchor",
          sess.overlay_anchor_indices() == [a.index],
          "got %r" % sess.overlay_anchor_indices())

    # Undo the erase: seams and the mark overlay come back exactly.
    check("undo returns True", sess.undo_last(bm))
    check("undo of erase restores the marked seams exactly",
          {e.index for e in bm.edges if e.seam} == marked)
    polys = sess.overlay_polylines()
    check("undo of erase restores the mark overlay",
          len(polys) == 1 and polys[0][1] is False
          and polys[0][0][0] == a.index and polys[0][0][-1] == b.index)
    check("undo of erase retreats anchor", sess.current_anchor == b.index)

    # Undo the mark: mesh back to no seams, anchor retreats to a.
    check("undo of mark returns True", sess.undo_last(bm))
    check("undo of mark clears all seams", not any(e.seam for e in bm.edges))
    check("anchor history back to first click", sess.anchors == [a.index])
    check("overlay empty again", sess.overlay_polylines() == [])

    # Undo before any segment: removes the initial anchor; then nothing.
    check("undo removes initial anchor", sess.undo_last(bm)
          and sess.anchors == [] and sess.current_anchor is None)
    check("undo with nothing left returns False", not sess.undo_last(bm))
    bm.free()


def test_undo_preserves_preexisting_seams():
    print("test_undo_preserves_preexisting_seams")
    bm, a, b = make_grid()
    edges_ab = tree_path(bm, a, b)
    # One edge of the path is a seam BEFORE the session touches it.
    pre = edges_ab[2]
    pre.seam = True

    sess = session.SeamSession()
    sess.add_anchor(a.index)
    sess.commit_segment(a, edges_ab, clearing=False)
    check("all path edges seamed after mark",
          all(e.seam for e in edges_ab))
    sess.undo_last(bm)
    check("undo restores prior state exactly (pre-existing seam kept)",
          pre.seam and all(not e.seam for e in edges_ab if e is not pre))
    bm.free()


def test_erase_preexisting_seam_shows_grey():
    print("test_erase_preexisting_seam_shows_grey")
    bm, a, b = make_grid()
    # A seam that predates the session, along the exact path the erase
    # will take.
    edges_ab = tree_path(bm, a, b, prefer_seams=False)
    for e in edges_ab:
        e.seam = True

    sess = session.SeamSession()
    sess.add_anchor(a.index)
    # Erase follows the seam (prefer_seams) — same edges here by
    # construction.
    edges = tree_path(bm, a, b, prefer_seams=True)
    check("erase path follows the pre-existing seam",
          {e.index for e in edges} == {e.index for e in edges_ab})
    sess.commit_segment(a, edges, clearing=True)
    check("pre-existing seams erased", not any(e.seam for e in bm.edges))
    polys = sess.overlay_polylines()
    check("grey overlay shown where a pre-existing seam was erased",
          len(polys) == 1 and polys[0][1] is True
          and polys[0][0][0] == a.index and polys[0][0][-1] == b.index)
    # Undo brings the pre-existing seam back.
    sess.undo_last(bm)
    check("undo restores the pre-existing seam",
          all(e.seam for e in edges_ab))
    bm.free()


def test_partial_erase_splits_overlay():
    print("test_partial_erase_splits_overlay")
    # Mark a straight (unique-shortest) row a->c, then erase only its
    # second half c->m: the mark overlay must shrink to the first half —
    # no grey stacked over it — and undoing the erase must restore the
    # full mark overlay.
    bm, a, _b = make_grid()
    ys = sorted({round(v.co.y, 6) for v in bm.verts})
    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    c = vert_at(bm, (xs[-1], ys[0], 0.0))   # same row as a
    m = vert_at(bm, (xs[2], ys[0], 0.0))    # middle of that row

    sess = session.SeamSession()
    sess.add_anchor(a.index)
    edges_ac = tree_path(bm, a, c)
    check("row path is the straight row", len(edges_ac) == len(xs) - 1)
    sess.commit_segment(a, edges_ac, clearing=False)

    edges_cm = tree_path(bm, c, m, prefer_seams=True)
    sess.commit_segment(c, edges_cm, clearing=True)
    erased = {e.index for e in edges_cm}
    check("second half seams gone, first half kept",
          {e.index for e in bm.edges if e.seam}
          == {e.index for e in edges_ac} - erased)
    polys = sess.overlay_polylines()
    check("mark overlay trimmed to the un-erased half, no grey",
          len(polys) == 1 and polys[0][1] is False
          and polys[0][0][0] == a.index and polys[0][0][-1] == m.index,
          "got %r" % polys)
    check("anchor dots: mark endpoints + current anchor",
          set(sess.overlay_anchor_indices()) == {a.index, c.index, m.index})

    sess.undo_last(bm)
    check("undo re-extends the mark overlay to full length",
          [(p[0][0], p[0][-1], p[1]) for p in sess.overlay_polylines()]
          == [(a.index, c.index, False)])
    check("undo restores full seam row",
          {e.index for e in bm.edges if e.seam}
          == {e.index for e in edges_ac})
    bm.free()


def test_prefer_seams_never_detours():
    print("test_prefer_seams_never_detours")
    # Seam preference must only flip ties, never win via a longer route:
    # direct unseamed edge (length 2) vs an all-seam detour (length 2.5).
    bm = bmesh.new()
    a = bm.verts.new((0.0, 0.0, 0.0))
    b = bm.verts.new((2.0, 0.0, 0.0))
    direct = bm.edges.new((a, b))
    t = bm.verts.new((1.0, 0.75, 0.0))  # |a-t| = |t-b| = 1.25
    e1 = bm.edges.new((a, t))
    e2 = bm.edges.new((t, b))
    e1.seam = True
    e2.seam = True
    bm.verts.ensure_lookup_table()
    path = tree_path(bm, a, b, prefer_seams=True)
    check("shorter unseamed route still wins over seam detour",
          path == [direct])
    bm.free()


# ---------------------------------------------------------------------------
# Bug 1 regression: pure occlusion logic against a constructed BVH
# ---------------------------------------------------------------------------

def test_picking_occlusion():
    print("test_picking_occlusion")
    # Two parallel quads: front at z=0, back at z=-2. Viewer at z=+5
    # looking down -z. The back quad is larger so its verts are laterally
    # clear of the front quad except where we aim through it.
    front = [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
             (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]
    back = [(-3.0, -3.0, -2.0), (3.0, -3.0, -2.0),
            (3.0, 3.0, -2.0), (-3.0, 3.0, -2.0)]
    bvh = BVHTree.FromPolygons(front + back, [(0, 1, 2, 3), (4, 5, 6, 7)])

    # Mouse ray through the middle: the FRONT polygon must win the pick.
    loc, _n, index, dist = bvh.ray_cast(Vector((0.0, 0.0, 5.0)),
                                        Vector((0.0, 0.0, -1.0)))
    check("view ray hits the front face, not the back",
          index == 0 and math.isclose(dist, 5.0, rel_tol=1e-6))

    down = Vector((0.0, 0.0, -1.0))

    # A back-quad point straight behind the front quad is occluded ...
    origin = Vector((0.5, 0.5, 5.0))
    check("vertex behind front surface is rejected",
          not picking.is_vert_visible(bvh, origin, down, 7.0))
    # ... while a front-quad vertex is NOT rejected by its own face
    # (ray hits the face exactly at the vertex distance: epsilon rule).
    origin = Vector((1.0, -1.0, 5.0))
    check("front vertex does not self-occlude",
          picking.is_vert_visible(bvh, origin, down, 5.0))
    # A laterally clear back-quad vertex (nothing in front of it) is
    # visible even though it is on the far plane.
    origin = Vector((3.0, 3.0, 5.0))
    check("unobstructed far vertex is visible",
          picking.is_vert_visible(bvh, origin, down, 7.0))
    # A ray that misses everything is visible by definition.
    origin = Vector((10.0, 10.0, 5.0))
    check("ray hitting nothing counts as visible",
          picking.is_vert_visible(bvh, origin, down, 4.0))

    # first_visible_candidate: priority order wins among visible, and
    # occluded entries are skipped even when they are first.
    candidates = [
        ("behind-front", Vector((0.5, 0.5, 5.0)), down, 7.0),   # occluded
        ("front-vert", Vector((1.0, -1.0, 5.0)), down, 5.0),    # visible
        ("clear-back", Vector((3.0, 3.0, 5.0)), down, 7.0),     # visible
    ]
    check("first visible candidate wins, occluded ones are skipped",
          picking.first_visible_candidate(bvh, candidates) == "front-vert")
    check("all-occluded candidate list yields None",
          picking.first_visible_candidate(
              bvh, [("x", Vector((0.5, 0.5, 5.0)), down, 7.0)]) is None)

    # Epsilon scales with distance and has an absolute floor.
    check("occlusion epsilon has a floor and grows with distance",
          picking.occlusion_epsilon(0.0) > 0.0
          and picking.occlusion_epsilon(1000.0)
          > picking.occlusion_epsilon(1.0))


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_erase_regression_bug2,
        test_undo_preserves_preexisting_seams,
        test_erase_preexisting_seam_shows_grey,
        test_partial_erase_splits_overlay,
        test_prefer_seams_never_detours,
        test_picking_occlusion,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            traceback.print_exc()
            FAILURES.append("%s raised" % t.__name__)

    sys.stdout.flush()
    if FAILURES:
        print("SESSION_TESTS_FAILED: %d failure(s): %s"
              % (len(FAILURES), ", ".join(FAILURES)))
    else:
        print("SESSION_TESTS_PASSED")
    sys.stdout.flush()


main()
