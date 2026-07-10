# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for the 1.4.0 array tree backends in core.py
(run inside `blender --background --python`).

Covers the cross-backend correctness contract the fast tree fills rest
on (slicer == scipy == vectorized BFS):

- Path COSTS identical to the pure-Python slicer everywhere (all verts,
  all meshes, both modes), within float tolerance.
- Every returned path is a valid contiguous edge chain whose summed
  weight equals the reported distance.
- Paths identical wherever the shortest path is UNIQUE (jittered grid);
  on tie-rich meshes backends may legitimately pick different equal-cost
  paths (same latitude astar_path already has).
- prefer_seams retracing (the Bug-2 erase regression) per backend: after
  marking a path, a seam-preferring tree from the OTHER end must retrace
  exactly the marked edges — the discount makes that route a STRICT
  optimum, so every backend must agree. Verified for LENGTH (scipy) and
  TOPOLOGY (scipy + the BFS two-tier frontier), including a long
  (>100-edge) seam so the 1e-6 relative discount is shown to survive
  each backend's float handling.
- Hidden verts/edges are excluded exactly like the slicer excludes them.
- Backend selection (select_tree_backend / make_tree), the graceful
  no-scipy degradation (exercised via core._scipy_override even when
  scipy IS installed), the slicer-compatible advance() budget contract,
  per-session GraphArrays caching, and incremental seam-flag updates.

scipy is OPTIONAL: when it is not importable, the scipy-backend cases
are skipped (with a printed note) and everything else must still pass.

Prints BACKEND_TESTS_PASSED on success.
"""

import math
import os
import sys
import traceback

import bmesh
from mathutils import Matrix, Vector

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from seam_path_tool import core  # noqa: E402

FAILURES = []

# Set SEAM_PATH_NO_SCIPY=1 to run this whole file as if scipy were not
# installed (validates the degraded world on a Blender that has scipy).
if os.environ.get("SEAM_PATH_NO_SCIPY"):
    print("SEAM_PATH_NO_SCIPY set: simulating a Blender without scipy")
    core._scipy_override = False

HAVE_SCIPY = core.scipy_available()
print("scipy available in this Blender: %r" % HAVE_SCIPY)
if not HAVE_SCIPY:
    print("  NOTE: scipy backend cases will be SKIPPED (BFS/slicer cases "
          "still run and must pass)")


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def edge_ids(edges):
    return None if edges is None else [e.index for e in edges]


def make_grid(seg=16, stretch=None, jitter=0.0):
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
    return [
        ("grid", make_grid(seg=16)),
        ("stretched grid", make_grid(seg=12, stretch=(5.0, 1.0))),
        ("jittered grid", make_grid(seg=14, jitter=0.31)),
        ("sphere", make_sphere()),
    ]


def backend_cases(prefer_topology_only=False):
    """(backend, mode) pairs to test, honouring scipy availability."""
    cases = [('BFS', 'TOPOLOGY')]
    if HAVE_SCIPY:
        cases.append(('SCIPY', 'TOPOLOGY'))
        if not prefer_topology_only:
            cases.append(('SCIPY', 'LENGTH'))
    return cases


def solved_tree(bm, src, mode, backend, prefer_seams=False, cache=None):
    t = core.make_tree(bm, src, mode=mode, prefer_seams=prefer_seams,
                       cache=cache, backend=backend)
    t.advance()  # no budget: one-shot solve
    return t


def sample_targets(bm, n=40):
    step = max(1, len(bm.verts) // n)
    return list(bm.verts)[::step]


def chain_is_valid(v_from, v_to, edges):
    """True if edges form a contiguous chain v_from -> v_to."""
    v = v_from
    for e in edges:
        if v not in e.verts:
            return False
        v = e.other_vert(v)
    return v is v_to


# ---------------------------------------------------------------------------

def test_costs_match_slicer_everywhere():
    print("test_costs_match_slicer_everywhere")
    # Predecessor trees may differ on ties, but path costs must agree
    # with the pure-Python slicer at EVERY vertex, on every mesh.
    for mesh_name, bm in meshes():
        for backend, mode in backend_cases():
            dist_full, _prev = core.dijkstra_tree(bm, bm.verts[0], mode=mode)
            t = solved_tree(bm, bm.verts[0], mode, backend)
            ok = True
            detail = ""
            for v in bm.verts:
                d_py = dist_full.get(v)
                d_ar = t.distance(v)
                if (d_py is None) != (d_ar is None):
                    ok, detail = False, "reachability differs at v%d" % v.index
                    break
                if d_py is not None and not math.isclose(
                        d_py, d_ar, rel_tol=1e-9, abs_tol=1e-12):
                    ok, detail = False, ("v%d cost %r != %r"
                                         % (v.index, d_ar, d_py))
                    break
            check("%s %s/%s costs == slicer (all %d verts)"
                  % (mesh_name, backend, mode, len(bm.verts)), ok, detail)
        bm.free()


def test_paths_valid_and_unique_paths_identical():
    print("test_paths_valid_and_unique_paths_identical")
    # Every backend path must be a valid chain whose cost equals the
    # reported distance; where the shortest path is unique (jittered
    # grid, LENGTH) the edge list must match the slicer's exactly.
    for mesh_name, bm in meshes():
        for backend, mode in backend_cases():
            src = bm.verts[0]
            t = solved_tree(bm, src, mode, backend)
            ok = True
            detail = ""
            for tgt in sample_targets(bm):
                p = core.path_from_tree(t.tree, src, tgt)
                if tgt is src:
                    if p != []:
                        ok, detail = False, "self path not []"
                        break
                    continue
                if p is None:
                    ok, detail = False, "v%d unreachable" % tgt.index
                    break
                if not chain_is_valid(src, tgt, p):
                    ok, detail = False, "v%d chain invalid" % tgt.index
                    break
                cost = (core.path_length(p) if mode == 'LENGTH'
                        else float(len(p)))
                if not math.isclose(cost, t.distance(tgt),
                                    rel_tol=1e-9, abs_tol=1e-12):
                    ok, detail = False, ("v%d chain cost %r != dist %r"
                                         % (tgt.index, cost, t.distance(tgt)))
                    break
                # path() (direct) and path_from_tree (modal call shape)
                # must be the same thing.
                if edge_ids(t.path(src, tgt)) != edge_ids(p):
                    ok, detail = False, "path()/path_from_tree disagree"
                    break
            check("%s %s/%s paths valid, cost == dist (sampled targets)"
                  % (mesh_name, backend, mode), ok, detail)
        bm.free()

    if HAVE_SCIPY:
        # Unique-shortest-path mesh: scipy LENGTH tree must pick the
        # exact slicer paths (no ties to hide behind).
        bm = make_grid(seg=14, jitter=0.31)
        src = bm.verts[0]
        tree_full = core.dijkstra_tree(bm, src, mode='LENGTH')
        t = solved_tree(bm, src, 'LENGTH', 'SCIPY')
        ok = all(
            edge_ids(t.path(src, tgt))
            == edge_ids(core.path_from_tree(tree_full, src, tgt))
            for tgt in sample_targets(bm))
        check("jittered grid SCIPY/LENGTH paths identical to slicer "
              "(unique-path mesh)", ok)
        bm.free()
    else:
        print("  skip scipy unique-path case (scipy unavailable)")


def test_prefer_seams_retrace_per_backend():
    print("test_prefer_seams_retrace_per_backend")
    # The Bug-2 erase-retrace regression, per backend: mark a->b, then a
    # seam-preferring tree rooted at b must retrace EXACTLY the marked
    # edges. seg=60 makes the marked path 120 edges long, so the 1e-6
    # relative discount must survive >100 accumulations in each
    # backend's float handling to keep the seam route strictly optimal.
    for mode in ('LENGTH', 'TOPOLOGY'):
        bm = make_grid(seg=60)
        a = min(bm.verts, key=lambda v: (v.co.x + v.co.y, v.index))
        b = max(bm.verts, key=lambda v: (v.co.x + v.co.y, v.index))
        marked = core.mark_seam_path(bm, a, b, mode=mode)
        marked_ids = {e.index for e in marked}
        check("%s marked path is long enough to stress the discount "
              "(%d edges)" % (mode, len(marked)), len(marked) >= 100)

        # Slicer reference (already covered by test_incremental, used
        # here for cost comparison).
        dist_ps, _prev = core.dijkstra_tree(bm, b, mode=mode,
                                            prefer_seams=True)
        for backend, m in backend_cases():
            if m != mode:
                continue
            t = solved_tree(bm, b, mode, backend, prefer_seams=True)
            p = t.path(b, a)
            check("%s/%s prefer_seams retraces the marked edges exactly"
                  % (backend, mode),
                  p is not None and {e.index for e in p} == marked_ids,
                  "got %r" % (sorted(e.index for e in p) if p else p))
            check("%s/%s discounted cost matches slicer at far end"
                  % (backend, mode),
                  math.isclose(t.distance(a), dist_ps[a],
                               rel_tol=1e-9, abs_tol=1e-12),
                  "%r != %r" % (t.distance(a), dist_ps[a]))
            # The discount must actually have bitten: discounted cost is
            # strictly below the undiscounted optimum by ~len*tie/...
            t_plain = solved_tree(bm, b, mode, backend, prefer_seams=False)
            check("%s/%s discount survives float handling (strictly "
                  "cheaper than undiscounted)" % (backend, mode),
                  t.distance(a) < t_plain.distance(a))
        bm.free()
    if not HAVE_SCIPY:
        print("  skip scipy retrace cases (scipy unavailable)")


def test_hidden_exclusion_matches_slicer():
    print("test_hidden_exclusion_matches_slicer")
    # Hide a vertical band of verts plus a few extra edges; every
    # backend must agree with the slicer on reachability AND distance at
    # every vertex (the slicer skips e.hide / other.hide at relaxation
    # time; the arrays exclude those edges at build time).
    bm = make_grid(seg=12)
    xs = sorted({round(v.co.x, 6) for v in bm.verts})
    band_x = xs[len(xs) // 2]
    for v in bm.verts:
        if abs(v.co.x - band_x) < 1e-9 and v.co.y < 0.5:
            v.hide = True
    for i, e in enumerate(bm.edges):
        if i % 37 == 0:
            e.hide = True
    src = bm.verts[0]
    assert not src.hide

    for backend, mode in backend_cases():
        dist_full, _prev = core.dijkstra_tree(bm, src, mode=mode)
        t = solved_tree(bm, src, mode, backend)
        ok = True
        detail = ""
        hidden_unreachable = True
        for v in bm.verts:
            d_py = dist_full.get(v)
            d_ar = t.distance(v)
            if v.hide and d_ar is not None:
                hidden_unreachable = False
            if (d_py is None) != (d_ar is None):
                ok, detail = False, "reachability differs at v%d" % v.index
                break
            if d_py is not None and not math.isclose(
                    d_py, d_ar, rel_tol=1e-9, abs_tol=1e-12):
                ok, detail = False, "v%d cost differs" % v.index
                break
        check("hidden band: %s/%s dists == slicer (all verts)"
              % (backend, mode), ok, detail)
        check("hidden band: %s/%s hidden verts unreachable"
              % (backend, mode), hidden_unreachable)
    bm.free()


def test_backend_selection_and_fallbacks():
    print("test_backend_selection_and_fallbacks")
    big = core.ARRAY_BACKEND_MIN_VERTS + 1
    small = core.ARRAY_BACKEND_MIN_VERTS - 1

    # Below the threshold the slicer always wins (array build overhead
    # buys nothing when the pure-Python tree fits in a few slices).
    check("small mesh -> SLICER (both modes)",
          core.select_tree_backend(small, 'LENGTH') == 'SLICER'
          and core.select_tree_backend(small, 'TOPOLOGY') == 'SLICER')

    # The no-scipy world, forced via the test hook so it is exercised
    # even on a Blender that has scipy installed.
    prev_override = core._scipy_override
    core._scipy_override = False
    try:
        check("no scipy: big LENGTH -> SLICER (universal fallback)",
              core.select_tree_backend(big, 'LENGTH') == 'SLICER')
        check("no scipy: big TOPOLOGY -> BFS (numpy always available)",
              core.select_tree_backend(big, 'TOPOLOGY') == 'BFS')
        check("no scipy: scipy_available() honours override",
              not core.scipy_available())
        bm = make_grid(seg=16)
        t = core.make_tree(bm, bm.verts[0], mode='LENGTH')
        check("no scipy: make_tree on small mesh returns DijkstraSlicer",
              isinstance(t, core.DijkstraSlicer) and t.sliceable)
        bm.free()
    finally:
        core._scipy_override = prev_override

    if HAVE_SCIPY:
        check("with scipy: big LENGTH -> SCIPY",
              core.select_tree_backend(big, 'LENGTH') == 'SCIPY')
        check("with scipy: big TOPOLOGY -> SCIPY",
              core.select_tree_backend(big, 'TOPOLOGY') == 'SCIPY')
    else:
        print("  skip with-scipy selection checks (scipy unavailable)")

    try:
        core.select_tree_backend(100, 'NOPE')
        check("select_tree_backend rejects bad mode", False)
    except ValueError:
        check("select_tree_backend rejects bad mode", True)
    try:
        bm = make_grid(seg=4)
        core.make_tree(bm, bm.verts[0], mode='LENGTH', backend='BFS')
        check("BFS backend rejects LENGTH mode", False)
    except ValueError:
        check("BFS backend rejects LENGTH mode", True)
    finally:
        bm.free()


def test_arraytree_driving_contract():
    print("test_arraytree_driving_contract")
    # The slicer-compatible interface the modal drives: a too-small
    # advance budget does NOTHING (returns False, tree not computed —
    # this is what keeps hover boosts and commit clicks hitch-free on
    # big meshes); an unbudgeted advance solves completely.
    bm = make_grid(seg=16)
    src = bm.verts[0]
    cache = {}
    t = core.make_tree(bm, src, mode='TOPOLOGY', backend='BFS', cache=cache)
    check("one-shot backends advertise sliceable=False", not t.sliceable)
    check("zero budget: advance refuses (returns False)",
          t.advance(time_budget=0.0) is False and not t.done)
    check("settled() False before solve", not t.settled(src))
    check("estimated_cost positive before solve", t.estimated_cost() > 0.0)
    check("unbudgeted advance solves", t.advance() is True and t.done)
    check("settled() True after solve (all verts)",
          t.settled(src) and t.settled(bm.verts[-1]))
    check("advance idempotent once done", t.advance(time_budget=0.0) is True)
    check("root distance 0, self path []",
          t.distance(src) == 0.0 and t.path(src, src) == [])
    check("settled_count == reachable count",
          t.settled_count == len(bm.verts))
    check("graph arrays landed in the caller's cache",
          isinstance(cache.get('graph'), core.GraphArrays))

    # Cache reuse across trees (the per-session invariant): a second
    # tree from another root must reuse the same GraphArrays object.
    g_first = cache['graph']
    t2 = core.make_tree(bm, bm.verts[-1], mode='TOPOLOGY', backend='BFS',
                        cache=cache)
    t2.advance()
    check("second tree reuses the session GraphArrays",
          cache['graph'] is g_first)
    check("ensure_settled True for reachable vert",
          t2.ensure_settled(bm.verts[0]))
    bm.free()

    # Disconnected components: unreachable targets are None/False, like
    # the slicer.
    bm = bmesh.new()
    ret1 = bmesh.ops.create_cube(bm, size=1.0)
    v1 = ret1["verts"][0]
    ret2 = bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.translate(bm, verts=ret2["verts"], vec=(10.0, 0.0, 0.0))
    v2 = ret2["verts"][0]
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    for backend, mode in backend_cases(prefer_topology_only=True):
        t = solved_tree(bm, v1, mode, backend)
        check("%s disconnected target: distance None, path None"
              % backend,
              t.distance(v2) is None and t.path(v1, v2) is None)
        check("%s ensure_settled False on disconnected target" % backend,
              not core.make_tree(bm, v1, mode=mode,
                                 backend=backend).ensure_settled(v2))
    bm.free()


def test_incremental_seam_updates():
    print("test_incremental_seam_updates")
    # The modal patches the cached seam flags per commit/undo instead of
    # re-extracting all edges; the patched array must equal a fresh
    # extraction, and a prefer_seams tree built AFTER the patch must
    # retrace the newly marked path.
    bm = make_grid(seg=20)
    cache = {}
    a = bm.verts[0]
    b = bm.verts[len(bm.verts) - 1]

    # Force seam extraction with no seams yet.
    t0 = solved_tree(bm, a, 'TOPOLOGY', 'BFS', prefer_seams=True,
                     cache=cache)
    graph = cache['graph']
    check("seam flags extracted lazily on first prefer_seams tree",
          graph.seams_extracted)

    # Mark a path, patch incrementally (what _note_seam_change does).
    marked = core.mark_seam_path(bm, a, b, mode='TOPOLOGY')
    graph.update_seams(bm, [e.index for e in marked])
    fresh = core.GraphArrays(bm)
    check("patched seam array == fresh extraction",
          bool((graph.seam_array(bm) == fresh.seam_array(bm)).all()))

    t1 = solved_tree(bm, b, 'TOPOLOGY', 'BFS', prefer_seams=True,
                     cache=cache)
    p = t1.path(b, a)
    check("prefer_seams tree after patch retraces the marked path",
          p is not None
          and {e.index for e in p} == {e.index for e in marked})

    # Undo-style change: clear the seams again, patch, verify.
    core.apply_seams(marked, clear=True)
    graph.update_seams(bm, [e.index for e in marked])
    check("patch after clearing matches fresh extraction",
          bool((graph.seam_array(bm)
                == core.GraphArrays(bm).seam_array(bm)).all()))
    check("t0 solved fine with empty seam set (sanity)",
          t0.distance(b) is not None)
    bm.free()


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_costs_match_slicer_everywhere,
        test_paths_valid_and_unique_paths_identical,
        test_prefer_seams_retrace_per_backend,
        test_hidden_exclusion_matches_slicer,
        test_backend_selection_and_fallbacks,
        test_arraytree_driving_contract,
        test_incremental_seam_updates,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            traceback.print_exc()
            FAILURES.append("%s raised" % t.__name__)

    sys.stdout.flush()
    if FAILURES:
        print("BACKEND_TESTS_FAILED: %d failure(s): %s"
              % (len(FAILURES), ", ".join(FAILURES)))
    else:
        print("BACKEND_TESTS_PASSED")
    sys.stdout.flush()


main()
