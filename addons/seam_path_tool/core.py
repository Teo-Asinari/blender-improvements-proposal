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

Array backends (1.4.0): on large meshes the whole-tree fill can instead
run at C speed. `GraphArrays` extracts the visible edge graph into flat
numpy arrays ONCE per tool session (the pure-Python extraction loop is
the expensive part — ~0.5 s at 600k edges — while per-tree solves are
cheap: only the seam flags change between trees, and those are updated
incrementally per commit). `ArrayTree` then solves one root in a single
shot, via `scipy.sparse.csgraph.dijkstra` when scipy is importable
(LENGTH and TOPOLOGY; measured ~45 ms at 300k verts vs ~750 ms for the
pure-Python slicer), or via a vectorized-numpy level-synchronous BFS for
TOPOLOGY when scipy is absent (~70–85 ms at 300k verts; numpy ships with
Blender, scipy does not). `prefer_seams` is encoded directly in the
weight array (seam edges x (1 - SEAM_TIE_BREAK) — the same discount and
the same float64 multiply as the slicer), so the strictly-shortest seam
route that erase retracing depends on is preserved by every backend; the
BFS backend gets the same guarantee from a two-tier frontier (see
`_bfs_tree`). `make_tree` picks the best available backend
(`select_tree_backend`) and every backend presents the DijkstraSlicer
advance/settled/done interface, with the slicer as the universal
fallback (no numpy, LENGTH without scipy, meshes below
ARRAY_BACKEND_MIN_VERTS). Different backends may tie-break equal-cost
paths differently — exactly the latitude the modal already grants
`astar_path` — but path COSTS always agree, and seam-discounted routes
(the erase-retrace correctness case) are strict optima on which all
backends agree.
"""

import heapq
import importlib.util
import time

try:
    import numpy as _np
except ImportError:  # numpy ships with Blender; guarded for exotic builds
    _np = None

__all__ = (
    "DijkstraSlicer",
    "GraphArrays",
    "ArrayTree",
    "make_tree",
    "select_tree_backend",
    "scipy_available",
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

    #: This backend supports genuinely bounded `advance` slices (ArrayTree
    #: does not: its solve is a single C call — see ArrayTree.advance).
    sliceable = True

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
               v_from. An `ArrayTree` (whose `.tree` property is itself)
               is accepted too and delegates to its own index-array walk.
    :return: ordered list of BMEdge from v_from to v_to,
             [] if v_from is v_to,
             None if v_to is unreachable from v_from.
    """
    if isinstance(tree, ArrayTree):
        return tree.path(v_from, v_to)
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


# ---------------------------------------------------------------------------
# Array backends (1.4.0): C-speed whole-tree fills on large meshes.
# ---------------------------------------------------------------------------

# Below this vertex count the pure-Python slicer is preferred: a full
# slicer tree at 25k verts costs ~60 ms (measured ~400 settled verts/ms
# on the reference machine) — it completes within the first few 12 ms
# background slices and even a worst-case far-hover A* fallback stays in
# imperceptible territory, so the array backends' session build cost and
# per-tree overhead buy nothing. Above it, the fill window (and the A*
# fallback ceiling) grows past ~100 ms and the C-speed solves win.
ARRAY_BACKEND_MIN_VERTS = 25000

# Conservative per-edge cost model (seconds/edge) used by
# ArrayTree.estimated_cost to decide sync-at-click vs first-TIMER-tick.
# Calibrated at ~1.5-2x the times measured on a 600k-edge grid (Blender
# 5.1.2, see tests/profile_commit.py output in the README): scipy solve
# 35 ms + csr build 8 ms; BFS 67-84 ms; GraphArrays build ~480 ms; seam
# flag extraction 69 ms.
_EST_SOLVE_PER_EDGE = {'SCIPY': 1.5e-7, 'BFS': 2.5e-7}
_EST_GRAPH_BUILD_PER_EDGE = 1.2e-6
_EST_SEAM_EXTRACT_PER_EDGE = 2.0e-7
# First `import scipy.sparse.csgraph` measured at ~0.74 s inside Blender;
# charged once per Blender session, so the first solve is always pushed
# off the click (and off any hover boost) onto a TIMER tick.
_EST_SCIPY_IMPORT = 1.5

#: Test hook: set to False to make scipy_available() report False (so the
#: no-scipy selection/fallback paths can be exercised in a Blender that
#: has scipy installed), or True to force it. None = really probe.
_scipy_override = None
_scipy_spec_cache = None   # cached find_spec probe result
_scipy_modules = None      # (csr_matrix, dijkstra) once actually imported


def scipy_available():
    """True if `scipy.sparse.csgraph` can be imported (cheap probe).

    Uses importlib.util.find_spec("scipy") so that merely ASKING costs
    microseconds; the actual (one-off, ~0.7 s) import is deferred to the
    first scipy solve. Blender's bundled Python does NOT ship scipy by
    default — everything degrades gracefully to the BFS / slicer
    backends when this returns False.
    """
    global _scipy_spec_cache
    if _scipy_override is not None:
        return _scipy_override
    if _np is None:
        return False
    if _scipy_spec_cache is None:
        try:
            _scipy_spec_cache = importlib.util.find_spec("scipy") is not None
        except Exception:
            _scipy_spec_cache = False
    return _scipy_spec_cache


def _load_scipy():
    """Import (once) and return (csr_matrix, csgraph.dijkstra)."""
    global _scipy_modules
    if _scipy_modules is None:
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import dijkstra
        _scipy_modules = (csr_matrix, dijkstra)
    return _scipy_modules


def select_tree_backend(n_verts, mode='LENGTH'):
    """Which whole-tree backend `make_tree` would use.

    :return: 'SLICER' (pure-Python resumable Dijkstra — the universal
             fallback), 'SCIPY' (scipy.sparse.csgraph.dijkstra, LENGTH
             and TOPOLOGY), or 'BFS' (vectorized-numpy level BFS,
             TOPOLOGY only — unit weights make Dijkstra a BFS).
    """
    if mode not in VALID_MODES:
        raise ValueError("mode must be one of %r, got %r" % (VALID_MODES, mode))
    if _np is None or n_verts < ARRAY_BACKEND_MIN_VERTS:
        return 'SLICER'
    if scipy_available():
        return 'SCIPY'
    if mode == 'TOPOLOGY':
        return 'BFS'
    return 'SLICER'


def make_tree(bm, v_from, mode='LENGTH', prefer_seams=False, cache=None,
              backend=None):
    """Single-source shortest-path tree on the best available backend.

    Every returned object presents the DijkstraSlicer driving interface
    (`advance` / `settled` / `done` / `.tree` usable with
    `path_from_tree`), so the modal operator drives them identically.

    :arg cache: optional dict owned by the caller (one per tool session).
        Array backends store their `GraphArrays` under cache['graph'] so
        the expensive pure-Python edge extraction is paid once per
        session and reused across anchors/trees (only seam flags change
        between trees — see GraphArrays.update_seams).
    :arg backend: force 'SLICER' / 'SCIPY' / 'BFS' (tests); default picks
        via `select_tree_backend`.
    """
    if backend is None:
        backend = select_tree_backend(len(bm.verts), mode)
    if backend == 'SLICER':
        return DijkstraSlicer(bm, v_from, mode=mode,
                              prefer_seams=prefer_seams)
    return ArrayTree(bm, v_from, mode=mode, prefer_seams=prefer_seams,
                     backend=backend, cache=cache)


class GraphArrays:
    """Flat numpy view of the VISIBLE bmesh edge graph.

    Built ONCE per tool session: the pure-Python extraction loops are the
    dominant cost (~0.5 s at 600k edges, vs ~45 ms for a scipy solve), and
    nothing they capture can change while the modal tool runs — the modal
    swallows every edit key (hide/reveal included) and never changes
    topology, so vertex positions, connectivity and hide flags are frozen
    for the session. Seam flags DO change (that is the tool's job); they
    are extracted lazily on the first prefer_seams tree and then patched
    incrementally per commit/undo via `update_seams` (O(path) instead of
    re-walking all edges).

    Edges hidden themselves or with a hidden endpoint are excluded
    entirely, matching the slicer's relaxation-time skips: hidden verts
    are simply unreachable in every array tree.

    Indexing: arrays are positional over the visible-edge subset;
    `edge_index[pos]` maps back to the bmesh edge index (assumed to equal
    its position in bm.edges, which `_reset_tree`'s
    ensure_lookup_table'd, topology-frozen bmesh guarantees — asserted
    cheaply below).
    """

    __slots__ = ("n_verts", "n_edges", "edge_index", "edge_u", "edge_v",
                 "length", "indptr", "nbr_vert", "nbr_epos", "_pos_of_edge",
                 "_seam", "build_seconds")

    def __init__(self, bm):
        if _np is None:
            raise RuntimeError("GraphArrays requires numpy")
        t0 = time.perf_counter()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        n = len(bm.verts)
        n_all = len(bm.edges)
        if n_all and (bm.edges[n_all - 1].index != n_all - 1
                      or bm.verts[n - 1].index != n - 1):
            raise ValueError("bmesh indices out of sync with element order "
                             "(call index_update() after topology edits)")
        self.n_verts = n

        # The one big pure-Python pass (measured: the flat comprehension
        # is ~1.7x faster than building per-edge tuples).
        uv = _np.array([v.index for e in bm.edges for v in e.verts],
                       dtype=_np.int64).reshape(-1, 2)
        ehide = _np.fromiter((e.hide for e in bm.edges), _np.bool_,
                             count=n_all)
        vhide = _np.fromiter((v.hide for v in bm.verts), _np.bool_, count=n)
        visible = ~(ehide | vhide[uv[:, 0]] | vhide[uv[:, 1]])

        self.edge_index = _np.flatnonzero(visible)
        self.edge_u = uv[visible, 0]
        self.edge_v = uv[visible, 1]
        m = len(self.edge_index)
        self.n_edges = m

        # Edge lengths via the SAME C call the slicer's weight function
        # uses (BMEdge.calc_length computes in mathutils' float32), so
        # LENGTH weights are bit-identical across backends. (A float64
        # numpy norm over extracted coords differs from calc_length by
        # ~1e-8 relative on curved meshes — enough to fail cross-backend
        # cost-parity tolerances — and measured SLOWER than this loop.)
        lengths_all = _np.fromiter((e.calc_length() for e in bm.edges),
                                   _np.float64, count=n_all)
        self.length = lengths_all[visible]

        # CSR-style adjacency: neighbours of vertex i sit in
        # nbr_vert[indptr[i]:indptr[i+1]], with nbr_epos giving the
        # (positional) edge used.
        src = _np.concatenate((self.edge_u, self.edge_v))
        dst = _np.concatenate((self.edge_v, self.edge_u))
        epos2 = _np.concatenate((_np.arange(m), _np.arange(m)))
        order = _np.argsort(src, kind='stable')
        self.nbr_vert = dst[order]
        self.nbr_epos = epos2[order]
        indptr = _np.zeros(n + 1, _np.int64)
        _np.cumsum(_np.bincount(src, minlength=n), out=indptr[1:])
        self.indptr = indptr

        pos = _np.full(n_all, -1, _np.int64)
        pos[self.edge_index] = _np.arange(m)
        self._pos_of_edge = pos
        self._seam = None  # lazy: first prefer_seams tree extracts it
        self.build_seconds = time.perf_counter() - t0

    @property
    def seams_extracted(self):
        return self._seam is not None

    def seam_array(self, bm):
        """Boolean seam flag per visible edge (positional); extracted
        from the bmesh on first use, then kept current via
        `update_seams`."""
        if self._seam is None:
            full = _np.fromiter((e.seam for e in bm.edges), _np.bool_,
                                count=len(bm.edges))
            self._seam = full[self.edge_index]
        return self._seam

    def update_seams(self, bm, edge_indices):
        """Patch the cached seam flags for the given bmesh edge indices
        (called by the tool after each commit/undo, whose edge lists are
        exactly the seams that changed). No-op until the seam array has
        been extracted — a later extraction reads the live flags anyway.
        """
        if self._seam is None:
            return
        bm.edges.ensure_lookup_table()
        pos_of = self._pos_of_edge
        seam = self._seam
        for ei in edge_indices:
            pos = pos_of[ei]
            if pos >= 0:
                seam[pos] = bm.edges[ei].seam

    def weights(self, bm, mode, prefer_seams):
        """float64 weight per visible edge — the array form of
        `_make_weight`: LENGTH uses 3D edge lengths, TOPOLOGY unit
        weights, and prefer_seams applies the identical
        w * (1 - SEAM_TIE_BREAK) float64 multiply to seam edges, so the
        seam-preferring tie-break (an erase-retrace CORRECTNESS feature)
        carries over to the array backends unchanged."""
        if mode not in VALID_MODES:
            raise ValueError("mode must be one of %r, got %r"
                             % (VALID_MODES, mode))
        if mode == 'LENGTH':
            w = self.length
        else:
            w = _np.ones(self.n_edges, _np.float64)
        if prefer_seams:
            seam = self.seam_array(bm)
            if mode == 'LENGTH':
                w = w.copy()
            w[seam] *= (1.0 - SEAM_TIE_BREAK)
        return w


def _bfs_tree(graph, root, seam=None):
    """Level-synchronous vectorized BFS over GraphArrays adjacency.

    With unit weights, Dijkstra IS breadth-first search, and frontier
    expansion vectorizes over the CSR arrays at O(E) total numpy work.

    prefer_seams (TOPOLOGY weights 1 and 1 - SEAM_TIE_BREAK) is handled
    by a two-tier frontier trick rather than falling back to the slicer:
    every edge weight is ~1, so each edge crosses exactly one BFS level
    and the discounted cost of a vertex settled at level k with s seam
    edges on its path is k - s*SEAM_TIE_BREAK. Since s <= k < 1e6 /
    SEAM_TIE_BREAK, cost order is exactly lexicographic (fewest edges,
    then MOST seam edges), so plain BFS levels pick k and, within a
    level, each vertex takes the predecessor maximizing the accumulated
    seam count — Dijkstra's strict optimum on the discounted weights,
    without any float accumulation at all. (This would overflow the
    lexicographic equivalence only if a path exceeded 1/SEAM_TIE_BREAK =
    1e6 edges; such meshes are far past this tool's design range.)

    :arg seam: optional bool array (positional, per visible edge); its
        presence enables the seam tier.
    :return: (level, pred_epos, seam_count) int64 arrays per vertex;
        level -1 = unreachable, pred_epos -1 = root/unreachable.
    """
    n = graph.n_verts
    indptr = graph.indptr
    nbr = graph.nbr_vert
    nbr_e = graph.nbr_epos

    level_of = _np.full(n, -1, _np.int64)
    pred = _np.full(n, -1, _np.int64)
    score = _np.zeros(n, _np.int64) if seam is not None else None
    level_of[root] = 0
    frontier = _np.array([root], _np.int64)
    level = 0
    while frontier.size:
        starts = indptr[frontier]
        counts = indptr[frontier + 1] - starts
        total = int(counts.sum())
        if total == 0:
            break
        shift = _np.concatenate(([0], _np.cumsum(counts)[:-1]))
        idx = _np.arange(total) + _np.repeat(starts - shift, counts)
        cand = nbr[idx]
        cand_e = nbr_e[idx]
        unvisited = level_of[cand] == -1
        cand = cand[unvisited]
        cand_e = cand_e[unvisited]
        if cand.size == 0:
            break
        level += 1
        if seam is None:
            # Any predecessor is a valid tie-break at equal cost.
            uniq, first = _np.unique(cand, return_index=True)
            pred_e = cand_e[first]
        else:
            srcs = _np.repeat(frontier, counts)[unvisited]
            sc = score[srcs] + seam[cand_e]
            order = _np.lexsort((sc, cand))
            cand_o = cand[order]
            # Last entry of each equal-cand run has the max seam score.
            last = _np.flatnonzero(_np.concatenate(
                (cand_o[1:] != cand_o[:-1], [True])))
            uniq = cand_o[last]
            pred_e = cand_e[order][last]
            score[uniq] = sc[order][last]
        level_of[uniq] = level
        pred[uniq] = pred_e
        frontier = uniq
    return level_of, pred, score


class ArrayTree:
    """Whole-tree single-source shortest paths at C speed ('SCIPY' or
    'BFS' backend), presenting the DijkstraSlicer driving interface.

    Unlike the slicer, the solve is NOT sliceable: it is one C call.
    `advance(time_budget=...)` therefore runs it only when the estimated
    cost fits the offered budget and otherwise does nothing (so the
    modal's small hover-boost budgets never hitch a mousemove);
    `advance()` with no budget always solves. Until solved, `settled`
    is False everywhere and the modal answers hovers via `astar_path`,
    exactly as it does inside the slicer's fill window — the window just
    shrinks from ~a second to at most one TIMER tick.

    Correctness contract (shared with the slicer): distances are plain
    float64 accumulations of the identical per-edge weights, so path
    COSTS agree with the slicer within float tolerance everywhere;
    predecessor tie-breaks among EQUAL-cost paths may differ (as
    `astar_path`'s already may — any shortest path is a valid preview,
    and a commit reuses the previewed edge list verbatim); and under
    prefer_seams the discounted seam route is a STRICT optimum, so all
    backends produce the exact marked-seam retrace that erasing depends
    on.
    """

    sliceable = False

    __slots__ = ("bm", "root", "mode", "prefer_seams", "backend", "cache",
                 "_dist", "_pred_vert", "_pred_epos", "solve_seconds")

    def __init__(self, bm, v_from, mode='LENGTH', prefer_seams=False,
                 backend='SCIPY', cache=None):
        if v_from is None:
            raise ValueError("v_from must be a BMVert")
        if mode not in VALID_MODES:
            raise ValueError("mode must be one of %r, got %r"
                             % (VALID_MODES, mode))
        if backend not in ('SCIPY', 'BFS'):
            raise ValueError("backend must be 'SCIPY' or 'BFS', got %r"
                             % (backend,))
        if backend == 'BFS' and mode != 'TOPOLOGY':
            raise ValueError("the BFS backend is TOPOLOGY-only "
                             "(unit weights)")
        if _np is None:
            raise RuntimeError("ArrayTree requires numpy")
        self.bm = bm
        self.root = v_from
        self.mode = mode
        self.prefer_seams = prefer_seams
        self.backend = backend
        self.cache = cache if cache is not None else {}
        self._dist = None
        self._pred_vert = None
        self._pred_epos = None
        self.solve_seconds = None

    # -- state -------------------------------------------------------------

    @property
    def done(self):
        """True once the whole tree has been solved."""
        return self._dist is not None

    def settled(self, v):
        """All distances become final at once, when the solve runs."""
        return self._dist is not None

    @property
    def settled_count(self):
        if self._dist is None:
            return 0
        return int((self._dist != _np.inf).sum())

    @property
    def tree(self):
        """Self; `path_from_tree` dispatches back to `self.path`."""
        return self

    def distance(self, v):
        """Cost from the root to *v*, or None (unreachable / not yet
        solved)."""
        if self._dist is None:
            return None
        d = float(self._dist[v.index])
        return None if d == float('inf') else d

    # -- driving -----------------------------------------------------------

    def estimated_cost(self):
        """Rough seconds the next `compute()` would block for, from the
        measured per-edge cost model — includes the one-off GraphArrays
        build and scipy import if they are still pending."""
        graph = self.cache.get('graph')
        n_e = graph.n_edges if graph is not None else len(self.bm.edges)
        t = n_e * _EST_SOLVE_PER_EDGE[self.backend]
        if graph is None:
            t += n_e * _EST_GRAPH_BUILD_PER_EDGE
        if self.prefer_seams and (graph is None or not graph.seams_extracted):
            t += n_e * _EST_SEAM_EXTRACT_PER_EDGE
        if self.backend == 'SCIPY' and _scipy_modules is None:
            t += _EST_SCIPY_IMPORT
        return t

    def advance(self, max_verts=None, time_budget=None, stop_at=None):
        """Slicer-compatible driver. The solve is all-or-nothing: with a
        time_budget smaller than the estimated solve cost this does
        NOTHING and returns False (the caller keeps its TIMER running and
        answers hovers via astar_path meanwhile); without a budget — or
        with one the estimate fits — it solves completely.

        max_verts / stop_at are accepted for signature compatibility;
        they cannot bound a one-shot C solve and are treated as "no
        budget given".
        """
        if self.done:
            return True
        if time_budget is not None and self.estimated_cost() > time_budget:
            return False
        self.compute()
        return True

    def ensure_settled(self, v):
        """Solve (if needed); True if *v* is reachable."""
        self.compute()
        return self._dist[v.index] != _np.inf

    def compute(self):
        """Run the whole-tree solve now (idempotent)."""
        if self.done:
            return
        t0 = time.perf_counter()
        graph = self.cache.get('graph')
        if graph is None:
            graph = GraphArrays(self.bm)
            self.cache['graph'] = graph
        root = self.root.index
        if self.backend == 'SCIPY':
            csr_matrix, sp_dijkstra = _load_scipy()
            w = graph.weights(self.bm, self.mode, self.prefer_seams)
            G = csr_matrix((w, (graph.edge_u, graph.edge_v)),
                           shape=(graph.n_verts, graph.n_verts))
            dist, pred = sp_dijkstra(G, directed=False, indices=root,
                                     return_predecessors=True)
            self._dist = dist
            self._pred_vert = pred
        else:  # BFS (TOPOLOGY)
            seam = graph.seam_array(self.bm) if self.prefer_seams else None
            level, pred_e, score = _bfs_tree(graph, root, seam=seam)
            dist = level.astype(_np.float64)
            dist[level < 0] = _np.inf
            if score is not None:
                # cost = k - s*SEAM_TIE_BREAK (see _bfs_tree); float64
                # noise vs the slicer's sequential sums is ~1e-16
                # relative, far below the 1e-6 discount.
                dist = dist - score * SEAM_TIE_BREAK
                dist[level < 0] = _np.inf
            self._dist = dist
            self._pred_epos = pred_e
        self.solve_seconds = time.perf_counter() - t0

    # -- queries -----------------------------------------------------------

    def _edge_pos_between(self, v_idx, prev_idx):
        """Positional edge index connecting two vertex indices (bmesh has
        no parallel edges, so the pair is unambiguous)."""
        graph = self.cache['graph']
        lo = graph.indptr[v_idx]
        hi = graph.indptr[v_idx + 1]
        for k in range(lo, hi):
            if graph.nbr_vert[k] == prev_idx:
                return int(graph.nbr_epos[k])
        raise ValueError("predecessor %d not adjacent to %d"
                         % (prev_idx, v_idx))

    def path(self, v_from, v_to):
        """Ordered BMEdge list root -> v_to (path_from_tree semantics):
        [] if v_from is v_to, None if unreachable (or not yet solved).
        """
        if v_from is not self.root and v_from.index != self.root.index:
            raise ValueError("path must start at the tree root")
        if v_from is v_to:
            return []
        if self._dist is None or self._dist[v_to.index] == _np.inf:
            return None
        graph = self.cache['graph']
        bm_edges = self.bm.edges
        edge_index = graph.edge_index
        root = self.root.index
        edges = []
        cur = v_to.index
        if self._pred_epos is not None:      # BFS: predecessor EDGE known
            pred_e = self._pred_epos
            eu, ev = graph.edge_u, graph.edge_v
            while cur != root:
                epos = int(pred_e[cur])
                edges.append(bm_edges[int(edge_index[epos])])
                u = int(eu[epos])
                cur = u if u != cur else int(ev[epos])
        else:                                # scipy: predecessor VERTEX
            pred_v = self._pred_vert
            while cur != root:
                prev = int(pred_v[cur])
                epos = self._edge_pos_between(cur, prev)
                edges.append(bm_edges[int(edge_index[epos])])
                cur = prev
        edges.reverse()
        return edges
