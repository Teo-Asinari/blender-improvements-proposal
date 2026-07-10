# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure path-finding / seam-marking logic over a bmesh.

No bpy / UI imports here (only bmesh-level data is touched), so everything
in this module is fully testable in a headless Blender (`--background`).

Design note: the modal tool needs a live preview of the candidate path on
every mouse move. Rather than running a two-point search per mousemove, we
run a SINGLE-SOURCE Dijkstra per committed anchor, which yields the full
predecessor tree from that anchor. Each preview is then just a
predecessor-tree walk from the hovered vertex (`path_from_tree`) —
O(path length), no pathfinding.

Performance design (1.3.0): on large meshes (~300k verts) the full tree
costs ~0.7 s in pure Python, which used to run synchronously inside the
click handler and made every commit hitch. The tree construction is now
RESUMABLE (`DijkstraSlicer`): the heap loop can be advanced in bounded
slices (vertex-count or wall-time budgets), so the modal operator runs it
in ~10 ms increments on TIMER events while the UI stays responsive.

  - A slice boundary never changes the result: pausing only suspends the
    pop loop, and a vertex's outgoing edges are always relaxed in the same
    step that settles it, so the resumed run continues the exact same heap
    sequence. A slicer driven to completion produces a tree IDENTICAL to a
    one-shot run (`dijkstra_tree` is literally a one-shot slicer).
  - Once a vertex is SETTLED (popped), its distance and predecessor are
    final — hovers over the settled region can be answered from the
    partial tree immediately, with the same result the full tree gives.
  - `ensure_settled(v)` is the early-exit form: it advances the same heap
    until *v* settles, so an early-exit query returns the identical path
    (including tie-breaking) that the full tree would, while settling only
    the vertices cheaper-or-equal to the target.
  - Hovers outside the settled region can instead use `astar_path`, an
    independent two-point query with an admissible euclidean lower-bound
    heuristic in LENGTH mode (scaled by (1 - SEAM_TIE_BREAK) when seams
    are preferred, keeping it admissible for the discounted weights; plain
    early-exit Dijkstra in TOPOLOGY mode, where no geometric lower bound
    exists). A* guarantees an optimal-cost path but may tie-break
    differently from the Dijkstra tree among equal-cost paths; the modal
    keeps preview == commit by committing exactly the previewed edge list,
    so this only ever affects which of several equally-short previews is
    shown.
"""

import heapq
import time

__all__ = (
    "DijkstraSlicer",
    "dijkstra_tree",
    "path_from_tree",
    "astar_path",
    "shortest_path",
    "path_length",
    "mark_seam_path",
    "apply_seams",
)

VALID_MODES = {'LENGTH', 'TOPOLOGY'}

# Relative weight discount for edges that are already seams when
# `prefer_seams` is enabled. Small enough to never make a meaningfully
# longer route win (it can only flip exact or near-exact cost ties), large
# enough to dominate float noise. See `DijkstraSlicer` for why this exists.
SEAM_TIE_BREAK = 1e-6

# When advancing under a time budget, the clock is checked every this many
# settled vertices (a perf_counter call per pop would be measurable at
# ~400 settled verts/ms).
_TIME_CHECK_EVERY = 64


def _make_weight(mode, prefer_seams):
    """Edge-weight function for the given mode / seam preference."""
    if mode not in VALID_MODES:
        raise ValueError("mode must be one of %r, got %r" % (VALID_MODES, mode))
    if mode == 'LENGTH':
        def base_weight(edge):
            return edge.calc_length()
    else:  # TOPOLOGY
        def base_weight(edge):
            return 1.0
    if not prefer_seams:
        return base_weight

    def weight(edge):
        w = base_weight(edge)
        return w * (1.0 - SEAM_TIE_BREAK) if edge.seam else w
    return weight


class DijkstraSlicer:
    """Resumable single-source Dijkstra over the bmesh edge graph.

    Runs the classic heap loop rooted at *v_from*, but the loop can be
    paused and resumed at any vertex-settle boundary via `advance`
    budgets. The visible state (`dist`, `prev_edge`, `settled`) at any
    pause point is a valid PARTIAL shortest-path tree: every settled
    vertex's distance and predecessor edge are final and identical to what
    a full run produces. Driving the slicer to completion yields exactly
    the same tree as a one-shot run — slice boundaries cannot reorder the
    heap.

    :arg bm: the BMesh (traversal walks vert.link_edges).
    :arg v_from: root BMVert.
    :arg mode: 'LENGTH' — weight edges by their 3D length (default);
               'TOPOLOGY' — unit weight per edge (fewest edges wins).
    :arg prefer_seams: discount edges that are currently seams by the tiny
        relative factor SEAM_TIE_BREAK, so that among equal-cost paths the
        one running along existing seams always wins. Erase gestures need
        this: shortest paths are not unique (any quad grid is full of
        equal-cost "staircases"), and Dijkstra's tie-breaking depends on
        the tree root, so a path recomputed from the opposite end can be a
        *different* equal-length path than the one that was marked —
        clearing the wrong edges and leaving the seam in place. With
        seam-preference on, a fully-seamed optimal path has strictly
        minimal discounted cost, so retracing a marked segment erases
        exactly its edges. The discount only ever flips (near-)ties; it
        cannot make a longer detour win.

    NOTE: weights are sampled when an edge is relaxed, so seam flags (for
    prefer_seams) and topology must not change between `advance` calls; the
    modal tool discards the slicer on any commit/undo, which is when flags
    change.

    :ivar dist: {BMVert: cost from v_from} — final for settled verts,
        best-so-far (upper bound) for frontier verts.
    :ivar prev_edge: {BMVert: BMEdge used to reach it} — same caveat.
    """

    __slots__ = ("bm", "root", "mode", "prefer_seams", "dist", "prev_edge",
                 "_weight", "_visited", "_heap", "_counter")

    def __init__(self, bm, v_from, mode='LENGTH', prefer_seams=False):
        if v_from is None:
            raise ValueError("v_from must be a BMVert")
        self.bm = bm
        self.root = v_from
        self.mode = mode
        self.prefer_seams = prefer_seams
        self._weight = _make_weight(mode, prefer_seams)
        # BMVerts are hashable, so we key dicts on them directly. Heap
        # entries carry a monotonically increasing tie-breaker because
        # BMVerts are not orderable.
        self.dist = {v_from: 0.0}
        self.prev_edge = {}
        self._visited = set()
        self._heap = [(0.0, 0, v_from)]
        self._counter = 0

    # -- state ---------------------------------------------------------------

    @property
    def done(self):
        """True once every reachable vertex has been settled."""
        return not self._heap

    @property
    def settled_count(self):
        """Number of vertices settled so far (final dist/prev)."""
        return len(self._visited)

    def settled(self, v):
        """True if *v*'s distance and predecessor are final."""
        return v in self._visited

    @property
    def tree(self):
        """(dist, prev_edge) view compatible with `path_from_tree`.

        Only paths to SETTLED vertices are guaranteed shortest; check
        `settled(v)` (or `done`) before walking to v.
        """
        return self.dist, self.prev_edge

    # -- the loop --------------------------------------------------------------

    def advance(self, max_verts=None, time_budget=None, stop_at=None):
        """Run the heap loop until a budget is hit or the tree completes.

        :arg max_verts: settle at most this many additional vertices.
        :arg time_budget: run for at most this many seconds (checked every
            few settles, so slight overshoot is possible).
        :arg stop_at: BMVert — pause right after this vertex settles (its
            outgoing edges ARE relaxed first, so resuming stays exact).
        :return: `self.done` (True when the whole tree is finished).
        """
        heap = self._heap
        visited = self._visited
        dist = self.dist
        prev_edge = self.prev_edge
        weight = self._weight
        counter = self._counter

        budget = max_verts
        deadline = None
        if time_budget is not None:
            deadline = time.perf_counter() + time_budget
        since_check = 0

        while heap:
            d, _, v = heapq.heappop(heap)
            if v in visited:
                continue
            visited.add(v)
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
            if v is stop_at:
                break
            if budget is not None:
                budget -= 1
                if budget <= 0:
                    break
            if deadline is not None:
                since_check += 1
                if since_check >= _TIME_CHECK_EVERY:
                    since_check = 0
                    if time.perf_counter() >= deadline:
                        break

        self._counter = counter
        return not heap

    def ensure_settled(self, v):
        """Early-exit query: advance until *v* is settled.

        Because this only continues the same heap sequence, the resulting
        path to *v* is IDENTICAL (edges and tie-breaking) to the one the
        completed tree would give — it just stops as soon as *v*'s entry
        is final.

        :return: True if *v* is settled (reachable), False if the graph
            was exhausted without reaching it (other component / hidden).
        """
        if v in self._visited:
            return True
        self.advance(stop_at=v)
        return v in self._visited


def dijkstra_tree(bm, v_from, mode='LENGTH', prefer_seams=False):
    """Single-source Dijkstra over the bmesh edge graph (one-shot).

    Builds the complete shortest-path tree rooted at *v_from* by driving a
    `DijkstraSlicer` to completion in one call, so any number of
    destination queries can be answered afterwards with `path_from_tree`
    at O(path length) cost. See `DijkstraSlicer` for the argument
    semantics (including `prefer_seams`).

    :return: (dist, prev_edge) where
             dist is {BMVert: cost from v_from} and
             prev_edge is {BMVert: BMEdge used to reach it}.
             Vertices absent from dist are unreachable (other component
             or hidden).
    """
    slicer = DijkstraSlicer(bm, v_from, mode=mode, prefer_seams=prefer_seams)
    slicer.advance()
    return slicer.dist, slicer.prev_edge


def path_from_tree(tree, v_from, v_to):
    """Extract the path v_from -> v_to from a Dijkstra tree.

    :arg tree: the (dist, prev_edge) tuple returned by `dijkstra_tree` (or
               a `DijkstraSlicer.tree` whose v_to is settled), rooted at
               v_from.
    :return: ordered list of BMEdge from v_from to v_to,
             [] if v_from is v_to,
             None if v_to is unreachable from v_from.
    """
    dist, prev_edge = tree
    if v_from is v_to:
        return []
    if v_to not in prev_edge:
        return None
    edges = []
    v = v_to
    while v is not v_from:
        e = prev_edge[v]
        edges.append(e)
        v = e.other_vert(v)
    edges.reverse()
    return edges


def astar_path(bm, v_from, v_to, mode='LENGTH', prefer_seams=False):
    """Early-exit two-point query: A* in LENGTH mode, Dijkstra otherwise.

    Used by the modal tool for hovers that land OUTSIDE the region the
    background `DijkstraSlicer` has settled so far: instead of forcing the
    whole tree, this answers one target directly.

    - LENGTH mode: A* with the euclidean straight-line distance as
      heuristic — an admissible, consistent lower bound on the remaining
      path length. With `prefer_seams`, the heuristic is scaled by
      (1 - SEAM_TIE_BREAK) so it also lower-bounds the discounted weights
      (each edge costs at least (1 - SEAM_TIE_BREAK) * its length).
    - TOPOLOGY mode: no geometric lower bound relates edge counts to
      positions, so the heuristic is 0 and this degenerates to an
      early-exit Dijkstra (identical tie-breaking to the full tree).

    The returned path always has optimal (discounted) cost. In LENGTH
    mode, among multiple equal-cost shortest paths A* may pick a different
    one than the Dijkstra tree's tie-break; with `prefer_seams`, a fully
    seamed optimal route is a STRICT optimum, so erase retracing is exact
    here too.

    :return: ordered list of BMEdge from v_from to v_to,
             [] if v_from is v_to,
             None if v_to is unreachable.
    """
    if v_from is None or v_to is None:
        raise ValueError("v_from and v_to must be BMVerts")
    weight = _make_weight(mode, prefer_seams)  # validates mode
    if v_from is v_to:
        return []

    if mode == 'LENGTH':
        goal_co = v_to.co
        hscale = (1.0 - SEAM_TIE_BREAK) if prefer_seams else 1.0

        def heuristic(v):
            return (v.co - goal_co).length * hscale
    else:  # TOPOLOGY
        def heuristic(v):
            return 0.0

    dist = {v_from: 0.0}
    prev_edge = {}
    visited = set()
    counter = 0
    heap = [(heuristic(v_from), 0, v_from)]

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
            nd = d + weight(e)
            if nd < dist.get(other, float('inf')):
                dist[other] = nd
                prev_edge[other] = e
                counter += 1
                heapq.heappush(heap, (nd + heuristic(other), counter, other))

    if v_to not in visited:
        return None
    return path_from_tree((dist, prev_edge), v_from, v_to)


def shortest_path(bm, v_from, v_to, mode='LENGTH', prefer_seams=False):
    """Shortest path between two BMVerts over the edge graph.

    Convenience wrapper: early-exit Dijkstra from v_from (`ensure_settled`
    on a slicer), which returns exactly the path the full tree would —
    same relaxation code, same tie-breaking — while settling only the
    vertices at most as costly as v_to.

    :arg prefer_seams: tie-break equal-cost paths toward existing seam
        edges (see `DijkstraSlicer`).
    :return: list of BMEdge from v_from to v_to (in order),
             [] if v_from is v_to,
             None if the vertices are in disconnected components.
    """
    if v_from is None or v_to is None:
        raise ValueError("v_from and v_to must be BMVerts")
    if v_from is v_to:
        return []
    slicer = DijkstraSlicer(bm, v_from, mode=mode, prefer_seams=prefer_seams)
    if not slicer.ensure_settled(v_to):
        return None
    return path_from_tree(slicer.tree, v_from, v_to)


def path_length(edges):
    """Total 3D length of a list of BMEdges."""
    return sum(e.calc_length() for e in edges)


def apply_seams(edges, clear=False):
    """Set (or clear) ``edge.seam`` on a list of BMEdges.

    :return: list of (edge, previous_seam_state) pairs, so the caller can
             restore prior state (segment undo in the modal tool).
    """
    state = not clear
    prior = [(e, e.seam) for e in edges]
    for e in edges:
        e.seam = state
    return prior


def mark_seam_path(bm, v_from, v_to, mode='LENGTH', clear=False):
    """Mark (or clear) UV seams along the shortest path between two verts.

    :arg clear: when True, unset ``edge.seam`` instead of setting it
                (eraser behaviour). Clearing also enables seam-preferring
                tie-breaking (see `DijkstraSlicer`), so clearing between
                the endpoints of a previously marked path removes exactly
                that path even when equal-cost alternatives exist.
    :return: the list of BMEdges the seam state was applied to,
             [] for a zero-length path, or None if disconnected.
    """
    edges = shortest_path(bm, v_from, v_to, mode=mode, prefer_seams=clear)
    if edges is None:
        return None
    apply_seams(edges, clear=clear)
    return edges
