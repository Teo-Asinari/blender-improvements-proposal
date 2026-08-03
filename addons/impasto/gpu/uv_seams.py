"""Pure mesh-topology to UV-seam correspondence planning.

The GPU painter works in UV space, but paint continuity is defined by mesh
topology.  This module pairs the two UV representations of every manifold
mesh edge so a later raster pass can transfer texels across distant islands.
It deliberately performs no Blender or GPU work.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class UVHalfEdge:
    triangle: int
    corner: int
    mesh_edge: tuple
    # UVs are ordered by the canonical (lowest id first) mesh-edge vertices.
    uv0: tuple
    uv1: tuple


@dataclass(frozen=True)
class UVSeamPair:
    mesh_edge: tuple
    first: UVHalfEdge
    second: UVHalfEdge


@dataclass(frozen=True)
class UVSeamDiagnostics:
    mesh_edges: int
    seam_edges: int
    continuous_edges: int
    boundary_edges: tuple
    non_manifold_edges: tuple
    degenerate_edges: tuple
    overlapping_uv_edges: tuple


@dataclass(frozen=True)
class UVSeamCorrespondence:
    pairs: tuple
    diagnostics: UVSeamDiagnostics


def _uv_close(a, b, tolerance):
    return (abs(a[0] - b[0]) <= tolerance
            and abs(a[1] - b[1]) <= tolerance)


def _quantized_uv(value, tolerance):
    # A tolerance-sized grid makes the overlap diagnostic stable in the face
    # of harmless float noise.  Pair classification still uses _uv_close.
    return tuple(round(float(component) / tolerance) for component in value)


def build_seam_correspondence(vertex_triangles, uv_triangles,
                              tolerance=1e-7):
    """Pair manifold mesh edges whose two faces use different UV segments.

    Each returned half-edge stores its UV endpoints in canonical mesh-vertex
    order.  Thus ``first.uv0`` and ``second.uv0`` describe the same 3D vertex
    even when triangle winding differs.  Boundary and non-manifold edges are
    reported rather than guessed.  Coincident UV segments belonging to
    different mesh edges are also reported because their ownership is
    ambiguous for a seam-transfer rasterizer.
    """
    tolerance = float(tolerance)
    if tolerance <= 0.0:
        raise ValueError("tolerance must be positive")
    vertices = [tuple(int(value) for value in tri)
                for tri in vertex_triangles]
    uvs = [tuple((float(uv[0]), float(uv[1])) for uv in tri)
           for tri in uv_triangles]
    if len(vertices) != len(uvs):
        raise ValueError("vertex and UV triangle counts differ")
    if any(len(tri) != 3 for tri in vertices) or any(
            len(tri) != 3 for tri in uvs):
        raise ValueError("loop triangles must contain exactly three corners")

    by_mesh_edge = {}
    degenerate = set()
    by_uv_segment = {}
    for triangle, (tri_vertices, tri_uvs) in enumerate(zip(vertices, uvs)):
        for corner in range(3):
            nxt = (corner + 1) % 3
            va, vb = tri_vertices[corner], tri_vertices[nxt]
            if va == vb:
                degenerate.add((va, vb))
                continue
            mesh_edge = (min(va, vb), max(va, vb))
            ua, ub = tri_uvs[corner], tri_uvs[nxt]
            uv0, uv1 = (ua, ub) if va < vb else (ub, ua)
            half_edge = UVHalfEdge(
                triangle, corner, mesh_edge, uv0, uv1)
            by_mesh_edge.setdefault(mesh_edge, []).append(half_edge)
            uv_key = tuple(sorted((_quantized_uv(uv0, tolerance),
                                   _quantized_uv(uv1, tolerance))))
            by_uv_segment.setdefault(uv_key, set()).add(mesh_edge)

    pairs = []
    continuous = 0
    boundaries = []
    non_manifold = []
    for mesh_edge in sorted(by_mesh_edge):
        half_edges = by_mesh_edge[mesh_edge]
        if len(half_edges) == 1:
            boundaries.append(mesh_edge)
        elif len(half_edges) != 2:
            non_manifold.append(mesh_edge)
        else:
            first, second = half_edges
            if (_uv_close(first.uv0, second.uv0, tolerance)
                    and _uv_close(first.uv1, second.uv1, tolerance)):
                continuous += 1
            else:
                pairs.append(UVSeamPair(mesh_edge, first, second))

    overlaps = tuple(sorted(
        tuple(sorted(mesh_edges))
        for mesh_edges in by_uv_segment.values() if len(mesh_edges) > 1))
    diagnostics = UVSeamDiagnostics(
        mesh_edges=len(by_mesh_edge), seam_edges=len(pairs),
        continuous_edges=continuous, boundary_edges=tuple(boundaries),
        non_manifold_edges=tuple(non_manifold),
        degenerate_edges=tuple(sorted(degenerate)),
        overlapping_uv_edges=overlaps)
    return UVSeamCorrespondence(tuple(pairs), diagnostics)
