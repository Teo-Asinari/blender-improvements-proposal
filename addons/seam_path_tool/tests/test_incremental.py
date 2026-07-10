# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for the 1.3.0 incremental path machinery in core.py
(run inside `blender --background --python`).

Covers the invariants the interactive tool's performance design rests on:

- A `DijkstraSlicer` driven in arbitrary small slices (vertex budgets or
  time budgets) completes to a tree IDENTICAL to a one-shot
  `dijkstra_tree` run — same distances, same predecessor edges — across
  grid / stretched-grid / sphere meshes.
- `ensure_settled` (early-exit) yields exactly the same path as the full
  tree — same edge list including tie-breaking — for every sampled
  target, and for prefer_seams erase retracing (the Bug 2 semantics).
- Partial tree + `astar_path` fallback: targets settled in a partial
  tree give paths identical to the full tree; unsettled targets answered
  by astar_path have identical optimal cost (and identical edges where
  the shortest path is unique / seam-tie-broken).
- Performance sanity (not wall-clock asserts): early-exit to a nearby
  target settles a small fraction of what the full tree settles; measured
  timings are printed for the report.

Prints INCREMENTAL_TESTS_PASSED on success.
"""

import math
import os
import sys
import time
import traceback

import bmesh
from mathutils import Matrix, Vector

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


def edge_ids(edges):
    return None if edges is None else [e.index for e in edges]


def make_grid(seg=16, stretch=None, jitter=0.0):
    """Quad grid; optional non-uniform stretch and deterministic vertex
    jitter (jitter breaks shortest-path ties, making paths unique)."""
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=seg, y_segments=seg, size=1.0)
    if stretch is not None:
        bmesh.ops.transform(bm, matrix=Matrix.Diagonal(stretch + (1.0,)),
                            verts=bm.verts[:])
    if jitter:
        spacing = 2.0 / seg
        for i, v in enumerate(bm.verts):
            v.co.x += jitter * spacing * math.sin(7.13 * i + 0.4)
            v.co.y += jitter * spacing * math.cos(3.71 * i + 1.9)
            v.co.z += jitter * spacing * math.sin(1.93 * i)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    return bm


def make_sphere(useg=12, vseg=10):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=useg, v_segments=vseg,
                              radius=1.0)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    return bm


def meshes():
    """(name, bm) test meshes; caller frees."""
    return [
        ("grid", make_grid(seg=16)),
        ("stretched grid", make_grid(seg=12, stretch=(5.0, 1.0))),
        ("jittered grid", make_grid(seg=14, jitter=0.31)),
        ("sphere", make_sphere()),
    ]


def sample_targets(bm, n=40):
    step = max(1, len(bm.verts) // n)
    return list(bm.verts)[::step]


# ---------------------------------------------------------------------------

def test_sliced_completion_identical():
    print("test_sliced_completion_identical")
    # Driving the slicer in arbitrary tiny slices must give the exact
    # tree of a one-shot run: identical distances and predecessor edges.
    # (Exercises the pause/resume boundary: a vertex's edges are relaxed
    # in the same step that settles it, so no resume can miss relaxations.)
    for mesh_name, bm in meshes():
        for mode in ('LENGTH', 'TOPOLOGY'):
            src = bm.verts[0]
            dist_full, prev_full = core.dijkstra_tree(bm, src, mode=mode)

            s = core.DijkstraSlicer(bm, src, mode=mode)
            steps = 0
            while not s.advance(max_verts=7):
                steps += 1
                assert steps < 10 ** 6, "slicer failed to terminate"
            check("%s/%s sliced dist == one-shot dist" % (mesh_name, mode),
                  {v.index: d for v, d in s.dist.items()}
                  == {v.index: d for v, d in dist_full.items()})
            check("%s/%s sliced prev == one-shot prev" % (mesh_name, mode),
                  {v.index: e.index for v, e in s.prev_edge.items()}
                  == {v.index: e.index for v, e in prev_full.items()})
            check("%s/%s sliced settles every reachable vert"
                  % (mesh_name, mode),
                  s.settled_count == len(dist_full))

            # Time-budgeted slices terminate and give the same tree too.
            s2 = core.DijkstraSlicer(bm, src, mode=mode)
            steps = 0
            while not s2.advance(time_budget=0.0005):
                steps += 1
                assert steps < 10 ** 6, "slicer failed to terminate"
            check("%s/%s time-sliced tree identical" % (mesh_name, mode),
                  {v.index: d for v, d in s2.dist.items()}
                  == {v.index: d for v, d in dist_full.items()})
        bm.free()


def test_early_exit_identical_paths():
    print("test_early_exit_identical_paths")
    # ensure_settled must return the exact full-tree path (same edges,
    # same tie-breaking) for every sampled target — it is the same heap
    # sequence merely stopped early.
    for mesh_name, bm in meshes():
        src = bm.verts[0]
        tree_full = core.dijkstra_tree(bm, src, mode='LENGTH')
        ok = True
        detail = ""
        for t in sample_targets(bm):
            s = core.DijkstraSlicer(bm, src, mode='LENGTH')
            reached = s.ensure_settled(t)
            p_early = core.path_from_tree(s.tree, src, t) if reached else None
            p_full = core.path_from_tree(tree_full, src, t)
            if edge_ids(p_early) != edge_ids(p_full):
                ok = False
                detail = "target %d: %r != %r" % (
                    t.index, edge_ids(p_early), edge_ids(p_full))
                break
        check("%s early-exit path identical for all sampled targets"
              % mesh_name, ok, detail)
        bm.free()


def test_early_exit_prefer_seams_retrace():
    print("test_early_exit_prefer_seams_retrace")
    # The Bug 2 semantics through the early-exit machinery: mark a path
    # a->b, then an early-exit prefer_seams query from b must retrace
    # exactly the marked edges (and identically to the full
    # prefer_seams tree).
    bm = make_grid(seg=12)
    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    ys = sorted({round(v.co.y, 6) for v in bm.verts})

    def vert_at(x, y):
        return min(bm.verts, key=lambda v: (v.co - Vector((x, y, 0))).length)

    a = vert_at(xs[0], ys[0])
    b = vert_at(xs[-1], ys[-1])

    marked = core.mark_seam_path(bm, a, b, mode='LENGTH')
    marked_ids = {e.index for e in marked}

    tree_full = core.dijkstra_tree(bm, b, mode='LENGTH', prefer_seams=True)
    p_full = core.path_from_tree(tree_full, b, a)

    s = core.DijkstraSlicer(bm, b, mode='LENGTH', prefer_seams=True)
    check("prefer_seams early-exit reaches source", s.ensure_settled(a))
    p_early = core.path_from_tree(s.tree, b, a)

    check("prefer_seams early-exit path == full-tree path",
          edge_ids(p_early) == edge_ids(p_full))
    check("prefer_seams early-exit retraces the marked edges exactly",
          {e.index for e in p_early} == marked_ids)
    # And the A* fallback retraces it too (fully seamed optimum is a
    # STRICT optimum under the discounted weights, so no tie ambiguity).
    p_astar = core.astar_path(bm, b, a, mode='LENGTH', prefer_seams=True)
    check("prefer_seams astar_path retraces the marked edges exactly",
          {e.index for e in p_astar} == marked_ids)
    bm.free()


def test_partial_tree_plus_astar_fallback():
    print("test_partial_tree_plus_astar_fallback")
    # The modal's hover strategy: partially filled tree answers settled
    # targets (identical to full tree); unsettled targets fall back to
    # astar_path, which must have identical optimal cost — and identical
    # edges where shortest paths are unique (jittered grid).
    for mesh_name, bm, unique_paths in [
            ("jittered grid", make_grid(seg=14, jitter=0.31), True),
            ("grid", make_grid(seg=16), False),
            ("sphere", make_sphere(), False)]:
        src = bm.verts[0]
        tree_full = core.dijkstra_tree(bm, src, mode='LENGTH')
        dist_full = tree_full[0]

        s = core.DijkstraSlicer(bm, src, mode='LENGTH')
        s.advance(max_verts=len(bm.verts) // 3)  # deliberately partial

        settled_hits = astar_hits = 0
        ok = True
        detail = ""
        for t in sample_targets(bm):
            p_full = core.path_from_tree(tree_full, src, t)
            if s.settled(t):
                settled_hits += 1
                p = core.path_from_tree(s.tree, src, t)
                if edge_ids(p) != edge_ids(p_full):
                    ok = False
                    detail = "settled target %d path differs" % t.index
                    break
            else:
                astar_hits += 1
                p = core.astar_path(bm, src, t, mode='LENGTH')
                if (p is None) != (p_full is None):
                    ok = False
                    detail = "astar target %d reachability differs" % t.index
                    break
                if p is None:
                    continue
                c_full = dist_full[t]
                c_astar = core.path_length(p)
                if not math.isclose(c_astar, c_full, rel_tol=1e-9,
                                    abs_tol=1e-12):
                    ok = False
                    detail = "astar target %d cost %r != %r" % (
                        t.index, c_astar, c_full)
                    break
                if unique_paths and edge_ids(p) != edge_ids(p_full):
                    ok = False
                    detail = "astar target %d path differs (unique mesh)" \
                        % t.index
                    break
        check("%s partial-tree + astar fallback consistent (settled=%d "
              "astar=%d)" % (mesh_name, settled_hits, astar_hits), ok, detail)
        check("%s partial fill exercises both branches" % mesh_name,
              settled_hits > 0 and astar_hits > 0,
              "settled=%d astar=%d" % (settled_hits, astar_hits))
        bm.free()

    # TOPOLOGY astar_path degenerates to early-exit Dijkstra: identical
    # edges to the full tree (same tie-breaking), for every sampled target.
    bm = make_grid(seg=14, jitter=0.31)
    src = bm.verts[0]
    tree_full = core.dijkstra_tree(bm, src, mode='TOPOLOGY')
    ok = all(
        edge_ids(core.astar_path(bm, src, t, mode='TOPOLOGY'))
        == edge_ids(core.path_from_tree(tree_full, src, t))
        for t in sample_targets(bm))
    check("TOPOLOGY astar_path identical to full tree (all targets)", ok)
    bm.free()


def test_disconnected_and_degenerate():
    print("test_disconnected_and_degenerate")
    bm = bmesh.new()
    ret1 = bmesh.ops.create_cube(bm, size=1.0)
    v1 = ret1["verts"][0]
    ret2 = bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.translate(bm, verts=ret2["verts"], vec=(10.0, 0.0, 0.0))
    v2 = ret2["verts"][0]

    s = core.DijkstraSlicer(bm, v1, mode='LENGTH')
    check("ensure_settled on disconnected target returns False",
          not s.ensure_settled(v2))
    check("slicer done after exhausting the component", s.done)
    check("disconnected target absent from partial tree",
          core.path_from_tree(s.tree, v1, v2) is None)
    check("astar_path disconnected returns None",
          core.astar_path(bm, v1, v2, mode='LENGTH') is None)
    check("astar_path same vertex returns []",
          core.astar_path(bm, v1, v1, mode='LENGTH') == [])
    check("advance with zero work returns done state", s.advance() is True)
    try:
        core.DijkstraSlicer(bm, v1, mode='NOPE')
        check("slicer rejects bad mode", False)
    except ValueError:
        check("slicer rejects bad mode", True)
    bm.free()


def test_perf_sanity():
    print("test_perf_sanity")
    # Structural (not wall-clock) assertion: early-exit to a nearby
    # target settles a small fraction of the full tree. Timings are
    # printed for the report only.
    seg = 120
    bm = make_grid(seg=seg)
    corner = min(bm.verts, key=lambda v: (v.co.x + v.co.y, v.index))
    far = max(bm.verts, key=lambda v: (v.co.x + v.co.y, v.index))
    spacing = 2.0 / seg
    near_co = corner.co + Vector((10 * spacing, 10 * spacing, 0.0))
    near = min(bm.verts, key=lambda v: (v.co - near_co).length)

    t0 = time.perf_counter()
    dist_full, _prev = core.dijkstra_tree(bm, corner, mode='LENGTH')
    t_full = time.perf_counter() - t0
    full_settled = len(dist_full)

    s = core.DijkstraSlicer(bm, corner, mode='LENGTH')
    t0 = time.perf_counter()
    s.ensure_settled(near)
    t_near = time.perf_counter() - t0

    t0 = time.perf_counter()
    p_far = core.astar_path(bm, corner, far, mode='LENGTH')
    t_astar = time.perf_counter() - t0

    print("  [perf] %d verts: full tree %.1f ms (settled %d) | "
          "early-exit near %.2f ms (settled %d) | astar far %.1f ms "
          "(%d edges)"
          % (len(bm.verts), t_full * 1e3, full_settled,
             t_near * 1e3, s.settled_count, t_astar * 1e3, len(p_far)))

    check("early-exit settles < 5%% of the full tree (near target)",
          s.settled_count < 0.05 * full_settled,
          "settled %d of %d" % (s.settled_count, full_settled))
    check("full tree settles every vertex", full_settled == len(bm.verts))
    bm.free()


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_sliced_completion_identical,
        test_early_exit_identical_paths,
        test_early_exit_prefer_seams_retrace,
        test_partial_tree_plus_astar_fallback,
        test_disconnected_and_degenerate,
        test_perf_sanity,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            traceback.print_exc()
            FAILURES.append("%s raised" % t.__name__)

    sys.stdout.flush()
    if FAILURES:
        print("INCREMENTAL_TESTS_FAILED: %d failure(s): %s"
              % (len(FAILURES), ", ".join(FAILURES)))
    else:
        print("INCREMENTAL_TESTS_PASSED")
    sys.stdout.flush()


main()
