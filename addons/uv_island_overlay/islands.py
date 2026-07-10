# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure UV-island logic: island computation and color assignment.

No bpy/gpu imports here — everything operates on a BMesh handed in by the
caller (duck-typed; only loop/edge/face attribute access is used), so this
module is importable and fully testable in `blender --background`.
"""

import colorsys

# Golden-ratio conjugate: stepping the hue by this per island gives a
# low-discrepancy sequence — consecutive islands land far apart on the
# hue wheel, and the sequence never exactly repeats.
_GOLDEN = 0.6180339887498949

# Two UV coordinates closer than this are considered "the same point".
# Loose enough to absorb float32 storage rounding, tight enough that no
# real unwrap places distinct islands within it.
DEFAULT_EPSILON = 1e-5


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------

class _UnionFind:
    __slots__ = ("parent", "rank")

    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:          # path compression
            parent[x], x = root, parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _groups_to_islands(uf, n_faces):
    """Collect union-find components into face-index sets, ordered stably
    by each island's minimum face index (so island ids — and therefore
    colors — do not shuffle between recomputes of the same mesh)."""
    groups = {}
    for i in range(n_faces):
        groups.setdefault(uf.find(i), set()).add(i)
    return sorted(groups.values(), key=min)


# ---------------------------------------------------------------------------
# Island computation
# ---------------------------------------------------------------------------

def _edge_uvs_match(l1, l2, uv_layer, eps):
    """True if two loops of the same mesh edge (in different faces) carry
    matching UVs at both endpoints — i.e. the edge is NOT a UV seam in
    practice. Each loop `l` has l.edge == the shared edge; the UV at
    l.vert is l[uv], and the UV at the edge's other vert is on the loop's
    successor within its face."""
    a1 = l1[uv_layer].uv
    b1 = l1.link_loop_next[uv_layer].uv
    a2 = l2[uv_layer].uv
    b2 = l2.link_loop_next[uv_layer].uv
    if l1.vert == l2.vert:
        # Same winding direction (non-manifold or flipped face).
        return ((a1 - a2).length < eps and (b1 - b2).length < eps)
    # Normal manifold case: opposite winding, endpoints swap.
    return ((a1 - b2).length < eps and (b1 - a2).length < eps)


def compute_islands(bm, uv_layer, epsilon=DEFAULT_EPSILON):
    """UV islands as a list of face-index sets, by TRUE UV-space
    connectivity: two faces sharing a mesh edge are in the same island
    iff that edge's loop UVs coincide on both sides (within epsilon).

    This follows the actual unwrap result, not seam flags — islands split
    by Smart UV Project, lightmap pack, or manual UV edits are detected
    correctly even when no edge is flagged as a seam.
    """
    n = len(bm.faces)
    if n == 0:
        return []
    bm.faces.ensure_lookup_table()
    if bm.faces[0].index == -1 or (n > 1 and bm.faces[n - 1].index == -1):
        bm.faces.index_update()

    uf = _UnionFind(n)
    for edge in bm.edges:
        loops = edge.link_loops
        if len(loops) < 2:
            continue
        # Pairwise over the edge's loops handles non-manifold edges too;
        # len(loops) is 2 for manifold meshes so this is O(1) per edge.
        for i in range(len(loops)):
            fi = loops[i].face.index
            for j in range(i + 1, len(loops)):
                fj = loops[j].face.index
                if fi == fj:
                    continue
                if _edge_uvs_match(loops[i], loops[j], uv_layer, epsilon):
                    uf.union(fi, fj)
    return _groups_to_islands(uf, n)


def compute_islands_by_seam(bm):
    """Seam-predicted islands: connected components of faces, treating
    seam-flagged edges as island boundaries (boundary/non-manifold edges
    behave exactly as their link_faces dictate: a boundary edge joins
    nothing, a non-manifold edge joins all its faces unless seamed).

    This is what the SEAM ("Seams (predicted)") overlay source shows: the
    partition the next seam-respecting Unwrap will produce, computable
    live while marking seams — no UV data needed. Also the fallback for
    meshes with no UV layer. The bmesh reference implementation; overlay
    uses the vectorized compute_islands_by_seam_arrays below (identical
    output, ~10x faster on large meshes)."""
    n = len(bm.faces)
    if n == 0:
        return []
    bm.faces.ensure_lookup_table()
    if bm.faces[0].index == -1 or (n > 1 and bm.faces[n - 1].index == -1):
        bm.faces.index_update()

    uf = _UnionFind(n)
    for edge in bm.edges:
        if edge.seam:
            continue
        faces = edge.link_faces
        if len(faces) < 2:
            continue
        first = faces[0].index
        for f in faces[1:]:
            uf.union(first, f.index)
    return _groups_to_islands(uf, n)


def compute_islands_by_seam_arrays(n_faces, loop_edge_index,
                                   loop_face_index, edge_seam):
    """Vectorized twin of compute_islands_by_seam, operating on flat
    arrays (as produced by ``Mesh.foreach_get`` — no bmesh needed):

    - loop_edge_index: per-loop edge index (``Mesh.loops`` "edge_index")
    - loop_face_index: per-loop owning-face index (loops are stored
      contiguously per polygon, so ``np.repeat(arange(n_faces),
      loop_total)`` produces this)
    - edge_seam:       per-edge seam flag (``Mesh.edges`` "use_seam")

    Returns ``(face_to_island, n_islands)`` where face_to_island is an
    int array of length n_faces mapping face index -> island id, with
    islands ordered by their minimum face index — the exact ordering
    _groups_to_islands produces, so colors agree between the two
    implementations (and stay stable across recomputes).

    Semantics match compute_islands_by_seam exactly: faces sharing a
    non-seam edge are joined (ALL faces around a non-manifold edge are
    chained together), boundary edges (single loop) join nothing.

    Algorithm: min-label propagation with pointer jumping — a handful of
    whole-array numpy passes instead of a Python loop over every edge.
    Measured on a 302k-face grid (Blender 5.1.2): ~0.06 s vs ~0.57 s for
    the pure-Python union-find above.
    """
    import numpy as np

    n_faces = int(n_faces)
    if n_faces == 0:
        return np.empty(0, dtype=np.int64), 0
    loop_edge_index = np.asarray(loop_edge_index, dtype=np.int64)
    loop_face_index = np.asarray(loop_face_index, dtype=np.int64)
    edge_seam = np.asarray(edge_seam, dtype=bool)

    # Sort loops by edge index: loops sharing an edge become consecutive,
    # and consecutive pairs (a,b), (b,c), ... chain-union all faces around
    # the edge — the same closure the pairwise union-find loop computes,
    # non-manifold edges included. Boundary edges (one loop) yield no pair.
    order = np.argsort(loop_edge_index, kind='stable')
    se = loop_edge_index[order]
    sf = loop_face_index[order]
    same_edge = se[1:] == se[:-1]
    pair_mask = same_edge & ~edge_seam[se[1:]]
    a = sf[:-1][pair_mask]
    b = sf[1:][pair_mask]
    keep = a != b
    a = a[keep]
    b = b[keep]

    # Min-label propagation: hook the larger label onto the smaller, then
    # compress paths (label = label[label]) to a fixed point. Labels only
    # ever decrease and the minimum face index of a component keeps its
    # own label, so at convergence every face carries its component's
    # minimum face index. Terminates: each changed round strictly
    # decreases the (integer, bounded-below) label sum. ~6 rounds at 302k.
    label = np.arange(n_faces, dtype=np.int64)
    while True:
        la = label[a]
        lb = label[b]
        lo = np.minimum(la, lb)
        hi = np.maximum(la, lb)
        upd = hi != lo
        changed = bool(upd.any())
        if changed:
            np.minimum.at(label, hi[upd], lo[upd])
        while True:
            nxt = label[label]
            if np.array_equal(nxt, label):
                break
            label = nxt
        if not changed:
            break

    # Roots are component-minimum face indices; np.unique returns them
    # ascending, so island ids are ordered by minimum face index.
    roots, face_to_island = np.unique(label, return_inverse=True)
    return face_to_island, int(len(roots))


def islands_from_face_mapping(mapping):
    """Inverse of face_index_to_island: group a face->island-id mapping
    back into a list of face-index sets ordered by island id (pure
    Python; handy for comparing partitions in tests)."""
    groups = {}
    for face, island_id in enumerate(mapping):
        groups.setdefault(int(island_id), set()).add(face)
    return [groups[i] for i in sorted(groups)]


# ---------------------------------------------------------------------------
# Colors / lookup tables
# ---------------------------------------------------------------------------

def island_colors(n, seed=0, saturation=0.6, value=0.95, alpha=1.0):
    """n visually distinct RGBA colors via golden-ratio hue stepping.

    Deterministic: the same (n, seed) always yields the same list, and
    color i does not depend on n — recomputing a mesh whose island count
    grew keeps the existing islands' colors. Moderate saturation/value so
    wireframes and selection highlights stay readable through the tint.
    """
    colors = []
    for i in range(n):
        hue = (seed * _GOLDEN + i * _GOLDEN) % 1.0
        # Nudge value on a 3-cycle so near-hue collisions at high n still
        # separate visually.
        v = value - 0.12 * (i % 3)
        r, g, b = colorsys.hsv_to_rgb(hue, saturation, v)
        colors.append((r, g, b, alpha))
    return colors


def face_index_to_island(islands):
    """Flat lookup list: face index -> island id. Length is the total
    face count; every face must appear in exactly one island (as produced
    by the compute_* functions above)."""
    total = sum(len(s) for s in islands)
    mapping = [-1] * total
    for island_id, faces in enumerate(islands):
        for f in faces:
            mapping[f] = island_id
    return mapping
