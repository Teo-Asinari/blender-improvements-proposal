# SPDX-License-Identifier: GPL-2.0-or-later
"""Profiling harness for the commit-click cost on a ~300k-vert mesh.

NOT part of the test suite (run_tests.sh does not run it) — this is a
measurement tool used to drive/validate the 1.3.0 performance work.

Run headless:
  "/mnt/c/Program Files/Blender Foundation/Blender 5.1/blender.exe" \
      --background --factory-startup \
      --python "$(wslpath -w .../tests/profile_commit.py)"

Measures, on a subdivided grid of ~300k verts, the pieces a commit click
pays for:
  - core.dijkstra_tree: full single-source tree from one vertex
  - early-exit variant (stop when target settled): nearby + far target
  - A* early-exit (euclidean lower bound) to a far target
  - bpy.ops.ed.undo_push in edit mode
  - bmesh.update_edit_mesh (loop_triangles=False, destructive=False)
  - BVHTree.FromPolygons via the add-on's _build_pick_bvh
Plus audits:
  - does bmesh.from_edit_mesh return the same wrapper across calls?
  - does ed.undo_push swap the wrapper (invalidate caches keyed on it)?

Prints PROFILE_DONE at the end.
"""

import heapq
import os
import sys
import time

import bpy
import bmesh
from mathutils import Vector

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from seam_path_tool import core  # noqa: E402

SEG = 547  # (SEG+1)^2 verts: 548^2 = 300304


def timeit(label, fn, repeat=1):
    best = None
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    print("  %-46s %8.1f ms" % (label, best * 1000.0))
    return result, best


# ---------------------------------------------------------------------------
# Reference implementations for the candidate optimizations (prototypes;
# the real ones live in core.py after the fix).
# ---------------------------------------------------------------------------

def dijkstra_early_exit(bm, v_from, v_to, mode='LENGTH', prefer_seams=False):
    """Same heap/relaxation as core.dijkstra_tree, stop when v_to settles.
    Returns (dist, prev_edge, settled_count)."""
    if mode == 'LENGTH':
        def base_weight(edge):
            return edge.calc_length()
    else:
        def base_weight(edge):
            return 1.0
    if prefer_seams:
        def weight(edge):
            w = base_weight(edge)
            return w * (1.0 - core.SEAM_TIE_BREAK) if edge.seam else w
    else:
        weight = base_weight

    dist = {v_from: 0.0}
    prev_edge = {}
    visited = set()
    counter = 0
    heap = [(0.0, 0, v_from)]
    while heap:
        d, _, v = heapq.heappop(heap)
        if v in visited:
            continue
        visited.add(v)
        if v is v_to:
            break
        for e in v.link_edges:
            if e.hide:
                continue
            other = e.other_vert(v)
            if other in visited or other.hide:
                continue
            nd = d + weight(e)
            if nd < dist.get(other, float('inf')):
                dist[other] = nd
                prev_edge[other] = e
                counter += 1
                heapq.heappush(heap, (nd, counter, other))
    return dist, prev_edge, len(visited)


def astar_early_exit(bm, v_from, v_to, prefer_seams=False):
    """A* with euclidean lower-bound heuristic (LENGTH mode).
    Returns (dist, prev_edge, settled_count)."""
    hscale = (1.0 - core.SEAM_TIE_BREAK) if prefer_seams else 1.0
    goal_co = v_to.co

    dist = {v_from: 0.0}
    prev_edge = {}
    visited = set()
    counter = 0
    heap = [((v_from.co - goal_co).length * hscale, 0, v_from)]
    while heap:
        _f, _, v = heapq.heappop(heap)
        if v in visited:
            continue
        visited.add(v)
        if v is v_to:
            break
        d = dist[v]
        for e in v.link_edges:
            if e.hide:
                continue
            other = e.other_vert(v)
            if other in visited or other.hide:
                continue
            w = e.calc_length()
            if prefer_seams and e.seam:
                w *= (1.0 - core.SEAM_TIE_BREAK)
            nd = d + w
            if nd < dist.get(other, float('inf')):
                dist[other] = nd
                prev_edge[other] = e
                counter += 1
                h = (other.co - goal_co).length * hscale
                heapq.heappush(heap, (nd + h, counter, other))
    return dist, prev_edge, len(visited)


# ---------------------------------------------------------------------------

def main():
    print("profile_commit: building %dx%d grid ..." % (SEG + 1, SEG + 1))
    t0 = time.perf_counter()
    bmb = bmesh.new()
    bmesh.ops.create_grid(bmb, x_segments=SEG, y_segments=SEG, size=1.0)
    me = bpy.data.meshes.new("grid")
    bmb.to_mesh(me)
    bmb.free()
    obj = bpy.data.objects.new("grid", me)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    print("  mesh build: %.1f ms" % ((time.perf_counter() - t0) * 1000.0))

    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(me)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    print("  verts=%d edges=%d faces=%d"
          % (len(bm.verts), len(bm.edges), len(bm.faces)))

    # Corner / near / far picks.
    corner = min(bm.verts, key=lambda v: (v.co.x + v.co.y, v.index))
    far = max(bm.verts, key=lambda v: (v.co.x + v.co.y, v.index))
    spacing = 2.0 / SEG
    near_target_co = corner.co.copy()
    near_target_co.x += 10 * spacing
    near_target_co.y += 10 * spacing
    near = min(bm.verts, key=lambda v: (v.co - near_target_co).length)
    print("  corner=%d near=%d (10+10 hops) far=%d"
          % (corner.index, near.index, far.index))

    # --- re-acquire / lookup-table cost (paid per mousemove) ---------------
    def reacquire():
        b = bmesh.from_edit_mesh(me)
        b.verts.ensure_lookup_table()
        b.edges.ensure_lookup_table()
        return b
    timeit("from_edit_mesh + ensure_lookup_table", reacquire, repeat=3)

    # --- full single-source tree (what a click currently pays) -------------
    (tree, t_full) = timeit("core.dijkstra_tree FULL (LENGTH)",
                            lambda: core.dijkstra_tree(bm, corner,
                                                       mode='LENGTH'))
    dist_full, prev_full = tree
    print("      settled %d verts  (%.0f verts/ms)"
          % (len(dist_full), len(dist_full) / (t_full * 1000.0)))
    timeit("core.dijkstra_tree FULL (LENGTH, prefer_seams)",
           lambda: core.dijkstra_tree(bm, corner, mode='LENGTH',
                                      prefer_seams=True))
    timeit("core.dijkstra_tree FULL (TOPOLOGY)",
           lambda: core.dijkstra_tree(bm, corner, mode='TOPOLOGY'))

    # --- early-exit variants ------------------------------------------------
    (res, _t) = timeit("early-exit Dijkstra -> NEAR target",
                       lambda: dijkstra_early_exit(bm, corner, near))
    print("      settled %d verts (full tree: %d)"
          % (res[2], len(dist_full)))
    (res, _t) = timeit("early-exit Dijkstra -> FAR target",
                       lambda: dijkstra_early_exit(bm, corner, far))
    print("      settled %d verts" % res[2])
    (res, _t) = timeit("A* (euclid) -> FAR target",
                       lambda: astar_early_exit(bm, corner, far))
    print("      settled %d verts" % res[2])
    (res, _t) = timeit("A* (euclid) -> NEAR target",
                       lambda: astar_early_exit(bm, corner, near))
    print("      settled %d verts" % res[2])
    # A* correctness spot-check vs the full tree.
    p_tree = core.path_from_tree((dist_full, prev_full), corner, far)
    d_astar, pe_astar, _n = astar_early_exit(bm, corner, far)
    p_astar = core.path_from_tree((d_astar, pe_astar), corner, far)
    same_cost = abs(core.path_length(p_tree) - core.path_length(p_astar)) \
        < 1e-9 * max(1.0, core.path_length(p_tree))
    print("      A* far path cost == tree path cost: %r" % same_cost)

    # --- BVH build (occlusion picking) --------------------------------------
    import seam_path_tool as spt
    timeit("_build_pick_bvh (BVHTree.FromPolygons)",
           lambda: spt._build_pick_bvh(bm), repeat=2)

    # --- update_edit_mesh / undo_push ---------------------------------------
    # Flip some seam flags first so the calls are representative of a commit.
    path_edges = core.path_from_tree((dist_full, prev_full), corner, far)
    for e in path_edges:
        e.seam = True
    timeit("bmesh.update_edit_mesh (flags-only)",
           lambda: bmesh.update_edit_mesh(me, loop_triangles=False,
                                          destructive=False), repeat=3)
    timeit("bpy.ops.ed.undo_push",
           lambda: bpy.ops.ed.undo_push(message="profile"), repeat=3)

    # --- AFTER (1.3.0): what a commit click costs with the new design -------
    # Commit = reuse the previewed edge list (free) + session bookkeeping +
    # initial DijkstraSlicer slice + update_edit_mesh + undo_push; the rest
    # of the tree fills in ~12 ms TIMER slices.
    print("post-fix commit-click simulation:")
    from seam_path_tool import session as spt_session

    sess = spt_session.SeamSession()
    sess.add_anchor(corner.index)
    hover_edges = core.path_from_tree((dist_full, prev_full), corner, far)
    for e in hover_edges:  # start from a clean slate
        e.seam = False

    t0 = time.perf_counter()
    sess.commit_segment(corner, hover_edges, clearing=False)
    slicer = core.DijkstraSlicer(bm, far, mode='LENGTH')
    slicer.advance(time_budget=0.015)
    bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
    bpy.ops.ed.undo_push(message="profile-commit")
    t_click = time.perf_counter() - t0
    print("  click total (commit+initial slice+update+undo)  %8.1f ms"
          % (t_click * 1000.0))
    print("      initial slice settled %d verts" % slicer.settled_count)

    ticks = 0
    t0 = time.perf_counter()
    while not slicer.advance(time_budget=0.012):
        ticks += 1
    t_fill = time.perf_counter() - t0
    print("      background fill: %d x 12ms slices, %.0f ms total"
          % (ticks + 1, t_fill * 1000.0))

    # Hover fallbacks while the tree is still filling. On a pure quad
    # grid the euclidean heuristic prunes nothing for DIAGONAL targets
    # (manhattan >> euclid: worst case ~ a full tree); for axis-aligned
    # targets it prunes almost everything. Real (organic) meshes sit in
    # between.
    axis = min(bm.verts,
               key=lambda v: (v.co - Vector((corner.co.x, far.co.y,
                                             0.0))).length)
    timeit("hover fallback: astar far, axis-aligned (best)",
           lambda: core.astar_path(bm, far, axis, mode='LENGTH'))
    timeit("hover fallback: astar far, diagonal (worst)",
           lambda: core.astar_path(bm, far, corner, mode='LENGTH'))

    # --- AUDIT: wrapper identity across calls and across undo_push ----------
    print("audit:")
    bm_a = bmesh.from_edit_mesh(me)
    bm_b = bmesh.from_edit_mesh(me)
    print("  from_edit_mesh twice, same wrapper: %r" % (bm_a is bm_b))
    bm_a.verts.ensure_lookup_table()
    v_before = bm_a.verts[0]
    bpy.ops.ed.undo_push(message="audit")
    bm_c = bmesh.from_edit_mesh(me)
    print("  wrapper survives ed.undo_push:      %r" % (bm_a is bm_c))
    bm_c.verts.ensure_lookup_table()
    print("  BMVert ref survives ed.undo_push:   %r"
          % (v_before is bm_c.verts[0]))
    print("  old wrapper still valid:            %r"
          % (getattr(bm_a, "is_valid", None),))

    print("PROFILE_DONE")


main()
