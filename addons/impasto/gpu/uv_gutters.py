"""Pure planning primitives for safe UV-island gutter padding.

This module intentionally performs no painting.  It defines the ownership
contract required by the later GPU pass: a gutter texel may copy a complete
resident texel only from its deterministic, geometrically assigned source.
It must never infer ownership from channel alpha (erase and opaque scalar
canvases make alpha unsuitable).
"""

from dataclasses import dataclass
import time


DEFAULT_PADDING_PX = 8
# IEEE binary16 represents every integer through 2048 exactly.  Production
# padding is deliberately local (8 px by default), so this is both an exact
# invalid marker and far outside the set of valid offsets.
OFFSET_SENTINEL = 2048.0
OFFSET_FORMAT = "RG16F"


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


def bounded_jump_steps(radius):
    """Power-of-two propagation schedule bounded by ``radius``.

    Eight pixels therefore requires only 8/4/2/1, rather than a full-image
    jump flood beginning at half the canvas width.
    """
    radius = int(radius)
    if radius < 0 or radius > 1023:
        raise ValueError("padding radius must be in [0, 1023]")
    if radius == 0:
        return ()
    step = 1 << (radius.bit_length() - 1)
    return tuple(step >> index for index in range(step.bit_length()))


def offset_map_bytes(canvas_size, *, buffers=1):
    """Exact storage for an RG16F offset map (two 16-bit components)."""
    size = int(canvas_size)
    if size <= 0 or int(buffers) < 0:
        raise ValueError("invalid map dimensions or buffer count")
    return size * size * 4 * int(buffers)


_PROPAGATE_VERT = """
void main()
{
    gl_Position = vec4(pos, 1.0);
}
"""


_PROPAGATE_FRAG = """
bool valid_offset(vec2 value)
{
    return max(abs(value.x), abs(value.y)) < 2048.0;
}

void consider_candidate(ivec2 pixel, ivec2 neighbour, inout vec2 best,
                        inout float best_d2)
{
    if (any(lessThan(neighbour, ivec2(0))) ||
        any(greaterThanEqual(neighbour, map_size))) return;
    vec2 prior = texelFetch(source_offsets, neighbour, 0).rg;
    if (!valid_offset(prior)) return;
    vec2 candidate = prior + vec2(neighbour - pixel);
    float d2 = dot(candidate, candidate);
    if (d2 > float(radius_px * radius_px)) return;
    ivec2 candidate_source = pixel + ivec2(candidate);
    ivec2 best_source = pixel + ivec2(best);
    bool tie_first = d2 == best_d2 &&
        (candidate_source.y < best_source.y ||
         (candidate_source.y == best_source.y &&
          candidate_source.x < best_source.x));
    if (d2 < best_d2 || tie_first) {
        best = candidate;
        best_d2 = d2;
    }
}

void main()
{
    ivec2 pixel = ivec2(gl_FragCoord.xy);
    vec2 best = texelFetch(source_offsets, pixel, 0).rg;
    float best_d2 = valid_offset(best) ? dot(best, best) : 1e30;
    for (int y = -1; y <= 1; ++y) {
        for (int x = -1; x <= 1; ++x) {
            if (x == 0 && y == 0) continue;
            consider_candidate(pixel, pixel + ivec2(x, y) * jump_px,
                               best, best_d2);
        }
    }
    fragOffset = best;
}
"""


def _propagate_shader_create_info():
    import gpu
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant("IVEC2", "map_size")
    info.push_constant("INT", "jump_px")
    info.push_constant("INT", "radius_px")
    info.sampler(0, "FLOAT_2D", "source_offsets")
    info.vertex_in(0, "VEC3", "pos")
    info.fragment_out(0, "VEC2", "fragOffset")
    info.vertex_source(_PROPAGATE_VERT)
    info.fragment_source(_PROPAGATE_FRAG)
    return info


@dataclass
class CompactOffsetMap:
    """Prototype GPU map.  Not connected to production painting."""

    texture: object
    width: int
    height: int
    radius: int
    initialization_ms: float
    persistent_bytes: int
    peak_gpu_build_bytes: int
    transient_cpu_bytes: int


def build_compact_offset_map(interior_mask, radius=DEFAULT_PADDING_PX):
    """Build a deterministic bounded-interior source map on the GPU.

    ``interior_mask`` is a 2-D numpy-compatible array.  Interior texels seed
    exact ``(0, 0)`` offsets; all other texels seed ``OFFSET_SENTINEL``.  The
    returned RG16F texture stores a local pixel offset to a deterministic
    interior source texel, or the sentinel outside ``radius``.  The bounded
    jump propagation is approximate and does not promise the globally nearest
    source for every adversarial seed arrangement.

    This intentionally owns no per-channel resources and performs no paint,
    dilation, undo, or readback integration.
    """
    import numpy as np
    import gpu
    from gpu_extras.batch import batch_for_shader

    mask = np.asarray(interior_mask, dtype=bool)
    if mask.ndim != 2 or not mask.size:
        raise ValueError("interior_mask must be a non-empty 2-D array")
    radius = int(radius)
    steps = bounded_jump_steps(radius)
    height, width = mask.shape
    started = time.perf_counter()
    seed = np.full((height, width, 2), OFFSET_SENTINEL, dtype=np.float32)
    seed[mask] = (0.0, 0.0)
    buffer = gpu.types.Buffer("FLOAT", seed.shape, seed)
    current = gpu.types.GPUTexture((width, height), format=OFFSET_FORMAT,
                                   data=buffer)
    target = gpu.types.GPUTexture((width, height), format=OFFSET_FORMAT)
    shader = gpu.shader.create_from_info(_propagate_shader_create_info())
    batch = batch_for_shader(shader, "TRI_FAN", {
        "pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]})
    for step in steps:
        framebuffer = gpu.types.GPUFrameBuffer(color_slots=(target,))
        with framebuffer.bind():
            framebuffer.viewport_set(0, 0, width, height)
            gpu.state.blend_set("NONE")
            shader.bind()
            shader.uniform_int("map_size", (width, height))
            shader.uniform_int("jump_px", step)
            shader.uniform_int("radius_px", radius)
            shader.uniform_sampler("source_offsets", current)
            batch.draw(shader)
        current, target = target, current
    elapsed = (time.perf_counter() - started) * 1000.0
    return CompactOffsetMap(
        current, width, height, radius, elapsed,
        offset_map_bytes(width) if width == height else width * height * 4,
        width * height * 8,
        width * height * 8)
