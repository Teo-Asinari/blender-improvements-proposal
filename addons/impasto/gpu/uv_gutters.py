"""Pure planning primitives for safe UV-island gutter padding.

This module intentionally performs no painting.  It defines the ownership
contract required by the later GPU pass: a gutter texel may copy a complete
resident texel only from the nearest triangle belonging to its assigned UV
island.  It must never infer ownership from channel alpha (erase and opaque
scalar canvases make alpha unsuitable).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GutterPlan:
    """Inputs shared by ownership-map construction and stroke finalization."""

    triangle_islands: tuple
    padding_px: int
    canvas_size: int

    def expanded_rect(self, rect):
        return expand_pixel_rect(rect, self.padding_px, self.canvas_size)


def expand_pixel_rect(rect, padding, canvas_size):
    """Expand ``(x, y, width, height)`` and clip it to a square canvas."""
    if rect is None:
        return None
    x, y, width, height = (int(value) for value in rect)
    padding = max(0, int(padding))
    size = max(0, int(canvas_size))
    x0 = max(0, x - padding)
    y0 = max(0, y - padding)
    x1 = min(size, x + max(0, width) + padding)
    y1 = min(size, y + max(0, height) + padding)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def triangle_uv_islands(vertex_triangles, uv_triangles, tolerance=1e-7):
    """Return a stable island id for every loop triangle.

    Triangles join only across the same mesh edge when both UV endpoints also
    agree (in either direction).  Merely coincident UV coordinates do not join
    disconnected geometry, and a marked/implicit UV seam remains separated.

    ``vertex_triangles`` is an iterable of three mesh vertex ids;
    ``uv_triangles`` contains the corresponding three two-component UVs.
    """
    vertices = [tuple(int(v) for v in tri) for tri in vertex_triangles]
    uvs = [tuple((float(uv[0]), float(uv[1])) for uv in tri)
           for tri in uv_triangles]
    if len(vertices) != len(uvs):
        raise ValueError("vertex and UV triangle counts differ")
    if any(len(tri) != 3 for tri in vertices) or any(len(tri) != 3 for tri in uvs):
        raise ValueError("loop triangles must contain exactly three corners")

    parent = list(range(len(vertices)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    def close(a, b):
        return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance

    edges = {}
    for tri_index, (tri_vertices, tri_uvs) in enumerate(zip(vertices, uvs)):
        for corner in range(3):
            nxt = (corner + 1) % 3
            va, vb = tri_vertices[corner], tri_vertices[nxt]
            ua, ub = tri_uvs[corner], tri_uvs[nxt]
            key = (min(va, vb), max(va, vb))
            oriented = (ua, ub) if va <= vb else (ub, ua)
            for other_index, other_uvs in edges.get(key, ()):
                if close(oriented[0], other_uvs[0]) and close(oriented[1], other_uvs[1]):
                    union(tri_index, other_index)
            edges.setdefault(key, []).append((tri_index, oriented))

    stable_ids = {}
    result = []
    for index in range(len(vertices)):
        root = find(index)
        if root not in stable_ids:
            stable_ids[root] = len(stable_ids)
        result.append(stable_ids[root])
    return tuple(result)


def build_gutter_plan(vertex_triangles, uv_triangles, padding_px, canvas_size):
    """Build the CPU half of the future GPU ownership-map contract."""
    if int(padding_px) < 0:
        raise ValueError("padding_px must be non-negative")
    if int(canvas_size) <= 0:
        raise ValueError("canvas_size must be positive")
    return GutterPlan(
        triangle_uv_islands(vertex_triangles, uv_triangles),
        int(padding_px), int(canvas_size))

