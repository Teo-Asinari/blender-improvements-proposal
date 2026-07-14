# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto GPU multi-target painting engine, promoted from the proven spike.

All gpu-module work for the GPU paint spike: shaders, offscreen
framebuffers, dab dispatch, depth prepass, viewport preview and
GPU->Image sync-back. This is a RESEARCH PROTOTYPE — see FINDINGS.md.

Design (mirrors addons/uv_island_overlay/overlay.py conventions):

- ALL gpu shader/texture/framebuffer work is deferred to draw time and
  exception-LATCH-guarded: gpu object creation raises SystemError in
  ``--background`` (probed on 5.1.2), and a GUI failure must be loud
  exactly once, not once per frame. Starting a session headlessly is a
  harmless no-op (handlers fail to register quietly; pure state works).
- Shaders are built with GPUShaderCreateInfo + gpu.shader.create_from_info
  ONLY: the legacy ``GPUShader(vert, frag)`` constructor raises
  TypeError("cannot create 'GPUShader' instances") on 5.1.2 (probed).
- The modal operator NEVER touches gpu directly: it enqueues dabs and
  tags a redraw; the POST_VIEW draw callback (where a GPU context is
  guaranteed) flushes the queue. Readback results travel the other way:
  the draw callback stashes a numpy array, the modal operator writes it
  into the Image datablock (ID writes from draw callbacks are unsafe).

Dab rasterization technique
---------------------------
The mesh is rendered INTO UV SPACE: the vertex shader emits the UV
coordinate as the clip-space position (``vec4(uv * 2 - 1, 0, 1)``)
while passing the world-space position through to the fragment stage.
Each fragment therefore IS one texel of the paint texture, and knows
the 3D point it textures. The fragment shader projects that point
through the CURRENT VIEW (the same view_proj matrix the depth prepass
used), tests it against the screen-space brush disc (radius + hardness
falloff), tests occlusion against the prepass depth, and emits the
brush color with falloff alpha. Fixed-function 'ALPHA' blending
accumulates the dab into the paint texture — no ping-pong needed
(probed at runtime by _probe_capabilities; a probe line reports it).

Occlusion
---------
The viewport's own depth buffer is not readable from Python, so the
mesh is rendered once per VIEW CHANGE (not per dab) into a private
framebuffer: DEPTH_COMPONENT32F depth attachment for z-testing plus an
R32F color attachment storing positive linear view-space depth. The dab
shader uses exact texel fetches plus a continuity-gated local depth gradient,
which preserves steep visible surfaces without widening the threshold enough
to admit rear shells. The DEPTH texture itself is used only for prepass
z-testing, avoiding backend depth-convention ambiguity.

Deferred sync-back
------------------
Pen-up performs no readback. Paint textures stay GPU-resident for live
composed preview and atomic tile undo. At explicit flush or normal session
exit the framebuffer is read back once. Preferred path
(probed at session start): ``fb.read_color(..., data=Buffer)`` where
the Buffer wraps pre-allocated numpy memory — the pixels land directly
in the array ``image.pixels.foreach_set`` consumes, so the
Buffer->numpy conversion step disappears entirely. Otherwise the
returned ``gpu.types.Buffer`` is converted through the fastest probed
rung of BUFFER_TO_NUMPY_LADDER (asarray / frombuffer / memoryview /
to_list fallback; the ``buffer_to_numpy_path=`` probe line names the
winner). The naive ``np.asarray(buf)`` this replaces was measured at
~1.05 s for one 4K readback — numpy silently degrading to element-wise
sequence iteration over 16.7M Python floats (see FINDINGS.md). CPU
cost is paid once per flush rather than once per stroke.

Multi-channel (v0.3.0)
----------------------
N (1/2/4/8) RGBA16F textures attach to ONE GPUFrameBuffer (MRT); the
dab fragment shader writes a DISTINCT value per attachment (color, a
scalar packed in R, a second color, a height-ish scalar), all modulated
by the same brush falloff alpha. Design constraint (probed):
``gpu.state.blend_set`` is GLOBAL — one blend mode for every attachment
(per-attachment blend is not exposed by the Python API), which forces
same-blend-per-stroke semantics; that matches the shared-mask model
anyway. Sync-back reads N attachments (``fb.read_color(slot=i)``) and
writes N Image datablocks. A CONSERVATIVE dirty-rect shrinks the reads:
per-triangle screen bboxes (numpy, cached per prepass) are intersected
with each flush's dab-disc bbox; the union of the hit triangles' UV
bboxes bounds every texel the rasterizer can have touched (triangles
that cross the near plane count as always-dirty). Sub-rect reads land
in full-size CPU mirror arrays that ``foreach_set`` consumes whole —
Image.pixels has no partial-write API, so the CPU write stays full-cost
(a finding, not a bug).

Instrumentation reports submission and explicit-flush costs separately with
time.perf_counter and reported in a blf overlay, the N-panel, and one
machine-readable ``GPU_PAINT_SPIKE_STROKE ...`` console line per stroke.
NOTE: per-dab times measured on the CPU are SUBMISSION times (the GPU
runs asynchronously); pen-up deliberately does not drain. An explicit flush
forces completion and reports its transfer cost. Set
DEBUG_COMPARE_READS = True to restore the 0.1.0 A/B probe that timed
GPUTexture.read() next to fb.read_color (a second full transfer per
stroke; off in the production path).
"""

import math
import time
import traceback
from contextlib import contextmanager

import bpy
import gpu

from . import visibility
from . import tile_undo

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Clip-space depth bias for the preview pass (same mechanism as
# uv_island_overlay: pull toward the viewer by a w-scaled constant so
# the painted preview wins z-fighting against the mesh's own surface).
CLIP_DEPTH_BIAS = 1e-4

# Absolute floor for the linear view-space depth tolerance. The shader also
# applies a tiny distance-relative tolerance, avoiding the old nonlinear NDC
# epsilon that admitted rear surfaces when the viewport near clip was small.
DEPTH_EPSILON = 1e-4

# Screen-space dab spacing as a fraction of the brush radius, and a
# safety cap on dabs generated by a single mouse-move event.
DAB_SPACING_FACTOR = 0.25
MIN_DAB_SPACING_PX = 2.0
MAX_DABS_PER_EVENT = 256

# Multi-channel painting: how many RGBA16F targets one session may
# attach to the paint framebuffer (the panel offers 1/2/4/8; the probe
# reports what GPUFrameBuffer actually accepts on this build/GPU).
MAX_CHANNELS = 8

# Stable engine/UI contract for diagnostic live-preview display modes.
PREVIEW_MODES = (
    "LIT_PBR",
    "RAW_TANGENT_NORMAL",
    "NEUTRAL_NORMAL_LIGHTING",
    "HEIGHT_GRAYSCALE",
)
PREVIEW_MODE_INDEX = {name: index for index, name in enumerate(PREVIEW_MODES)}

# Conservative dirty-rect: texel padding added around the accumulated
# UV bbox before the sub-rect read (guards float->texel rounding).
DIRTY_RECT_PAD_PX = 2

# When True, the stroke-end finalize ALSO times GPUTexture.read() next
# to the production fb.read_color — the 0.1.0 A/B probe that produced
# the FINDINGS numbers (they measured ~equal: ~100 vs ~105 ms at 4K on
# OpenGL/Quadro RTX 5000). It costs a second full GPU->CPU transfer per
# stroke, so it is OFF in the production path; the tex_read_ms stat
# only appears when this is enabled.
DEBUG_COMPARE_READS = False

# ---------------------------------------------------------------------------
# GLSL (module-level constants so the headless suite can check them
# structurally — compiling is impossible in --background)
# ---------------------------------------------------------------------------

DAB_VERT_SRC = """
void main()
{
    /* Rasterize in UV space: the UV coordinate IS the clip position, so
     * every covered fragment is exactly one texel of the paint texture. */
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
    worldPos = (model_matrix * vec4(pos, 1.0)).xyz;
}
"""

# Everything up to (and including) the falloff computation is shared by
# the single-channel and MRT variants; dab_frag_src() appends the
# per-attachment output assignments.
_DAB_FRAG_PRELUDE = """
void main()
{
    /* Project this texel's 3D point through the same view_proj the
     * depth prepass used. */
    vec4 clip = view_proj_matrix * vec4(worldPos, 1.0);
    if (clip.w <= 0.0) {
        discard;   /* behind the eye */
    }
    vec3 ndc = clip.xyz / clip.w;

    /* Screen-space brush disc test (region pixels, origin bottom-left,
     * matching Blender's region-relative mouse coordinates). */
    vec2 px = (ndc.xy * 0.5 + 0.5) * region_size;
    float d = distance(px, brush_center_px);
    if (d > brush_radius_px) {
        discard;
    }

    /* Occlusion: compare positive linear view-space depth against the
     * frontmost value stored by the prepass. */
    if (use_occlusion > 0.5) {
        vec2 suv = ndc.xy * 0.5 + 0.5;
        if (suv.x < 0.0 || suv.x > 1.0 || suv.y < 0.0 || suv.y > 1.0) {
            discard;
        }
        float view_depth = dot(view_depth_plane, vec4(worldPos, 1.0));
        if (!impasto_visible_surface(scene_depth_tex, suv, view_depth,
                                     depth_epsilon,
                                     depth_relative_epsilon)) {
            discard;   /* this texel's surface point is hidden */
        }
    }

    /* Round brush falloff: 1 inside the hardness core, smoothstep to 0
     * at the rim. MUST match engine.brush_falloff() (tested headless). */
    float t = d / max(brush_radius_px, 1e-6);
    float h = clamp(brush_hardness, 0.0, 0.999);
    float f = 1.0 - smoothstep(h, 1.0, t);
"""

# v0.2.0's single-channel source, byte-for-byte (the N=1 case must not
# regress): prelude + the one output assignment.
def dab_frag_src(channels=1, additive=False):
    """MRT fragment source with one independent RGBA payload per slot."""
    if channels > MAX_CHANNELS:
        raise ValueError("channels > MAX_CHANNELS")
    lines = []
    for i in range(channels):
        output = "fragColor" if i == 0 else "fragColor%d" % i
        if additive:
            lines.append(
                "    %s = vec4(brush_value%d.rgb * brush_value%d.a * "
                "pressure * f, pressure * f);" % (output, i, i))
        else:
            lines.append("    %s = vec4(brush_value%d.rgb, "
                         "brush_value%d.a * pressure * f);"
                         % (output, i, i))
    return _DAB_FRAG_PRELUDE + "\n".join(lines) + "\n}\n"

PREPASS_VERT_SRC = """
void main()
{
    vec4 world = model_matrix * vec4(pos, 1.0);
    vec4 clip = view_proj_matrix * world;
    gl_Position = clip;
    viewDepth = dot(view_depth_plane, world);
}
"""

PREPASS_FRAG_SRC = """
void main()
{
    fragColor = vec4(viewDepth, 0.0, 0.0, 1.0);
}
"""

PREVIEW_VERT_SRC = """
void main()
{
    vec4 world = model_matrix * vec4(pos, 1.0);
    gl_Position = view_proj_matrix * world;
    /* Depth bias: pull toward the viewer in clip space (w-scaled, same
     * mechanism as uv_island_overlay) so the preview coat wins
     * z-fighting against the mesh surface it sits on. */
    gl_Position.z -= %r * gl_Position.w;
    uvInterp = uv;
    worldPos = world.xyz;
}
""" % CLIP_DEPTH_BIAS

PREVIEW_FRAG_SRC = """
vec3 srgb_to_linear(vec3 c)
{
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + 0.055) / 1.055, vec3(2.4));
    return mix(hi, lo, lessThanEqual(c, vec3(0.04045)));
}

vec4 straight_sample(sampler2D tex, vec2 uv)
{
    vec4 c = texture(tex, uv);
    if (c.a > 1e-6) c.rgb /= c.a;
    else c.rgb = vec3(0.0);
    return c;
}

float ggx_distribution(float ndh, float roughness)
{
    float a = max(roughness * roughness, 0.025);
    float a2 = a * a;
    float d = ndh * ndh * (a2 - 1.0) + 1.0;
    return a2 / max(3.14159265 * d * d, 1e-5);
}

float smith_visibility(float ndv, float ndl, float roughness)
{
    float k = (roughness + 1.0);
    k = k * k * 0.125;
    float gv = ndv / max(ndv * (1.0 - k) + k, 1e-5);
    float gl = ndl / max(ndl * (1.0 - k) + k, 1e-5);
    return gv * gl;
}

vec3 fresnel_schlick(float vdh, vec3 f0)
{
    return f0 + (vec3(1.0) - f0) * pow(1.0 - vdh, 5.0);
}

vec3 pbr_light(vec3 n, vec3 v, vec3 l, vec3 radiance, vec3 albedo,
               float metallic, float roughness)
{
    vec3 h = normalize(v + l);
    float ndv = max(dot(n, v), 0.001);
    float ndl = max(dot(n, l), 0.0);
    float ndh = max(dot(n, h), 0.0);
    float vdh = max(dot(v, h), 0.0);
    vec3 f0 = mix(vec3(0.04), albedo, metallic);
    vec3 f = fresnel_schlick(vdh, f0);
    vec3 specular = ggx_distribution(ndh, roughness)
                  * smith_visibility(ndv, ndl, roughness) * f
                  / max(4.0 * ndv * ndl, 1e-4);
    vec3 diffuse = (vec3(1.0) - f) * (1.0 - metallic)
                 * albedo / 3.14159265;
    return (diffuse + specular) * radiance * ndl;
}

void main()
{
    if (preview_mode == 1) {
        vec4 raw_normal = straight_sample(normal_tex, uvInterp);
        float normal_alpha = has_normal > 0.5 ? raw_normal.a : 0.0;
        vec3 encoded = mix(vec3(0.5, 0.5, 1.0), raw_normal.rgb,
                           clamp(normal_alpha, 0.0, 1.0));
        fragColor = vec4(encoded, 1.0);
        return;
    }
    if (preview_mode == 3) {
        float raw_height = texture(height_tex, uvInterp).r;
        float h = has_height > 0.5 ? raw_height : 0.5;
        fragColor = vec4(vec3(h), 1.0);
        return;
    }

    vec4 normal_sample = straight_sample(normal_tex, uvInterp);
    float height = texture(height_tex, uvInterp).r;

    vec3 dpdx = dFdx(worldPos), dpdy = dFdy(worldPos);
    vec2 dudx = dFdx(uvInterp), dudy = dFdy(uvInterp);
    vec3 geometric_n = normalize(cross(dpdx, dpdy));
    if (!gl_FrontFacing) geometric_n = -geometric_n;
    float uv_det = dudx.x * dudy.y - dudx.y * dudy.x;
    vec3 tangent;
    vec3 bitangent;
    if (abs(uv_det) > 1e-8) {
        float orientation = sign(uv_det);
        tangent = normalize((dpdx * dudy.y - dpdy * dudx.y)
                            * orientation);
        bitangent = normalize((-dpdx * dudy.x + dpdy * dudx.x)
                              * orientation);
    } else {
        vec3 axis = abs(geometric_n.z) < 0.999
            ? vec3(0.0, 0.0, 1.0) : vec3(0.0, 1.0, 0.0);
        tangent = normalize(cross(axis, geometric_n));
        bitangent = normalize(cross(geometric_n, tangent));
    }
    vec3 n = geometric_n;
    if (has_normal > 0.5 && normal_sample.a > 1e-6) {
        vec3 encoded_n = mix(vec3(0.5, 0.5, 1.0), normal_sample.rgb,
                             clamp(normal_sample.a, 0.0, 1.0));
        vec3 tangent_n = normalize(encoded_n * 2.0 - 1.0);
        n = normalize(mat3(tangent, bitangent, geometric_n) * tangent_n);
    }
    if (has_height > 0.5) {
        /* Screen derivatives avoid four extra texture taps and keep the
         * diagnostic response stable across texture resolutions. */
        float dhdx = dFdx(height);
        float dhdy = dFdy(height);
        vec3 displaced_dx = dpdx + geometric_n * dhdx * 8.0;
        vec3 displaced_dy = dpdy + geometric_n * dhdy * 8.0;
        vec3 height_n = normalize(cross(displaced_dx, displaced_dy));
        if (dot(height_n, geometric_n) < 0.0) height_n = -height_n;
        n = normalize(n + (height_n - geometric_n));
    }

    if (preview_mode == 2) {
        vec3 neutral_l0 = normalize(vec3(0.55, 0.20, 0.81));
        vec3 neutral_l1 = normalize(vec3(-0.65, 0.35, 0.67));
        float neutral = 0.12
            + 0.58 * max(dot(n, neutral_l0), 0.0)
            + 0.30 * max(dot(n, neutral_l1), 0.0);
        fragColor = vec4(vec3(neutral), 1.0);
        return;
    }

    vec4 base = straight_sample(base_color_tex, uvInterp);
    vec4 metal_sample = straight_sample(metallic_tex, uvInterp);
    vec4 rough_sample = straight_sample(roughness_tex, uvInterp);
    vec3 albedo = has_base_color > 0.5
        ? mix(vec3(0.5), srgb_to_linear(base.rgb), base.a) : vec3(0.5);
    float metallic = has_metallic > 0.5
        ? mix(0.0, metal_sample.r, metal_sample.a) : 0.0;
    float roughness = has_roughness > 0.5
        ? mix(0.5, rough_sample.r, rough_sample.a) : 0.5;

    /* Lightweight, deterministic environment-style PBR preview.
     * It deliberately composes every resident channel; Blender's material
     * remains authoritative after an explicit/session-exit flush. */
    vec3 v = normalize(camera_position - worldPos);
    metallic = clamp(metallic, 0.0, 1.0);
    roughness = clamp(roughness, 0.04, 1.0);
    vec3 rgb = pbr_light(n, v, normalize(vec3(0.35, 0.45, 0.82)),
                         vec3(3.0, 2.8, 2.55), albedo, metallic, roughness);
    rgb += pbr_light(n, v, normalize(vec3(-0.72, 0.18, 0.48)),
                     vec3(0.55, 0.72, 1.0), albedo, metallic, roughness);
    rgb += pbr_light(n, v, normalize(vec3(0.08, -0.82, 0.35)),
                     vec3(0.7, 0.42, 0.28), albedo, metallic, roughness);
    float sky = clamp(n.z * 0.5 + 0.5, 0.0, 1.0);
    vec3 environment = mix(vec3(0.035, 0.028, 0.025),
                           vec3(0.18, 0.23, 0.32), sky);
    vec3 f0 = mix(vec3(0.04), albedo, metallic);
    vec3 ambient_diffuse = albedo * (1.0 - metallic) * environment;
    vec3 ambient_specular = f0 * mix(vec3(0.32), environment * 1.8,
                                    roughness);
    rgb += ambient_diffuse + ambient_specular;
    rgb = rgb / (rgb + vec3(1.0));

    float coverage = 0.0;
    if (has_base_color > 0.5) coverage = max(coverage, base.a);
    if (has_metallic > 0.5) coverage = max(coverage, metal_sample.a);
    if (has_roughness > 0.5) coverage = max(coverage, rough_sample.a);
    if (has_normal > 0.5) coverage = max(coverage, normal_sample.a);
    if (has_height > 0.5) coverage = max(coverage,
                                        clamp(abs(height - 0.5) * 8.0, 0.0, 1.0));
    fragColor = vec4(rgb, coverage * preview_opacity);
}
"""

COPY_VERT_SRC = """
void main()
{
    gl_Position = vec4(pos, 1.0);
    uvInterp = uv_origin + uv * uv_scale;
}
"""

COPY_FRAG_SRC = """
void main()
{
    fragColor = texture(source_tex, uvInterp);
}
"""

# ---------------------------------------------------------------------------
# Pure math (headless-testable; the GLSL above must agree)
# ---------------------------------------------------------------------------


def brush_falloff(t, hardness):
    """Brush alpha at normalized distance ``t`` (0 center .. 1 rim) for a
    given hardness (0..1). 1.0 inside the hardness core, smoothstep to
    0.0 at the rim. Python mirror of the GLSL in DAB_FRAG_SRC."""
    h = min(max(hardness, 0.0), 0.999)
    if t <= h:
        return 1.0
    if t >= 1.0:
        return 0.0
    u = (t - h) / (1.0 - h)
    return 1.0 - (u * u * (3.0 - 2.0 * u))


def interpolate_dabs(x0, y0, x1, y1, spacing, leftover=0.0):
    """Dab positions along the segment (x0,y0)->(x1,y1) at ``spacing``
    px, carrying ``leftover`` distance from the previous segment so a
    fast stroke keeps even spacing across events. Returns
    ``(positions, new_leftover)`` where positions is a list of
    ``(x, y, t)`` with t in (0, 1]."""
    dx = x1 - x0
    dy = y1 - y0
    dist = math.hypot(dx, dy)
    if dist <= 0.0 or spacing <= 0.0:
        return [], leftover
    out = []
    s = spacing - leftover
    while s <= dist and len(out) < MAX_DABS_PER_EVENT:
        t = s / dist
        out.append((x0 + dx * t, y0 + dy * t, t))
        s += spacing
    new_leftover = dist - (s - spacing)
    return out, new_leftover


def dab_spacing(radius_px):
    return max(MIN_DAB_SPACING_PX, radius_px * DAB_SPACING_FACTOR)


def build_mesh_soup(obj):
    """(coords float32 (n,3), uvs float32 (n,2)) triangle soup with
    per-corner UVs from the mesh's active UV layer, or (None, None)
    when there is no UV layer. Pure (no gpu) — Object Mode reads via
    Mesh arrays, same pattern as uv_island_overlay."""
    import numpy as np

    me = obj.data
    uv_layer = me.uv_layers.active
    if uv_layer is None or len(me.loops) == 0:
        return None, None

    uv = np.empty(len(me.loops) * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)

    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    tris = me.loop_triangles
    n_tris = len(tris)
    if n_tris == 0:
        return None, None
    tv = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("vertices", tv)
    tl = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("loops", tl)

    coords = np.ascontiguousarray(co[tv], dtype=np.float32)
    uvs = np.ascontiguousarray(uv[tl], dtype=np.float32)
    return coords, uvs


# ---------------------------------------------------------------------------
# Conservative dirty-rect tracking (pure numpy; headless-testable)
#
# Exact "which texels did the rasterizer touch" is unknowable on the
# CPU, but a CONSERVATIVE bound is cheap: a texel can only have been
# written if its triangle produced a fragment inside a dab disc, and a
# triangle can only do that if its screen-space bbox intersects the
# disc's bbox. So: project every triangle once per prepass (numpy
# matmul), intersect the per-triangle screen bboxes with each flush's
# dab-union rect, and union the HIT triangles' UV bboxes. Triangles
# with any vertex at clip.w <= 0 cannot be projected — they count as
# always-dirty. Occluded/discarded fragments only make the rect larger
# than needed, never smaller: conservative is correct.
# ---------------------------------------------------------------------------


def triangle_uv_bboxes(uvs):
    """(n_tris, 4) float32 [min_u, min_v, max_u, max_v] per triangle
    from the soup's per-corner UVs (n_tris*3, 2)."""
    import numpy as np
    t = np.asarray(uvs, dtype=np.float32).reshape(-1, 3, 2)
    return np.concatenate([t.min(axis=1), t.max(axis=1)], axis=1)


def triangle_screen_bboxes(coords, mvp, region_w, region_h):
    """(bboxes (n_tris, 4) [minx, miny, maxx, maxy] in region pixels,
    unprojectable (n_tris,) bool) for the soup's coords (n_tris*3, 3)
    under the 4x4 ``mvp`` (view_proj @ model, row-major like mathutils).
    Triangles with any vertex at clip.w <= 0 are flagged unprojectable
    (their bbox row is garbage; callers must treat them as dirty)."""
    import numpy as np
    co = np.asarray(coords, dtype=np.float32).reshape(-1, 3)
    m = np.asarray(mvp, dtype=np.float32)
    hom = co @ m[:3, :3].T + m[:3, 3]          # clip.xyz
    w = co @ m[3, :3].T + m[3, 3]              # clip.w
    bad = (w <= 1e-6)
    w_safe = np.where(bad, 1.0, w)
    px = (hom[:, :2] / w_safe[:, None] * 0.5 + 0.5)
    px[:, 0] *= float(region_w)
    px[:, 1] *= float(region_h)
    tri = px.reshape(-1, 3, 2)
    bboxes = np.concatenate([tri.min(axis=1), tri.max(axis=1)], axis=1)
    unprojectable = bad.reshape(-1, 3).any(axis=1)
    return bboxes.astype(np.float32), unprojectable


def dab_rect_union(dabs, radius):
    """Screen-space bbox (minx, miny, maxx, maxy) covering every dab
    disc in ``dabs`` (iterable of (x, y, ...) tuples)."""
    xs = [d[0] for d in dabs]
    ys = [d[1] for d in dabs]
    r = float(radius)
    return (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)


def dirty_uv_bbox(screen_bboxes, unprojectable, uv_bboxes, rect):
    """UV bbox (min_u, min_v, max_u, max_v) unioned over the triangles
    whose screen bbox intersects ``rect`` — plus every unprojectable
    triangle (always dirty). None when no triangle is hit."""
    import numpy as np
    hit = ~((screen_bboxes[:, 2] < rect[0])
            | (screen_bboxes[:, 0] > rect[2])
            | (screen_bboxes[:, 3] < rect[1])
            | (screen_bboxes[:, 1] > rect[3]))
    hit = hit | unprojectable
    if not bool(hit.any()):
        return None
    sel = uv_bboxes[hit]
    return (float(sel[:, 0].min()), float(sel[:, 1].min()),
            float(sel[:, 2].max()), float(sel[:, 3].max()))


def union_bbox(a, b):
    """Union of two (min_u, min_v, max_u, max_v) bboxes; either may be
    None (returns the other)."""
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]),
            max(a[2], b[2]), max(a[3], b[3]))


def uv_bbox_to_pixel_rect(bbox, size, pad=DIRTY_RECT_PAD_PX):
    """Clamped integer texel rect (x, y, w, h) for a UV bbox on a
    size x size texture, padded by ``pad`` texels; None for a None/
    degenerate/out-of-range bbox."""
    if bbox is None:
        return None
    x0 = max(0, int(math.floor(bbox[0] * size)) - pad)
    y0 = max(0, int(math.floor(bbox[1] * size)) - pad)
    x1 = min(size, int(math.ceil(bbox[2] * size)) + pad)
    y1 = min(size, int(math.ceil(bbox[3] * size)) + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


# ---------------------------------------------------------------------------
# Buffer -> numpy conversion ladder (stroke-end readback)
#
# The 0.1.0 sync-back converted the readback Buffer with a bare
# np.asarray(buf) and measured ~1050 ms at 4K (GUI, Quadro RTX 5000):
# when the exporter offers neither __array_interface__ nor a usable
# C buffer protocol, numpy silently degrades to element-wise sequence
# iteration over 16.7M Python floats. The ladder below tries the
# zero-copy mechanisms EXPLICITLY (so a "works but slow" path can never
# be selected) and falls back to the always-correct to_list. Which rung
# a given Blender build/backend supports is unknowable headless (Buffer
# creation needs a GPU context), so probe_buffer_to_numpy_path picks
# the rung at session start on a small real Buffer with known contents,
# and the choice is logged as GPU_PAINT_SPIKE_PROBE
# buffer_to_numpy_path=<rung>. The rung functions themselves are pure
# and headless-testable with stand-in objects.
# ---------------------------------------------------------------------------


def _conv_asarray(buf):
    """Zero-copy via numpy's __array_interface__. Gated on the
    attribute: a bare np.asarray(buf) would silently 'succeed' through
    the element-wise sequence protocol on objects without it — the
    exact ~1 s/stroke trap measured at 4K."""
    import numpy as np
    if not hasattr(buf, "__array_interface__"):
        raise TypeError("no __array_interface__")
    return np.asarray(buf)


def _conv_frombuffer(buf):
    """Zero-copy via the C buffer protocol (PEP 3118). Raises if the
    object is not a buffer exporter. float32 by contract: the readback
    is always fb.read_color(..., 'FLOAT')."""
    import numpy as np
    return np.frombuffer(buf, dtype=np.float32)


def _conv_memoryview(buf):
    """Zero-copy via an explicit memoryview flattened to bytes; can
    succeed where np.frombuffer's stricter buffer request fails (e.g.
    exporters that only serve multi-dimensional views)."""
    import numpy as np
    return np.frombuffer(memoryview(buf).cast('B'), dtype=np.float32)


def _conv_to_list(buf):
    """Always-correct fallback: element-wise Python conversion. SLOW —
    order of 1-2 s for a 4K RGBA float buffer (measured: 1858 ms for a
    16.7M-element list -> np.asarray, headless, this machine)."""
    import numpy as np
    return np.asarray(buf.to_list(), dtype=np.float32)


# Fastest first; the last rung must be the always-correct fallback.
BUFFER_TO_NUMPY_LADDER = (
    ("asarray", _conv_asarray),
    ("frombuffer", _conv_frombuffer),
    ("memoryview", _conv_memoryview),
    ("to_list_fallback", _conv_to_list),
)


def probe_buffer_to_numpy_path(buf, reference):
    """Name of the first BUFFER_TO_NUMPY_LADDER rung that converts
    ``buf`` into values matching ``reference`` (flat float32). Meant to
    run ONCE per session on a small Buffer whose contents are known
    (reference comes from the trusted-but-slow to_list). Pure logic —
    headless tests exercise it with stand-in objects."""
    import numpy as np
    ref = np.asarray(reference, dtype=np.float32).ravel()
    for name, conv in BUFFER_TO_NUMPY_LADDER:
        try:
            arr = np.asarray(conv(buf), dtype=np.float32).ravel()
        except Exception:
            continue
        if arr.size == ref.size and np.allclose(arr, ref, atol=1e-3):
            return name
    return "to_list_fallback"


def buffer_to_numpy(buf, path="to_list_fallback"):
    """Flat float32 numpy array from a float Buffer, converted through
    the ladder rung named ``path`` (as chosen by
    probe_buffer_to_numpy_path). Unknown names or a failing rung fall
    back to the always-correct to_list — slow but never wrong."""
    import numpy as np
    conv = dict(BUFFER_TO_NUMPY_LADDER).get(path)
    if conv is not None:
        try:
            return np.asarray(conv(buf), dtype=np.float32).ravel()
        except Exception:
            pass
    return _conv_to_list(buf).ravel()


# Latched by _probe_capabilities (GUI only; headless-safe defaults):
# which conversion rung _finalize_stroke_gpu uses, and whether
# fb.read_color can fill a Buffer wrapping numpy memory directly
# (which makes the conversion step disappear entirely).
_buffer_numpy_path = "to_list_fallback"
_read_into_numpy = False


# ---------------------------------------------------------------------------
# Shader create-infos (descriptor population is pure bookkeeping and
# works in --background — probed on 5.1.2; only create_from_info touches
# the GPU. The headless suite builds these structurally.)
# ---------------------------------------------------------------------------


def dab_shader_create_info(channels=1, additive=False):
    iface = gpu.types.GPUStageInterfaceInfo("gpu_paint_spike_dab_iface")
    iface.smooth('VEC3', "worldPos")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "model_matrix")
    info.push_constant('MAT4', "view_proj_matrix")
    info.push_constant('VEC4', "view_depth_plane")
    info.push_constant('VEC2', "region_size")
    info.push_constant('VEC2', "brush_center_px")
    info.push_constant('FLOAT', "brush_radius_px")
    info.push_constant('FLOAT', "brush_hardness")
    info.push_constant('FLOAT', "depth_epsilon")
    info.push_constant('FLOAT', "depth_relative_epsilon")
    info.push_constant('FLOAT', "use_occlusion")
    info.push_constant('FLOAT', "pressure")
    for i in range(channels):
        info.push_constant('VEC4', "brush_value%d" % i)
    info.sampler(0, 'FLOAT_2D', "scene_depth_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    for i in range(1, channels):
        info.fragment_out(i, 'VEC4', "fragColor%d" % i)
    info.vertex_source(DAB_VERT_SRC)
    info.fragment_source(visibility.GLSL_SOURCE
                         + dab_frag_src(channels, additive=additive))
    return info


def prepass_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("gpu_paint_spike_prepass_iface")
    iface.smooth('FLOAT', "viewDepth")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "model_matrix")
    info.push_constant('MAT4', "view_proj_matrix")
    info.push_constant('VEC4', "view_depth_plane")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(PREPASS_VERT_SRC)
    info.fragment_source(PREPASS_FRAG_SRC)
    return info


def preview_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("gpu_paint_spike_preview_iface")
    iface.smooth('VEC2', "uvInterp")
    iface.smooth('VEC3', "worldPos")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "model_matrix")
    info.push_constant('MAT4', "view_proj_matrix")
    info.push_constant('VEC3', "camera_position")
    info.push_constant('FLOAT', "preview_opacity")
    info.push_constant('INT', "preview_mode")
    for name in ("base_color", "metallic", "roughness", "normal", "height"):
        info.push_constant('FLOAT', "has_" + name)
    info.sampler(0, 'FLOAT_2D', "base_color_tex")
    info.sampler(1, 'FLOAT_2D', "metallic_tex")
    info.sampler(2, 'FLOAT_2D', "roughness_tex")
    info.sampler(3, 'FLOAT_2D', "normal_tex")
    info.sampler(4, 'FLOAT_2D', "height_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(PREVIEW_VERT_SRC)
    info.fragment_source(PREVIEW_FRAG_SRC)
    return info


def copy_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_tile_copy_iface")
    iface.smooth('VEC2', "uvInterp")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('VEC2', "uv_origin")
    info.push_constant('VEC2', "uv_scale")
    info.sampler(0, 'FLOAT_2D', "source_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(COPY_VERT_SRC)
    info.fragment_source(COPY_FRAG_SRC)
    return info


# ---------------------------------------------------------------------------
# Stroke payload planning (pure; headless-testable)
#
# One logical Impasto Paint layer deposits into several channels per
# stroke. Each channel's payload carries the value the dab shader
# writes into that channel's MRT attachment plus the blend class the
# batch planner groups by: material channels alpha-blend (MIX), Height
# accumulates (ADD — RAISE adds, LOWER subtracts, both around the
# neutral 0.5 canvas). COLOR-kind channels are stored sRGB-encoded in
# their Image datablocks, and the GPU path writes raw stored values,
# so their brush values are encoded here; scalar/Non-Color channels
# pass through raw.
# ---------------------------------------------------------------------------

# Channels the multi-channel brush can deposit into, in registry order.
# Other channels (emission, subsurface, ...) stay native-paint-only.
GPU_PAINT_CHANNEL_KEYS = ("base_color", "metallic", "roughness",
                          "normal", "height")


def linear_to_srgb(v):
    """Scene-linear component -> sRGB-encoded component (IEC 61966-2-1).
    COLOR-kind canvases store encoded values; painting the raw linear
    swatch would render brighter than picked."""
    v = max(0.0, float(v))
    if v <= 0.0031308:
        return v * 12.92
    return 1.055 * v ** (1.0 / 2.4) - 0.055


def premultiply_canvas(arr):
    """Straight-alpha canvas pixels -> the PREMULTIPLIED space the MIX
    dab framebuffer accumulates in. In place; ``arr`` is float32 RGBA
    (flat or shaped); returns ``arr``.

    ``gpu.state.blend_set('ALPHA')`` is source-over with the destination
    held in premultiplied alpha (rgb: SRC_ALPHA / ONE_MINUS_SRC_ALPHA,
    a: ONE / ONE_MINUS_SRC_ALPHA), so every accumulated texel stores
    ``(value*coverage, coverage)``.  Image canvases are STRAIGHT alpha —
    the compiled node chains read the RGB as the channel VALUE and gate
    the mix by the alpha — so both boundaries must convert: premultiply
    on upload (here), divide on readback (unpremultiply_readback).
    Skipping the conversions was the v0.4 regression: soft brush rims
    and tablet pressure (any texel with a < 1) synced ``value*a`` back
    into the canvas, breaking Metallic/Roughness levels and decoding
    Tangent Normal rims into garbage directions in Material Preview.
    """
    view = arr.reshape(-1, 4)
    view[:, :3] *= view[:, 3:4]
    return arr


def unpremultiply_readback(arr):
    """Premultiplied MIX-framebuffer pixels -> straight-alpha canvas
    pixels. Returns a new float32 copy (the session's CPU mirror must
    stay in framebuffer space); a<=0 texels get rgb=0 (transparent —
    the compiled chains ignore their value)."""
    import numpy as np
    out = np.array(arr, dtype=np.float32, copy=True)
    view = out.reshape(-1, 4)
    alpha = view[:, 3:4]
    covered = alpha > 1e-8
    np.divide(view[:, :3], alpha, out=view[:, :3], where=covered)
    view[:, :3] *= covered
    return out


def stroke_payloads(channel_keys, brush):
    """MRT payload per channel key, aligned with ``channel_keys``.

    ``brush`` is a plain dict of the layer's brush properties:
    ``color`` (linear RGB), ``roughness``, ``metallic``, ``normal``
    (encoded RGB), ``height_strength``, ``height_direction``
    ('RAISE'|'LOWER'), optional ``strength`` (dab alpha, default 1.0).
    Pure — the operator snapshots PropertyGroups into the dict."""
    strength = float(brush.get("strength", 1.0))
    payloads = []
    for key in channel_keys:
        if key not in GPU_PAINT_CHANNEL_KEYS:
            raise ValueError("unpaintable channel %r" % key)
        if key == "base_color":
            value = tuple(linear_to_srgb(c)
                          for c in brush.get("color", (0.8, 0.2, 0.1)))
            blend = "MIX"
        elif key == "roughness":
            r = float(brush.get("roughness", 0.5))
            value = (r, r, r)
            blend = "MIX"
        elif key == "metallic":
            m = float(brush.get("metallic", 0.0))
            value = (m, m, m)
            blend = "MIX"
        elif key == "normal":
            value = tuple(float(c) for c in
                          brush.get("normal", (0.5, 0.5, 1.0)))
            blend = "MIX"
        else:   # height: signed additive step around the 0.5 canvas
            step = float(brush.get("height_strength", 0.05))
            if brush.get("height_direction", "RAISE") == "LOWER":
                step = -step
            value = (step, step, step)
            blend = "ADD"
        payloads.append({"value": value, "strength": strength,
                         "blend": blend})
    return payloads


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

MAX_FB_CHANNELS = 4


def plan_target_batches(payloads, max_slots=MAX_FB_CHANNELS):
    """Group equal-blend targets into framebuffer-sized batches.

    Blender's Python GPU state exposes one blend mode for the whole MRT
    framebuffer. MIX material channels and signed ADD Height therefore render
    in separate passes while consuming the exact same queued dabs.
    """
    groups = []
    for blend in ("MIX", "ADD"):
        indices = [i for i, item in enumerate(payloads)
                   if item.get("blend", "MIX") == blend]
        for start in range(0, len(indices), max_slots):
            groups.append((blend, tuple(indices[start:start + max_slots])))
    known = {i for _blend, indices in groups for i in indices}
    extra = [i for i in range(len(payloads)) if i not in known]
    for start in range(0, len(extra), max_slots):
        groups.append(("MIX", tuple(extra[start:start + max_slots])))
    return tuple(groups)


class _Session:
    def __init__(self, obj_name, image_names, size, region_ptr, channels=1,
                 payloads=None, settings=None):
        self.obj_name = obj_name
        # One Image datablock per channel (channel 0 first). Extra
        # channels beyond the provided images simply never sync.
        self.image_names = list(image_names)
        self.image_name = self.image_names[0] if self.image_names else ""
        self.size = size
        self.region_ptr = region_ptr
        self.channels = max(1, int(channels))
        self.payloads = list(payloads or [
            {"value": (0.8, 0.2, 0.1), "strength": 1.0,
             "blend": "MIX"} for _ in range(self.channels)])
        while len(self.payloads) < self.channels:
            self.payloads.append({"value": (0.0, 0.0, 0.0),
                                  "strength": 1.0, "blend": "MIX"})
        self.payloads = self.payloads[:self.channels]
        self.settings = dict(settings or {})
        self.target_batches = plan_target_batches(self.payloads)

        # Pure geometry (built eagerly at start — no gpu involved).
        self.coords = None
        self.uvs = None
        self.tri_uv_bboxes = None      # (n_tris, 4), pure numpy

        # GPU resources — ALL lazy, created at first draw.
        self.dab_shaders = None
        self.prepass_shader = None
        self.preview_shader = None
        self.paint_texs = None         # list of N RGBA16F GPUTextures
        self.paint_fbs = None
        self.depth_color_tex = None    # R32F: prepass NDC depth
        self.depth_depth_tex = None    # DEPTH_COMPONENT32F: z-testing
        self.depth_fb = None
        self.depth_fb_size = None      # (w, h) the depth FB was built at
        self.batch_dabs = None         # one pos+uv batch per shader
        self.batch_prepass = None      # pos only
        self.batch_preview = None      # pos + uv, composed surface overlay
        self.neutral_tex = None        # 1x1 fallback for absent channels
        self.copy_shader = None
        self.copy_batch = None
        self.single_fbs = None
        self.gpu_ready = False
        self.probe_lines = None

        # Prepass cache: key of the view state the prepass was rendered
        # for, plus the matrices captured at that moment (the dab shader
        # MUST use the same matrices for the comparison to be valid).
        self.prepass_key = None
        self.view_proj = None          # mathutils.Matrix copy
        self.view = None               # world -> view matrix copy
        self.view_depth_plane = None   # -Z row of world -> view matrix
        self.model = None              # mathutils.Matrix copy
        self.prepass_ms = 0.0

        # Dirty-rect: per-triangle screen bboxes cached per prepass
        # (same matrices the dab shader uses) + per-stroke accumulator.
        self.tri_screen_bboxes = None
        self.tri_unprojectable = None
        self.stroke_dirty = None       # UV bbox or None
        self.stroke_dirty_full = False  # tracking unavailable: full read
        self.session_dirty = None      # accumulated until explicit flush
        self.session_dirty_full = False
        self.dirty_ms = 0.0            # CPU cost of the tracking math

        # Full-size CPU mirror arrays (one per channel): sub-rect reads
        # scatter into them; foreach_set consumes them whole (there is
        # no partial Image.pixels write). Allocated at first finalize.
        self.cpu_mirrors = None

        # Stroke state.
        self.stroke_active = False
        self.stroke_t0 = None
        self.first_dab_t = None
        self.last_dab_t = None
        self.dab_count = 0
        self.submit_times = []         # seconds, one per dab
        self.dab_queue = []            # (x, y, pressure)
        self.last_px = None            # last dab position (x, y)
        self.leftover = 0.0
        self.pending_finalize = False
        self.pending_flush = False
        self.flush_in_flight = False
        self.stroke_transaction = None
        self.history = tile_undo.TileHistory()
        self.history_backend = None
        self.pending_history_action = None
        # Screen-space cursor is independent of paint preview.  Keeping the
        # composed material visible avoids the old raw-channel overlay that
        # made PBR rendering appear to change while the session was active.
        self.cursor = None

        # Sync-back: the draw callback stashes the readback here; the
        # modal operator writes it into the Image datablock.
        self.pending_pixels = None
        self.pending_gpu_stats = None

        # Error latch (printed once; shown in panel + overlay).
        self.error = None

    def latch(self, where):
        self.error = "%s\n%s" % (where, traceback.format_exc())
        print("[gpu_paint_spike] %s — session suspended. Traceback:" % where)
        print(self.error)


_session = None
_handle_view = None
_handle_pixel = None

# Survives stop_session so the panel can show the last stroke's numbers.
_last_stroke_stats = {}

# Ordered (key, label, format) for UI display of the stats dict.
STATS_LAYOUT = (
    ("size", "Texture", "%d px"),
    ("channels", "Channels", "%d"),
    ("dabs", "Dabs", "%d"),
    ("stroke_s", "Stroke wall time", "%.3f s"),
    ("dabs_per_s", "Dabs/sec sustained", "%.1f"),
    ("submit_avg_ms", "Dab submit avg", "%.3f ms"),
    ("submit_max_ms", "Dab submit max", "%.3f ms"),
    ("prepass_ms", "Depth prepass", "%.2f ms"),
    ("dirty_ms", "Dirty-rect math", "%.2f ms"),
    ("drain_ms", "GPU drain (1px read)", "%.2f ms"),
    ("readback_rect", "Readback rect", "%s"),
    ("fb_read_ms", "FB readback (all ch)", "%.2f ms"),
    ("fb_read_avg_ch_ms", "FB read avg/channel", "%.2f ms"),
    ("readback_path", "Readback path", "%s"),
    ("tex_read_ms", "tex.read() (debug A/B)", "%.2f ms"),
    ("to_numpy_ms", "Buffer -> numpy", "%.2f ms"),
    ("pixels_write_ms", "pixels.foreach_set (all ch)", "%.2f ms"),
    ("pixels_write_avg_ch_ms", "pixels write avg/channel", "%.2f ms"),
    ("image_update_ms", "image.update()", "%.2f ms"),
    ("syncback_total_ms", "Sync-back total", "%.2f ms"),
)


# ---------------------------------------------------------------------------
# Public API (modal operator + panel)
# ---------------------------------------------------------------------------


def session_active():
    return _session is not None


def stroke_active():
    return _session is not None and _session.stroke_active


def busy():
    """True while queued GPU work or an explicit flush is in flight."""
    s = _session
    return s is not None and (s.pending_finalize
                              or s.pending_flush
                              or s.flush_in_flight
                              or s.pending_history_action is not None
                              or s.pending_pixels is not None)


def last_error():
    return _session.error if _session is not None else None


def last_stroke_stats():
    return dict(_last_stroke_stats)


def probe_lines():
    if _session is not None and _session.probe_lines:
        return list(_session.probe_lines)
    return []


def normalize_preview_mode(mode):
    """Return a stable preview-mode identifier, defaulting safely to Lit."""
    mode = str(mode or "LIT_PBR").upper()
    return mode if mode in PREVIEW_MODE_INDEX else "LIT_PBR"


def preview_mode_index(mode):
    """Integer shader branch for a stable public preview identifier."""
    return PREVIEW_MODE_INDEX[normalize_preview_mode(mode)]


def set_preview_mode(mode):
    """Change live display mode without restarting the GPU paint session."""
    s = _session
    if s is None:
        return False
    s.settings["preview_mode"] = normalize_preview_mode(mode)
    return True


def current_preview_mode():
    if _session is None:
        return "LIT_PBR"
    return normalize_preview_mode(_session.settings.get("preview_mode"))


def set_input_paused(paused):
    """Pause viewport dab capture without ending or synchronizing a session."""
    if _session is None:
        return False
    _session.settings["input_paused"] = bool(paused)
    return True


def input_paused():
    return bool(_session is not None
                and _session.settings.get("input_paused", False))


def request_material_inspect():
    """Synchronize, then show Blender's material without ending the session."""
    s = _session
    if s is None or s.error is not None:
        return False
    s.settings["input_paused"] = True
    s.settings["material_inspect_requested"] = True
    if not has_unflushed_changes() and s.pending_pixels is None:
        s.settings["material_inspect"] = True
        s.settings["material_inspect_requested"] = False
    else:
        s.pending_flush = True
    return True


def complete_material_inspect():
    s = _session
    if s is None or not s.settings.get("material_inspect_requested", False):
        return False
    s.settings["material_inspect_requested"] = False
    s.settings["material_inspect"] = True
    s.settings["input_paused"] = True
    return True


def leave_material_inspect():
    if _session is None:
        return False
    _session.settings["material_inspect_requested"] = False
    _session.settings["material_inspect"] = False
    _session.settings["input_paused"] = False
    return True


def material_inspect_active():
    return bool(_session is not None
                and _session.settings.get("material_inspect", False))


def material_inspect_requested():
    return bool(_session is not None
                and _session.settings.get("material_inspect_requested", False))


def start_session(obj, images, region, channels=None, payloads=None,
                  settings=None):
    """Create the paint session for ``obj``/``images`` (a single Image
    or a list — one per channel, channel 0 first). Pure work happens
    here (geometry soup + per-triangle UV bboxes); ALL gpu work waits
    for the first draw. Safe headless: handler registration failures
    are quietly ignored."""
    global _session
    if _session is not None:
        stop_session()
    if not isinstance(images, (list, tuple)):
        images = [images]
    coords, uvs = build_mesh_soup(obj)
    if coords is None:
        return False
    channel_count = len(images) if channels is None else int(channels)
    s = _Session(obj.name, [im.name for im in images],
                 images[0].size[0], region.as_pointer() if region else 0,
                 channels=channel_count, payloads=payloads,
                 settings=settings)
    s.settings["preview_mode"] = normalize_preview_mode(
        s.settings.get("preview_mode"))
    s.settings["input_paused"] = False
    s.settings["material_inspect"] = False
    s.settings["material_inspect_requested"] = False
    s.coords = coords
    s.uvs = uvs
    s.tri_uv_bboxes = triangle_uv_bboxes(uvs)
    _session = s
    keys = tuple(s.settings.get("channel_keys", ()))
    targets = ",".join(
        "%s:%s" % (keys[i] if i < len(keys) else i, image.name)
        for i, image in enumerate(images))
    _log_line("GPU_PAINT_SPIKE_START channels=%d size=%d targets=%s"
              % (channel_count, images[0].size[0], targets))
    _add_handlers()
    return True


def stop_session():
    global _session
    _remove_handlers()
    if _session is not None:
        if _session.history_backend is not None:
            _session.history.clear(_session.history_backend)
        # Drop gpu refs; Blender frees them with the last reference.
        _session = None


def begin_stroke(x, y, pressure):
    s = _session
    if s is None or s.error is not None:
        return
    s.stroke_active = True
    s.stroke_t0 = time.perf_counter()
    s.first_dab_t = None
    s.last_dab_t = None
    s.dab_count = 0
    s.submit_times = []
    s.last_px = (x, y)
    s.leftover = 0.0
    s.stroke_dirty = None
    s.stroke_dirty_full = False
    s.dirty_ms = 0.0
    s.dab_queue.append((x, y, pressure))


def move_stroke(x, y, pressure, radius_px):
    s = _session
    if s is None or not s.stroke_active or s.error is not None:
        return
    x0, y0 = s.last_px
    stamp = s.settings.get("brush_stamp")
    spacing = (stamp.spacing_px if stamp is not None
               else dab_spacing(radius_px))
    positions, s.leftover = interpolate_dabs(
        x0, y0, x, y, spacing, s.leftover)
    for px, py, _t in positions:
        s.dab_queue.append((px, py, pressure))
    s.last_px = (x, y)


def end_stroke():
    s = _session
    if s is None or not s.stroke_active:
        return
    s.stroke_active = False
    # Pen-up is GPU-only. The draw callback drains any queued dabs and records
    # the stroke boundary, but performs no texture readback or Image writes.
    s.pending_finalize = True


def request_flush():
    """Queue a GPU->Image synchronization at the next owning-viewport draw.

    This is deliberately explicit/deferred: normal pen-up never calls it.
    Session exit and the panel's Flush button are the safe boundaries.
    """
    s = _session
    if s is None or s.error is not None:
        return False
    s.pending_flush = True
    return True


def request_history_action(action):
    """Queue atomic GPU tile undo/redo for the owning viewport draw."""
    s = _session
    action = str(action).upper()
    if (s is None or s.stroke_active or s.pending_finalize
            or action not in {'UNDO', 'REDO'}):
        return False
    s.pending_history_action = action
    return True


def history_counts():
    s = _session
    if s is None:
        return (0, 0)
    return (s.history.undo_count, s.history.redo_count)


def has_unflushed_changes():
    s = _session
    return bool(s is not None and (s.session_dirty is not None
                                   or s.session_dirty_full
                                   or s.stroke_dirty is not None
                                   or s.stroke_dirty_full
                                   or s.dab_queue))


def set_cursor(x, y):
    """Update the owning viewport's radius-scaled GPU brush reticle."""
    s = _session
    if s is not None:
        s.cursor = (float(x), float(y))


def cursor_position():
    """Current session reticle center, primarily a headless-test seam."""
    return _session.cursor if _session is not None else None


def update_stroke_settings(payloads, radius=None, hardness=None, opacity=None,
                           stamp=None):
    """Refresh values sampled at the next pen-down without restarting.

    The target images and channel order are fixed for a session, but brush
    values are not. This lets the N-panel remain useful between strokes.
    """
    s = _session
    if s is None or s.stroke_active:
        return False
    refreshed = list(payloads)
    if len(refreshed) != s.channels:
        raise ValueError("payload count must match session channels")
    s.payloads = refreshed
    s.target_batches = plan_target_batches(s.payloads)
    if radius is not None:
        s.settings["radius"] = float(radius)
    if hardness is not None:
        s.settings["hardness"] = float(hardness)
    if opacity is not None:
        s.settings["opacity"] = max(0.0, min(1.0, float(opacity)))
    s.settings["brush_stamp"] = stamp
    return True


def stroke_settings_snapshot():
    """Current payload/settings snapshot, primarily a headless-test seam."""
    if _session is None:
        return None
    return ([dict(item) for item in _session.payloads],
            dict(_session.settings))


def take_pending_pixels():
    """List of (numpy array, image_name) pairs — one per channel —
    awaiting the Image writes, or None. Called from the modal operator
    (never from a draw callback)."""
    s = _session
    if s is None or s.pending_pixels is None:
        return None
    pairs = s.pending_pixels
    s.pending_pixels = None
    s.flush_in_flight = False
    return pairs


def stats_log_path():
    """Stable per-user log file for the machine-readable stat lines.

    Console output is hidden by default on Windows, so every PROBE and
    STROKE line is also appended here for later collection.
    """
    import os
    return os.path.join(os.path.expanduser("~"), "gpu_paint_spike_stats.log")


def _log_line(text):
    print(text)
    try:
        import datetime
        with open(stats_log_path(), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().isoformat(
                timespec="seconds"), text))
    except OSError:
        pass  # logging must never break painting


def record_sync_stats(pixels_write_ms, image_update_ms):
    """Merge the CPU-side Image-write timings into the stroke stats and
    print the machine-readable per-stroke summary line."""
    global _last_stroke_stats
    s = _session
    if s is None or s.pending_gpu_stats is None:
        return
    stats = s.pending_gpu_stats
    s.pending_gpu_stats = None
    stats["pixels_write_ms"] = pixels_write_ms
    stats["image_update_ms"] = image_update_ms
    n = max(1, int(stats.get("channels", 1)))
    stats["pixels_write_avg_ch_ms"] = pixels_write_ms / n
    stats["syncback_total_ms"] = (stats.get("drain_ms", 0.0)
                                  + stats.get("fb_read_ms", 0.0)
                                  + stats.get("to_numpy_ms", 0.0)
                                  + pixels_write_ms + image_update_ms)
    _last_stroke_stats = stats
    _log_line("GPU_PAINT_SPIKE_STROKE "
              + " ".join("%s=%s" % (k, ("%.4f" % v) if isinstance(v, float)
                                    else v)
                         for k, v in sorted(stats.items())))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _add_handlers():
    global _handle_view, _handle_pixel
    try:
        if _handle_view is None:
            _handle_view = bpy.types.SpaceView3D.draw_handler_add(
                _draw_view, (), 'WINDOW', 'POST_VIEW')
        if _handle_pixel is None:
            _handle_pixel = bpy.types.SpaceView3D.draw_handler_add(
                _draw_pixel, (), 'WINDOW', 'POST_PIXEL')
    except Exception:
        # No viewport (background mode): the session still round-trips
        # logically; there is simply nothing to draw.
        _handle_view = None
        _handle_pixel = None


def _remove_handlers():
    global _handle_view, _handle_pixel
    for handle in (_handle_view, _handle_pixel):
        if handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except Exception:
                pass
    _handle_view = None
    _handle_pixel = None


@contextmanager
def _gpu_state_restored():
    """Save global gpu state, run the block, ALWAYS restore — draw
    callbacks and offscreen passes share global GPU state with all of
    Blender's own drawing (sibling-probed pattern). face_culling has no
    getter on 5.1.2; the documented default 'NONE' is restored. The
    viewport is captured too: GPUFrameBuffer binds do NOT save it
    (documented), so offscreen viewport_set calls would otherwise leak
    into the region's own drawing."""
    prior_blend = gpu.state.blend_get()
    prior_depth_test = gpu.state.depth_test_get()
    prior_depth_mask = gpu.state.depth_mask_get()
    prior_viewport = gpu.state.viewport_get()
    try:
        yield
    finally:
        gpu.state.blend_set(prior_blend)
        gpu.state.depth_test_set(prior_depth_test)
        gpu.state.depth_mask_set(prior_depth_mask)
        gpu.state.face_culling_set('NONE')
        gpu.state.viewport_set(*prior_viewport)


def _draw_view():
    s = _session
    if s is None or s.error is not None:
        return
    region = bpy.context.region
    rv3d = bpy.context.region_data
    if region is None or rv3d is None:
        return
    owning = (region.as_pointer() == s.region_ptr)
    try:
        with _gpu_state_restored():
            if owning:
                _ensure_gpu(s)
                _update_prepass(s, region, rv3d)
                if s.dab_queue:
                    _flush_dabs(s, region)
                if s.pending_finalize and not s.dab_queue:
                    _finalize_stroke_gpu(s)
                if s.pending_flush and not s.dab_queue \
                        and not s.pending_finalize:
                    _flush_session_gpu(s)
                if s.pending_history_action is not None \
                        and not s.dab_queue and not s.pending_finalize:
                    _apply_history_action(s)
                if not material_inspect_active():
                    _draw_composed_preview(s)
    except Exception:
        s.latch("draw failed")


def _draw_pixel():
    s = _session
    if s is None:
        return
    region = bpy.context.region
    if region is None or region.as_pointer() != s.region_ptr:
        return
    try:
        _draw_brush_reticle(s)
        _draw_stats_overlay(s)
    except Exception:
        # Text overlay must never take the viewport down; latch quietly.
        if s.error is None:
            s.latch("stats overlay failed")


# ---------------------------------------------------------------------------
# GPU setup + runtime capability probes
# ---------------------------------------------------------------------------


def _channel_blend(s, index):
    """Blend class of channel ``index``: 'MIX' channels accumulate
    premultiplied alpha (convert at both CPU<->GPU boundaries); 'ADD'
    (Height) accumulates raw signed values on an opaque canvas and
    round-trips byte-identically."""
    if 0 <= index < len(s.payloads):
        return s.payloads[index].get("blend", "MIX")
    return "MIX"


class _GPUTileBackend:
    """GPU-only snapshot backend for tile_undo.TileHistory."""

    def __init__(self, session):
        self.session = session
        keys = tuple(session.settings.get("channel_keys", ()))
        self.index_by_channel = {str(key): i for i, key in enumerate(keys)}

    def _index(self, key):
        try:
            return self.index_by_channel[key.channel]
        except KeyError as exc:
            raise tile_undo.TileHistoryError(
                "unknown GPU paint channel %r" % key.channel) from exc

    def _draw_copy(self, source, framebuffer, viewport, origin, scale):
        s = self.session
        with framebuffer.bind():
            framebuffer.viewport_set(*viewport)
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            sh = s.copy_shader
            sh.bind()
            sh.uniform_float("uv_origin", origin)
            sh.uniform_float("uv_scale", scale)
            sh.uniform_sampler("source_tex", source)
            s.copy_batch.draw(sh)

    def capture_tile(self, key):
        s = self.session
        index = self._index(key)
        tex = gpu.types.GPUTexture((key.width, key.height), format='RGBA16F')
        fb = gpu.types.GPUFrameBuffer(color_slots=(tex,))
        size = float(s.size)
        self._draw_copy(
            s.paint_texs[index], fb, (0, 0, key.width, key.height),
            (key.x / size, key.y / size),
            (key.width / size, key.height / size))
        return tile_undo.TileSnapshot(
            payload=tex, byte_size=key.width * key.height * 8)

    def restore_tile(self, key, snapshot):
        s = self.session
        index = self._index(key)
        self._draw_copy(
            snapshot.payload, s.single_fbs[index],
            (key.x, key.y, key.width, key.height),
            (0.0, 0.0), (1.0, 1.0))

    def release_tile(self, snapshot):
        # Releasing the TileSnapshot drops its last GPUTexture reference.
        return None


def _ensure_gpu(s):
    if s.gpu_ready:
        return
    import numpy as np
    from gpu_extras.batch import batch_for_shader

    if s.probe_lines is None:
        s.probe_lines = _probe_capabilities()
        for line in s.probe_lines:
            _log_line("GPU_PAINT_SPIKE_PROBE %s" % line)

    s.dab_shaders = [gpu.shader.create_from_info(
        dab_shader_create_info(len(indices), additive=(blend == 'ADD')))
        for blend, indices in s.target_batches]
    s.prepass_shader = gpu.shader.create_from_info(
        prepass_shader_create_info())
    s.preview_shader = gpu.shader.create_from_info(
        preview_shader_create_info())
    s.copy_shader = gpu.shader.create_from_info(copy_shader_create_info())

    # Paint textures: N RGBA16F accumulation targets on ONE framebuffer
    # (MRT). Readback goes through fb.read_color(..., slot=i, 'FLOAT')
    # which converts to float32 regardless of the attachment format.
    size = s.size
    n = s.channels

    # Every logical-layer binding owns an independent Blender Image. Seed all
    # GPU targets so an untouched channel round-trips losslessly.
    seeded_count = 0
    s.paint_texs = []
    for i, image_name in enumerate(s.image_names[:n]):
        tex = None
        image = bpy.data.images.get(image_name)
        if (image is not None and image.size[0] == size
                and image.size[1] == size):
            try:
                arr = np.empty(size * size * 4, dtype=np.float32)
                image.pixels.foreach_get(arr)
                if _channel_blend(s, i) != "ADD":
                    # MIX targets accumulate premultiplied (see
                    # premultiply_canvas); canvases store straight.
                    premultiply_canvas(arr)
                buf = gpu.types.Buffer('FLOAT', (size, size, 4),
                                       arr.reshape(size, size, 4))
                tex = gpu.types.GPUTexture((size, size),
                                           format='RGBA16F', data=buf)
                seeded_count += 1
            except Exception:
                traceback.print_exc()
        if tex is None:
            tex = gpu.types.GPUTexture((size, size), format='RGBA16F')
            tex.clear(format='FLOAT', value=(0.0, 0.0, 0.0, 0.0))
        s.paint_texs.append(tex)
    s.paint_fbs = [gpu.types.GPUFrameBuffer(
        color_slots=tuple(s.paint_texs[i] for i in indices))
        for _blend, indices in s.target_batches]
    s.single_fbs = [gpu.types.GPUFrameBuffer(color_slots=(tex,))
                    for tex in s.paint_texs]
    seed_line = "paint_tex_seeded_images=%d/%d" % (seeded_count, n)
    s.probe_lines.append(seed_line)
    _log_line("GPU_PAINT_SPIKE_PROBE %s" % seed_line)

    # VRAM: analytic (gpu.capabilities exposes no memory getters — the
    # probe reports whatever it finds). RGBA16F = 8 bytes/texel.
    per_ch_mb = size * size * 8 / (1024.0 * 1024.0)
    _log_line("GPU_PAINT_SPIKE_VRAM channels=%d format=RGBA16F size=%d "
              "per_channel_mb=%.1f total_mb=%.1f"
              % (n, size, per_ch_mb, per_ch_mb * n))

    s.batch_dabs = [batch_for_shader(
        shader, 'TRIS', {"pos": s.coords, "uv": s.uvs})
        for shader in s.dab_shaders]
    s.batch_prepass = batch_for_shader(
        s.prepass_shader, 'TRIS', {"pos": s.coords})
    s.batch_preview = batch_for_shader(
        s.preview_shader, 'TRIS', {"pos": s.coords, "uv": s.uvs})
    s.copy_batch = batch_for_shader(
        s.copy_shader, 'TRI_FAN', {
            "pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                    (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
            "uv": [(0.0, 0.0), (1.0, 0.0),
                   (1.0, 1.0), (0.0, 1.0)]})
    s.history_backend = _GPUTileBackend(s)
    s.gpu_ready = True

    # The research spike's destructive readback characterization is not part
    # of the production Impasto session; normal stroke stats remain enabled.


def _characterize_readback(s):
    """One-time per-session measurement: fb.read_color cost at 100% /
    25% / 5% of the texture AREA (slot 0), plus a full all-channels
    read. Logged as GPU_PAINT_SPIKE_READBACK_CHAR lines; best of 2."""
    import numpy as np
    size = s.size
    with s.paint_fb.bind():
        s.paint_fb.read_color(0, 0, 1, 1, 4, 0, 'FLOAT')   # warm/drain
        for frac_pct in (100, 25, 5):
            side = max(1, int(round(size * math.sqrt(frac_pct / 100.0))))
            best = None
            for _ in range(2):
                t0 = time.perf_counter()
                if _read_into_numpy:
                    arr = np.empty((side, side, 4), dtype=np.float32)
                    s.paint_fb.read_color(
                        0, 0, side, side, 4, 0, 'FLOAT',
                        data=gpu.types.Buffer('FLOAT', (side, side, 4), arr))
                else:
                    s.paint_fb.read_color(0, 0, side, side, 4, 0, 'FLOAT')
                dt = (time.perf_counter() - t0) * 1000.0
                best = dt if best is None else min(best, dt)
            _log_line("GPU_PAINT_SPIKE_READBACK_CHAR size=%d channels=1 "
                      "frac=%d%% rect=%dx%d ms=%.2f read_into_numpy=%s"
                      % (size, frac_pct, side, side, best,
                         "yes" if _read_into_numpy else "no"))
        if s.channels > 1:
            best = None
            for _ in range(2):
                t0 = time.perf_counter()
                for slot in range(s.channels):
                    if _read_into_numpy:
                        arr = np.empty((size, size, 4), dtype=np.float32)
                        s.paint_fb.read_color(
                            0, 0, size, size, 4, slot, 'FLOAT',
                            data=gpu.types.Buffer('FLOAT',
                                                  (size, size, 4), arr))
                    else:
                        s.paint_fb.read_color(0, 0, size, size, 4, slot,
                                              'FLOAT')
                dt = (time.perf_counter() - t0) * 1000.0
                best = dt if best is None else min(best, dt)
            _log_line("GPU_PAINT_SPIKE_READBACK_CHAR size=%d channels=%d "
                      "frac=100%% rect=%dx%d ms=%.2f read_into_numpy=%s "
                      "(all channels, full)"
                      % (size, s.channels, size, size, best,
                         "yes" if _read_into_numpy else "no"))


def _probe_capabilities():
    """Runtime probes of exactly the gpu features this spike leans on.
    Returns machine-readable "key=value" lines (also printed once with
    the GPU_PAINT_SPIKE_PROBE prefix). Runs inside the draw callback —
    a GPU context is guaranteed there."""
    import numpy as np
    from gpu_extras.batch import batch_for_shader
    from mathutils import Matrix

    lines = []

    try:
        from gpu import platform
        lines.append("backend=%s vendor=%s renderer=%s"
                     % (platform.backend_type_get(), platform.vendor_get(),
                        platform.renderer_get()))
    except Exception as e:
        lines.append("backend=unknown (%r)" % e)

    # RGBA16F color attachment on a custom framebuffer.
    try:
        tex = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        fb = gpu.types.GPUFrameBuffer(color_slots=(tex,))
        lines.append("rgba16f_color_fb=yes")
    except Exception as e:
        lines.append("rgba16f_color_fb=NO (%r)" % e)
        return lines   # everything else depends on this

    # Fixed-function ALPHA blending INTO a custom framebuffer
    # attachment: draw an opaque red quad, then a half-alpha blue quad,
    # read the center pixel back. (0.5, 0, 0.5) proves blending works
    # and ping-pong is unnecessary.
    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        quad = batch_for_shader(
            shader, 'TRI_FAN',
            {"pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                     (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]})
        ident = Matrix.Identity(4)
        with fb.bind():
            fb.viewport_set(0, 0, 8, 8)
            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            with gpu.matrix.push_pop():
                with gpu.matrix.push_pop_projection():
                    gpu.matrix.load_matrix(ident)
                    gpu.matrix.load_projection_matrix(ident)
                    shader.bind()
                    shader.uniform_float("color", (1.0, 0.0, 0.0, 1.0))
                    quad.draw(shader)
                    shader.uniform_float("color", (0.0, 0.0, 1.0, 0.5))
                    quad.draw(shader)
            buf = fb.read_color(4, 4, 1, 1, 4, 0, 'FLOAT')
        px = np.asarray(buf, dtype=np.float32).ravel()
        ok = (abs(px[0] - 0.5) < 0.06 and abs(px[2] - 0.5) < 0.06)
        lines.append("blend_alpha_into_offscreen_attachment=%s (center=%s)"
                     % ("yes" if ok else "NO",
                        np.round(px, 3).tolist()))
    except Exception as e:
        lines.append("blend_alpha_into_offscreen_attachment=NO (%r)" % e)

    # Stroke-end readback strategy (the 0.1.0 bottleneck: ~1050 ms
    # Buffer->numpy at 4K). Two probes against the blended 8x8 content
    # above, so correctness is checked against non-trivial values:
    # 1. fastest zero-copy Buffer->numpy ladder rung;
    # 2. fb.read_color(..., data=Buffer-wrapping-numpy): if the Buffer
    #    references the numpy memory (rather than copying), the read
    #    lands directly in the array foreach_set consumes and the
    #    conversion step disappears entirely.
    global _buffer_numpy_path, _read_into_numpy
    ref = None
    try:
        with fb.bind():
            small = fb.read_color(0, 0, 8, 8, 4, 0, 'FLOAT')
        ref = np.asarray(small.to_list(), dtype=np.float32).ravel()
        _buffer_numpy_path = probe_buffer_to_numpy_path(small, ref)
        lines.append("buffer_to_numpy_path=%s" % _buffer_numpy_path)
    except Exception as e:
        _buffer_numpy_path = "to_list_fallback"
        lines.append("buffer_to_numpy_path=to_list_fallback (probe: %r)" % e)
    try:
        target = np.zeros((8, 8, 4), dtype=np.float32)
        tbuf = gpu.types.Buffer('FLOAT', (8, 8, 4), target)
        with fb.bind():
            fb.read_color(0, 0, 8, 8, 4, 0, 'FLOAT', data=tbuf)
        ok = (ref is not None and bool(ref.any())
              and bool(np.allclose(target.ravel(), ref, atol=1e-3)))
        _read_into_numpy = ok
        lines.append("fb_read_into_numpy_buffer=%s"
                     % ("yes" if ok else
                        "NO (Buffer copies instead of wrapping, or "
                        "values mismatched)"))
    except Exception as e:
        _read_into_numpy = False
        lines.append("fb_read_into_numpy_buffer=NO (%r)" % e)

    # GPUTexture.read() on RGBA16F (vs fb.read_color): does the Buffer
    # convert to numpy, what dtype/length comes back, and does it export
    # the C buffer protocol (memoryview) — decides whether a half-float
    # read + CPU astype could ever beat the 'FLOAT' read (headless
    # measurement says no: float16->float32 astype alone costs ~119 ms
    # at 4K on this machine).
    try:
        buf = tex.read()
        try:
            mv = memoryview(buf)
            mv_desc = "memoryview format=%s itemsize=%d" % (mv.format,
                                                            mv.itemsize)
        except Exception as e:
            mv_desc = "memoryview unsupported (%r)" % e
        arr = np.asarray(buf)
        lines.append("gputexture_read_rgba16f=yes (numpy dtype=%s shape=%s; "
                     "%s)" % (arr.dtype, arr.shape, mv_desc))
    except Exception as e:
        lines.append("gputexture_read_rgba16f=NO (%r)" % e)

    # R32F color attachment (the prepass NDC-depth target).
    try:
        r32 = gpu.types.GPUTexture((8, 8), format='R32F')
        gpu.types.GPUFrameBuffer(color_slots=(r32,))
        lines.append("r32f_color_fb=yes")
    except Exception as e:
        lines.append("r32f_color_fb=NO (%r)" % e)

    # DEPTH_COMPONENT32F texture attached as depth_slot + clear.
    try:
        dt = gpu.types.GPUTexture((8, 8), format='DEPTH_COMPONENT32F')
        r32 = gpu.types.GPUTexture((8, 8), format='R32F')
        fb2 = gpu.types.GPUFrameBuffer(depth_slot=dt, color_slots=(r32,))
        with fb2.bind():
            fb2.clear(color=(1.0, 0.0, 0.0, 1.0), depth=1.0)
            dbuf = fb2.read_depth(4, 4, 1, 1)
        dval = float(np.asarray(dbuf).ravel()[0])
        lines.append("depth32f_attach_clear_read=yes (cleared=%.3f)" % dval)
    except Exception as e:
        lines.append("depth32f_attach_clear_read=NO (%r)" % e)

    # ---- v0.3.0 multi-channel probes ----------------------------------

    # How many color_slots does GPUFrameBuffer accept? (Try 8 then 4
    # then 2 — the panel's channel counts.)
    max_slots = 0
    slots_err = ""
    for count in (8, 4, 2):
        try:
            texs = [gpu.types.GPUTexture((8, 8), format='RGBA16F')
                    for _ in range(count)]
            gpu.types.GPUFrameBuffer(color_slots=tuple(texs))
            max_slots = count
            break
        except Exception as e:
            if not slots_err:
                slots_err = repr(e)
    lines.append("fb_max_color_slots=%d (of 8/4/2 tried%s)"
                 % (max_slots,
                    "" if max_slots == 8 else "; first failure: %s"
                    % slots_err))

    # R16F color attachment (scalar channels) + mixed-format MRT
    # (RGBA16F slot 0 + R16F slot 1 on one framebuffer).
    try:
        r16 = gpu.types.GPUTexture((8, 8), format='R16F')
        gpu.types.GPUFrameBuffer(color_slots=(r16,))
        lines.append("r16f_color_fb=yes")
    except Exception as e:
        lines.append("r16f_color_fb=NO (%r)" % e)
    try:
        fbm = gpu.types.GPUFrameBuffer(color_slots=(
            gpu.types.GPUTexture((8, 8), format='RGBA16F'),
            gpu.types.GPUTexture((8, 8), format='R16F')))
        with fbm.bind():
            fbm.clear(color=(0.25, 0.0, 0.0, 1.0))
            v0 = np.asarray(fbm.read_color(4, 4, 1, 1, 4, 0,
                                           'FLOAT').to_list()).ravel()
            v1 = np.asarray(fbm.read_color(4, 4, 1, 1, 1, 1,
                                           'FLOAT').to_list()).ravel()
        ok = abs(v0[0] - 0.25) < 0.01 and abs(v1[0] - 0.25) < 0.01
        lines.append("mixed_format_mrt_rgba16f_r16f=%s (slot0=%.3f "
                     "slot1=%.3f)" % ("yes" if ok else "NO",
                                      float(v0[0]), float(v1[0])))
    except Exception as e:
        lines.append("mixed_format_mrt_rgba16f_r16f=NO (%r)" % e)

    # Does fixed-function ALPHA blend apply to ALL MRT attachments?
    # gpu.state.blend_set has no per-attachment form, so if attachment 1
    # blends too this is a design constraint: one blend mode per stroke
    # across every channel. Shader routes DISTINCT values (att1 = rgb *
    # 0.5) so this also proves output-slot routing. Draw opaque red then
    # half-alpha blue: att0 -> (0.5, 0, 0.5); att1 -> (0.25, 0, 0.25)
    # iff blending applied there as well.
    try:
        mrt_sh = gpu.shader.create_from_info(_mrt_probe_shader_create_info())
        mrt_quad = batch_for_shader(
            mrt_sh, 'TRI_FAN',
            {"pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                     (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]})
        t0 = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        t1 = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        fb2 = gpu.types.GPUFrameBuffer(color_slots=(t0, t1))
        with fb2.bind():
            fb2.viewport_set(0, 0, 8, 8)
            fb2.clear(color=(0.0, 0.0, 0.0, 0.0))
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            mrt_sh.bind()
            mrt_sh.uniform_float("color", (1.0, 0.0, 0.0, 1.0))
            mrt_quad.draw(mrt_sh)
            mrt_sh.uniform_float("color", (0.0, 0.0, 1.0, 0.5))
            mrt_quad.draw(mrt_sh)
            a0 = np.asarray(fb2.read_color(4, 4, 1, 1, 4, 0,
                                           'FLOAT').to_list()).ravel()
            a1 = np.asarray(fb2.read_color(4, 4, 1, 1, 4, 1,
                                           'FLOAT').to_list()).ravel()
        ok0 = abs(a0[0] - 0.5) < 0.06 and abs(a0[2] - 0.5) < 0.06
        ok1 = abs(a1[0] - 0.25) < 0.06 and abs(a1[2] - 0.25) < 0.06
        lines.append("mrt_blend_alpha_all_attachments=%s (att0=%s att1=%s; "
                     "per-attachment blend NOT exposed by gpu.state — "
                     "one blend mode per stroke)"
                     % ("yes" if (ok0 and ok1) else "NO",
                        np.round(a0, 3).tolist(), np.round(a1, 3).tolist()))
    except Exception as e:
        lines.append("mrt_blend_alpha_all_attachments=NO (%r)" % e)

    # Sub-rect fb.read_color: paint left half red / right half blue via
    # viewport_set, then check that x/y offsets land on the right texels
    # and that a non-square sub-rect read into a numpy-wrapping Buffer
    # comes back in (h, w, 4) row-major order.
    try:
        tex3 = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        fb3 = gpu.types.GPUFrameBuffer(color_slots=(tex3,))
        with fb3.bind():
            fb3.viewport_set(0, 0, 8, 8)
            fb3.clear(color=(0.0, 0.0, 0.0, 0.0))
            gpu.state.blend_set('NONE')
            shader.bind()
            shader.uniform_float("color", (1.0, 0.0, 0.0, 1.0))
            with gpu.matrix.push_pop():
                with gpu.matrix.push_pop_projection():
                    gpu.matrix.load_matrix(ident)
                    gpu.matrix.load_projection_matrix(ident)
                    quad.draw(shader)
                    fb3.viewport_set(4, 0, 4, 8)
                    shader.uniform_float("color", (0.0, 0.0, 1.0, 1.0))
                    quad.draw(shader)
            left = np.asarray(fb3.read_color(1, 4, 1, 1, 4, 0,
                                             'FLOAT').to_list()).ravel()
            right = np.asarray(fb3.read_color(6, 4, 1, 1, 4, 0,
                                              'FLOAT').to_list()).ravel()
            sub = np.zeros((8, 4, 4), dtype=np.float32)   # h=8, w=4
            fb3.read_color(2, 0, 4, 8, 4, 0, 'FLOAT',
                           data=gpu.types.Buffer('FLOAT', (8, 4, 4), sub))
        ok_off = left[0] > 0.9 and left[2] < 0.1 \
            and right[2] > 0.9 and right[0] < 0.1
        # columns 2,3 red / 4,5 blue -> sub[:, :2] red, sub[:, 2:] blue
        ok_sub = (bool((sub[:, :2, 0] > 0.9).all())
                  and bool((sub[:, 2:, 2] > 0.9).all()))
        lines.append("fb_read_color_subrect=%s (offsets=%s; "
                     "into_numpy_hw4_order=%s)"
                     % ("yes" if (ok_off and ok_sub) else "NO",
                        "ok" if ok_off else "WRONG",
                        "ok" if ok_sub else "WRONG"))
    except Exception as e:
        lines.append("fb_read_color_subrect=NO (%r)" % e)

    # Queryable GPU memory info? (Expected absent; analytic VRAM
    # numbers are logged separately.)
    try:
        mem = []
        for name in dir(gpu.capabilities):
            if "mem" in name.lower() and name.endswith("_get"):
                try:
                    mem.append("%s=%r" % (name,
                                          getattr(gpu.capabilities, name)()))
                except Exception:
                    mem.append("%s=raises" % name)
        lines.append("gpu_capabilities_memory=%s"
                     % ("; ".join(mem) if mem else "none_exposed"))
    except Exception as e:
        lines.append("gpu_capabilities_memory=probe_failed (%r)" % e)

    # Direct sampling of a DEPTH texture through a FLOAT_2D sampler is
    # deliberately NOT relied on (backend convention minefield); the
    # spike samples its own R32F NDC-depth instead. Informational only.
    lines.append("depth_texture_direct_sampling=not_relied_on "
                 "(spike stores NDC depth in R32F color)")
    return lines


def _mrt_probe_shader_create_info():
    """Two-attachment MRT probe shader: slot 0 gets ``color``, slot 1
    gets ``color`` with rgb halved (distinct, so slot routing is
    proven). Positions are already NDC — no matrices."""
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('VEC4', "color")
    info.vertex_in(0, 'VEC3', "pos")
    info.fragment_out(0, 'VEC4', "fragColor")
    info.fragment_out(1, 'VEC4', "fragColor1")
    info.vertex_source("void main() { gl_Position = vec4(pos, 1.0); }\n")
    info.fragment_source(
        "void main()\n"
        "{\n"
        "    fragColor = color;\n"
        "    fragColor1 = vec4(color.rgb * 0.5, color.a);\n"
        "}\n")
    return info


# ---------------------------------------------------------------------------
# Depth prepass (per view change, NOT per dab)
# ---------------------------------------------------------------------------


def _prepass_state_key(s, region, rv3d, obj):
    m = rv3d.perspective_matrix
    mw = obj.matrix_world
    return (region.width, region.height,
            tuple(round(v, 6) for row in m for v in row),
            tuple(round(v, 6) for row in mw for v in row))


def _update_prepass(s, region, rv3d):
    obj = bpy.data.objects.get(s.obj_name)
    if obj is None:
        return
    key = _prepass_state_key(s, region, rv3d, obj)
    if key == s.prepass_key:
        return
    t0 = time.perf_counter()

    w = max(int(region.width), 8)
    h = max(int(region.height), 8)
    if s.depth_fb_size != (w, h):
        s.depth_color_tex = gpu.types.GPUTexture((w, h), format='R32F')
        s.depth_depth_tex = gpu.types.GPUTexture(
            (w, h), format='DEPTH_COMPONENT32F')
        s.depth_fb = gpu.types.GPUFrameBuffer(
            depth_slot=s.depth_depth_tex, color_slots=(s.depth_color_tex,))
        s.depth_fb_size = (w, h)

    # Capture the matrices the prepass renders with: the dab shader must
    # use the SAME values or the occlusion comparison is meaningless.
    s.view_proj = rv3d.perspective_matrix.copy()
    s.view = rv3d.view_matrix.copy()
    s.view_depth_plane = tuple(-s.view[2][i] for i in range(4))
    s.model = obj.matrix_world.copy()

    # Cache per-triangle screen bboxes for conservative dirty-rect
    # tracking (numpy, once per view change — the same matrices the
    # dab shader will project with, so the bound is honest).
    try:
        import numpy as np
        mvp = np.array(s.view_proj @ s.model, dtype=np.float32)
        s.tri_screen_bboxes, s.tri_unprojectable = triangle_screen_bboxes(
            s.coords, mvp, w, h)
    except Exception:
        s.tri_screen_bboxes = None
        s.tri_unprojectable = None

    with s.depth_fb.bind():
        s.depth_fb.viewport_set(0, 0, w, h)
        # The dab shader samples only pixels covered by this same mesh; a
        # large linear value remains a safe uncovered sentinel.
        s.depth_fb.clear(color=(1e30, 0.0, 0.0, 1.0), depth=1.0)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)
        gpu.state.face_culling_set('NONE')
        sh = s.prepass_shader
        sh.bind()
        sh.uniform_float("model_matrix", s.model)
        sh.uniform_float("view_proj_matrix", s.view_proj)
        sh.uniform_float("view_depth_plane", s.view_depth_plane)
        s.batch_prepass.draw(sh)
        # 1x1 read forces completion: this timing is TRUE prepass cost,
        # not submission cost (acceptable per view change).
        s.depth_fb.read_color(0, 0, 1, 1, 4, 0, 'FLOAT')
    s.prepass_ms = (time.perf_counter() - t0) * 1000.0
    s.prepass_key = key


# ---------------------------------------------------------------------------
# Dab dispatch
# ---------------------------------------------------------------------------


def _flush_dabs(s, region):
    if s.view_proj is None:
        return
    radius = float(s.settings.get("radius", 50.0))
    hardness = float(s.settings.get("hardness", 0.5))
    occlusion = bool(s.settings.get("occlusion", True))
    stamp = s.settings.get("brush_stamp")

    queue = s.dab_queue
    s.dab_queue = []
    now = time.perf_counter()
    if s.first_dab_t is None:
        s.first_dab_t = now

    # Conservative dirty-rect accumulation (pure numpy; timed so the
    # stats show what the tracking itself costs).
    t_dirty = time.perf_counter()
    if (s.tri_screen_bboxes is not None and s.tri_uv_bboxes is not None
            and queue):
        rect = dab_rect_union(queue, radius)
        bb = dirty_uv_bbox(s.tri_screen_bboxes, s.tri_unprojectable,
                           s.tri_uv_bboxes, rect)
        s.stroke_dirty = union_bbox(s.stroke_dirty, bb)
    elif queue:
        s.stroke_dirty_full = True   # no projection cache: full read
    s.dirty_ms += time.perf_counter() - t_dirty

    # Capture every touched channel tile once, immediately before its first
    # modification. Both before/after snapshots stay GPU-resident.
    if queue and s.history_backend is not None:
        dirty_rect = (uv_bbox_to_pixel_rect(bb, s.size)
                      if not s.stroke_dirty_full else None)
        if dirty_rect is not None or s.stroke_dirty_full:
            if s.stroke_transaction is None:
                s.stroke_transaction = s.history.begin_stroke(
                    s.history_backend, "GPU multi-channel stroke")
            rect = dirty_rect or (0, 0, s.size, s.size)
            keys = tuple(s.settings.get("channel_keys", ()))
            for i in range(s.channels):
                channel = keys[i] if i < len(keys) else str(i)
                s.stroke_transaction.touch_rect(
                    channel, rect, (s.size, s.size))

    for batch_index, ((blend, indices), fb, sh, draw_batch) in enumerate(
            zip(s.target_batches, s.paint_fbs, s.dab_shaders,
                s.batch_dabs)):
        with fb.bind():
            fb.viewport_set(0, 0, s.size, s.size)
            gpu.state.blend_set('ADDITIVE' if blend == 'ADD' else 'ALPHA')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            sh.bind()
            sh.uniform_float("model_matrix", s.model)
            sh.uniform_float("view_proj_matrix", s.view_proj)
            sh.uniform_float("view_depth_plane", s.view_depth_plane)
            sh.uniform_float("region_size",
                             (float(region.width), float(region.height)))
            sh.uniform_float("brush_radius_px", radius)
            sh.uniform_float("brush_hardness", hardness)
            sh.uniform_float("depth_epsilon", DEPTH_EPSILON)
            sh.uniform_float("depth_relative_epsilon",
                             visibility.DEFAULT_POLICY.relative_epsilon)
            sh.uniform_float("use_occlusion", 1.0 if occlusion else 0.0)
            sh.uniform_sampler("scene_depth_tex", s.depth_color_tex)
            for local, target_index in enumerate(indices):
                payload = s.payloads[target_index]
                value = tuple(payload.get("value", (0.0, 0.0, 0.0)))[:3]
                sh.uniform_float(
                    "brush_value%d" % local,
                    (value[0], value[1], value[2],
                     float(payload.get("strength", 1.0))))
            for (x, y, pressure) in queue:
                t0 = time.perf_counter()
                dab_radius, dab_opacity = (
                    stamp.values_at_pressure(pressure)
                    if stamp is not None else (radius, pressure))
                sh.uniform_float("brush_radius_px", dab_radius)
                sh.uniform_float("brush_center_px", (float(x), float(y)))
                stroke_opacity = float(s.settings.get("opacity", 1.0))
                sh.uniform_float("pressure", max(
                    0.0, min(1.0, dab_opacity * stroke_opacity)))
                draw_batch.draw(sh)
                s.submit_times.append(time.perf_counter() - t0)
    s.dab_count += len(queue)
    s.last_dab_t = time.perf_counter()


# ---------------------------------------------------------------------------
# Stroke-end readback (GPU side; the Image write happens in the modal op)
# ---------------------------------------------------------------------------


def _stroke_stats(s):
    """Cheap GPU-submission statistics; never forces GPU completion."""
    stats = {
        "size": s.size,
        "dabs": s.dab_count,
        "channels": s.channels,
        "prepass_ms": s.prepass_ms,
        "dirty_ms": s.dirty_ms * 1000.0,
        "deferred": 1,
    }
    if s.stroke_t0 is not None:
        stats["stroke_s"] = time.perf_counter() - s.stroke_t0
    if s.submit_times:
        stats["submit_avg_ms"] = (sum(s.submit_times)
                                  / len(s.submit_times)) * 1000.0
        stats["submit_max_ms"] = max(s.submit_times) * 1000.0
        stats["submit_total_ms"] = sum(s.submit_times) * 1000.0
    if (s.first_dab_t is not None and s.last_dab_t is not None
            and s.last_dab_t > s.first_dab_t and s.dab_count > 1):
        stats["dabs_per_s"] = ((s.dab_count - 1)
                               / (s.last_dab_t - s.first_dab_t))
    return stats


def _finalize_stroke_gpu(s):
    """Close one stroke without readback, drain, or Blender Image writes."""
    global _last_stroke_stats
    s.pending_finalize = False
    if s.stroke_dirty_full:
        s.session_dirty_full = True
    else:
        s.session_dirty = union_bbox(s.session_dirty, s.stroke_dirty)
    s.stroke_dirty = None
    s.stroke_dirty_full = False
    if s.stroke_transaction is not None:
        s.stroke_transaction.commit()
        s.stroke_transaction = None
    stats = _stroke_stats(s)
    _last_stroke_stats = stats
    _log_line("GPU_PAINT_SPIKE_STROKE "
              + " ".join("%s=%s" % (
                  k, ("%.4f" % v) if isinstance(v, float) else v)
                  for k, v in sorted(stats.items())))


def _apply_history_action(s):
    action = s.pending_history_action
    s.pending_history_action = None
    if s.history_backend is None:
        return
    if action == 'UNDO':
        record = s.history.undo(s.history_backend)
    else:
        record = s.history.redo(s.history_backend)
    if record is not None:
        # A later explicit/session-exit flush must persist the restored state.
        s.session_dirty_full = True


def _flush_session_gpu(s):
    """Read resident textures only at an explicit/session-exit boundary."""
    import numpy as np

    s.pending_flush = False
    if s.session_dirty is None and not s.session_dirty_full:
        return
    s.flush_in_flight = True
    size = s.size
    n = s.channels
    stats = _stroke_stats(s)
    stats["deferred"] = 0

    # Sub-rect decision: the stroke's accumulated conservative UV bbox,
    # unless tracking was unavailable, the user disabled it, or the
    # rect is no smaller than the full texture.
    use_subrect = bool(s.settings.get("subrect", True))
    rect = None
    if use_subrect and not s.session_dirty_full:
        rect = uv_bbox_to_pixel_rect(s.session_dirty, size)
    if rect is not None and rect[2] * rect[3] >= size * size:
        rect = None
    rx, ry, rw, rh = rect if rect is not None else (0, 0, size, size)
    stats["readback_rect"] = ("%dx%d" % (rw, rh)) if rect is not None \
        else "full"

    # Full-size CPU mirrors: sub-rect reads scatter into them and
    # foreach_set consumes them whole (Image.pixels has no partial write).
    # Every mirror starts from its binding-owned Image.
    if s.cpu_mirrors is None:
        s.cpu_mirrors = []
        for i in range(n):
            mirror = np.zeros(size * size * 4, dtype=np.float32)
            image = bpy.data.images.get(s.image_names[i])
            if (image is not None and image.size[0] == size
                    and image.size[1] == size):
                try:
                    image.pixels.foreach_get(mirror)
                    if _channel_blend(s, i) != "ADD":
                        # Mirrors live in framebuffer space: sub-rect
                        # reads scatter premultiplied MIX pixels into
                        # them, so the straight canvas seed converts up
                        # front (zeros are identical in both spaces).
                        premultiply_canvas(mirror)
                except Exception:
                    pass
            s.cpu_mirrors.append(mirror)

    t_read = 0.0
    t_conv = 0.0
    path = None
    alpha_max = [0.0] * n
    # 1x1 reads drain every batch before the atomic all-image finalize.
    t0 = time.perf_counter()
    for fb in s.paint_fbs:
        with fb.bind():
            fb.read_color(0, 0, 1, 1, 4, 0, 'FLOAT')
    stats["drain_ms"] = (time.perf_counter() - t0) * 1000.0

    for (_blend, indices), fb in zip(s.target_batches, s.paint_fbs):
        with fb.bind():
            for slot, target_index in enumerate(indices):
                view = s.cpu_mirrors[target_index].reshape(size, size, 4)
                sub = None
                direct = False
                t0 = time.perf_counter()
                if _read_into_numpy:
                    try:
                        if rect is not None:
                            sub = np.empty((rh, rw, 4), dtype=np.float32)
                            fb.read_color(
                            rx, ry, rw, rh, 4, slot, 'FLOAT',
                            data=gpu.types.Buffer('FLOAT', (rh, rw, 4),
                                                  sub))
                        else:
                            fb.read_color(
                            0, 0, size, size, 4, slot, 'FLOAT',
                            data=gpu.types.Buffer('FLOAT',
                                                  (size, size, 4), view))
                        direct = True
                        path = path or "read_into_numpy"
                    except Exception:
                        sub = None
                        direct = False
                if not direct:
                    buf = fb.read_color(rx, ry, rw, rh, 4, slot, 'FLOAT')
                    t_read += time.perf_counter() - t0
                    path = path or _buffer_numpy_path
                    t0 = time.perf_counter()
                    flat = buffer_to_numpy(buf, _buffer_numpy_path)
                    if rect is not None:
                        view[ry:ry + rh, rx:rx + rw] = flat.reshape(
                            rh, rw, 4)
                    else:
                        s.cpu_mirrors[target_index][:] = flat
                    t_conv += time.perf_counter() - t0
                else:
                    t_read += time.perf_counter() - t0
                    if sub is not None:
                        t0 = time.perf_counter()
                        view[ry:ry + rh, rx:rx + rw] = sub
                        t_conv += time.perf_counter() - t0
                # Diagnose the actual dirty-region attachment content, not
                # merely whether an Image.pixels write was attempted.
                region_pixels = (view[ry:ry + rh, rx:rx + rw]
                                 if rect is not None else view)
                if region_pixels.size:
                    alpha_max[target_index] = float(
                        region_pixels[..., 3].max())

    stats["fb_read_ms"] = t_read * 1000.0
    stats["fb_read_avg_ch_ms"] = t_read * 1000.0 / n
    stats["readback_path"] = path or "none"
    stats["to_numpy_ms"] = t_conv * 1000.0
    stats["alpha_max"] = ",".join("%.4f" % value
                                   for value in alpha_max)

    if DEBUG_COMPARE_READS:
        # 0.1.0 A/B probe: GPUTexture.read() (returns the attachment's
        # own format — half floats for RGBA16F). Costs a second full
        # GPU->CPU transfer; never part of the production path.
        try:
            t0 = time.perf_counter()
            s.paint_texs[0].read()
            stats["tex_read_ms"] = (time.perf_counter() - t0) * 1000.0
        except Exception:
            stats["tex_read_ms"] = float("nan")

    # Canvases store STRAIGHT alpha: MIX mirrors (premultiplied
    # framebuffer space) divide back out on a copy; ADD (Height)
    # mirrors sync raw, exactly as before.
    s.pending_pixels = [
        (mirror if _channel_blend(s, i) == "ADD"
         else unpremultiply_readback(mirror), name)
        for i, (mirror, name)
        in enumerate(zip(s.cpu_mirrors, s.image_names))]
    s.pending_gpu_stats = stats
    s.session_dirty = None
    s.session_dirty_full = False


# ---------------------------------------------------------------------------
# Viewport preview + stats overlay
# ---------------------------------------------------------------------------


def _draw_composed_preview(s):
    """Draw a low-cost PBR approximation from all resident textures."""
    if (not s.gpu_ready or s.preview_shader is None
            or s.batch_preview is None or s.view_proj is None
            or not s.paint_texs):
        return
    keys = tuple(s.settings.get("channel_keys", ()))
    by_key = {key: s.paint_texs[i] for i, key in enumerate(keys)
              if i < len(s.paint_texs)}
    fallback = s.paint_texs[0]
    shader = s.preview_shader
    shader.bind()
    shader.uniform_float("model_matrix", s.model)
    shader.uniform_float("view_proj_matrix", s.view_proj)
    try:
        camera_position = s.view.inverted().translation
    except Exception:
        camera_position = (0.0, 0.0, 10.0)
    shader.uniform_float("camera_position", camera_position)
    shader.uniform_float("preview_opacity", 1.0)
    shader.uniform_int("preview_mode", preview_mode_index(
        s.settings.get("preview_mode")))
    for key in GPU_PAINT_CHANNEL_KEYS:
        shader.uniform_float("has_" + key, 1.0 if key in by_key else 0.0)
        shader.uniform_sampler(key + "_tex", by_key.get(key, fallback))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(False)
    gpu.state.face_culling_set('BACK')
    s.batch_preview.draw(shader)


def _draw_brush_reticle(s):
    """Draw a screen-space circle using the exact GPU dab radius."""
    if (s.cursor is None or material_inspect_active()
            or material_inspect_requested()):
        return
    from gpu_extras.batch import batch_for_shader
    x, y = s.cursor
    radius = max(1.0, float(s.settings.get("radius", 50.0)))
    segments = max(32, min(128, int(radius * 0.8)))
    points = [(x + math.cos(i * math.tau / segments) * radius,
               y + math.sin(i * math.tau / segments) * radius)
              for i in range(segments)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'LINE_LOOP', {"pos": points})
    prior_blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.9))
        batch.draw(shader)
    finally:
        gpu.state.blend_set(prior_blend)


def _overlay_text_lines(s):
    dirty = " | unsaved GPU changes" if has_unflushed_changes() else ""
    lines = ["Impasto GPU paint — LMB paints  (RMB / Esc flushes + stops)"
             + dirty]
    if s.error is not None:
        lines.append("ERROR (see console): %s"
                     % s.error.strip().splitlines()[-1][:80])
        return lines
    if s.stroke_active:
        n = s.dab_count + len(s.dab_queue)
        line = "stroke: %d dabs" % n
        if s.submit_times:
            avg = sum(s.submit_times) / len(s.submit_times) * 1000.0
            line += "  |  submit avg %.3f ms" % avg
        lines.append(line)
    st = _last_stroke_stats
    if st:
        lines.append(
            "last stroke: %d dabs | %.1f dabs/s | submit avg %.3f ms "
            "(max %.3f)" % (st.get("dabs", 0), st.get("dabs_per_s", 0.0),
                            st.get("submit_avg_ms", 0.0),
                            st.get("submit_max_ms", 0.0)))
        if st.get("deferred", 0):
            lines.append("ch=%d | prepass %.2f ms | pen-up sync: deferred"
                         % (st.get("channels", 1),
                            st.get("prepass_ms", 0.0)))
        else:
            lines.append(
                "flush: ch=%d rect=%s | readback %.2f ms | pixels %.2f ms"
                % (st.get("channels", 1), st.get("readback_rect", "full"),
                   st.get("fb_read_ms", 0.0),
                   st.get("pixels_write_ms", 0.0)))
    return lines


def _draw_stats_overlay(s):
    import blf
    font_id = 0
    x, y = 20, 60
    blf.size(font_id, 12)
    blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
    for i, line in enumerate(reversed(_overlay_text_lines(s))):
        blf.position(font_id, x, y + i * 18, 0)
        blf.draw(font_id, line)
