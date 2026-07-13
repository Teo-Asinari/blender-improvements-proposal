# SPDX-License-Identifier: GPL-2.0-or-later
"""GPU overlay: a mesh-relative visual guide for the voxel size.

Draws, in the tracked object's local space (world-transformed on the
GPU, so unapplied scale is VISIBLE — the guide stretches exactly like
the remesh result would appear):

- the object-space bounding box (always);
- a single sample cell — a wireframe cube of edge ``v`` anchored at the
  bounds min corner, colored by the current risk band;
- three representative grid slices (one mid-plane per axis) with lines
  spaced ``v``, each direction CAPPED at :data:`SLICE_MAX_LINES`; a
  slice that would exceed the cap is dropped entirely (a partial grid
  would lie about the density) — at extreme density the guide falls
  back to box + sample cell + annotation, never every voxel;
- a 2D annotation (blf): longest-axis cells + risk band (+ a "grid
  capped" note when slices were dropped).

Design (mirrors uv_island_overlay/overlay.py to the letter):
- ALL gpu shader/batch work is deferred to draw time and exception-
  guarded, because it raises in ``--background`` mode (probed on
  5.1.2). Enabling the overlay headlessly is a harmless no-op.
- The shader is built with GPUShaderCreateInfo + create_from_info ONLY
  (the legacy ``GPUShader(vert, frag)`` constructor is removed on
  5.1.2). Descriptor construction is pure bookkeeping and testable
  headless; only create_from_info touches the GPU.
- Draw-time errors are LATCHED: printed once, shown in the panel, and
  the callback goes quiet until refresh/re-enable — never per-frame
  console spam, never a dead viewport.
- Global GPU state is restored by the ``_gpu_state_restored`` guard's
  finally-clause even when the draw raises mid-block.
- Line geometry building is pure (no gpu, no bpy) and lives in
  module-level functions the headless suite exercises directly.
- Draw callbacks never evaluate the depsgraph or scan geometry: bounds
  come from core's stats cache and the per-draw estimate is pure
  arithmetic (core.current_estimate's documented contract).
"""

import math
import traceback
from contextlib import contextmanager

import bpy
import gpu

from . import core
from . import estimate

# Cap on grid lines PER DIRECTION per slice. 3 slices x 2 directions x
# 129 lines is ~1.5k line segments worst case — trivial to draw, and
# well past the density where individual cells stop being readable.
SLICE_MAX_LINES = 129

# Colors (RGBA). The sample cell reads the risk band; box and slices
# stay neutral so the guide works over any theme.
COLOR_BOX = (0.75, 0.75, 0.75, 0.55)
COLOR_SLICES = (0.40, 0.75, 0.95, 0.30)
RISK_COLORS = {
    estimate.RISK_GREEN: (0.35, 0.90, 0.45, 1.0),
    estimate.RISK_YELLOW: (0.95, 0.80, 0.25, 1.0),
    estimate.RISK_RED: (0.95, 0.30, 0.25, 1.0),
}

ANNOTATION_FONT_SIZE = 12

# GLSL stage bodies (create-info API); module-level so the headless
# suite can sanity-check them structurally (compiling is impossible in
# --background). One flat-color line shader covers box/cell/slices —
# the color is a push constant, so the three passes are uniform updates
# over their own batches, never shader switches.
VERT_SHADER_SRC = """
void main()
{
    gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);
}
"""

FRAG_SHADER_SRC = """
void main()
{
    fragColor = line_color;
}
"""


class _State:
    handle_3d = None       # SpaceView3D POST_VIEW draw handler
    handle_2d = None       # SpaceView3D POST_PIXEL draw handler (blf)
    enabled = False
    dirty = True           # guide geometry needs rebuilding at next draw
    object_name = None     # mesh object being annotated
    shader = None          # lazily compiled (viewport only)
    batches = None         # {'box':, 'cell':, 'slices': or None}
    built_key = None       # (bounds_min, bounds_max, voxel_size) built
    capped = False         # slices dropped at the last build
    annotation = None      # (text, risk, bounds_text) or None
    anchor_world = None    # world-space point the annotation hangs off
    # First draw-time error since the last enable/refresh, as a
    # formatted traceback. Latched: printed once, shown in the UI.
    last_draw_error = None


_state = _State()


def is_enabled():
    return _state.enabled


def tracked_object_name():
    return _state.object_name


def last_draw_error():
    return _state.last_draw_error


def mark_dirty():
    _state.dirty = True
    _state.last_draw_error = None    # give drawing a fresh chance


# ---------------------------------------------------------------------------
# Guide geometry (pure — safe headless, unit-tested directly)
# ---------------------------------------------------------------------------

_BOX_EDGES = (
    (0, 1), (1, 3), (3, 2), (2, 0),      # bottom rectangle
    (4, 5), (5, 7), (7, 6), (6, 4),      # top rectangle
    (0, 4), (1, 5), (2, 6), (3, 7),      # verticals
)


def _corner(lo, hi, i):
    return (hi[0] if i & 1 else lo[0],
            hi[1] if i & 2 else lo[1],
            hi[2] if i & 4 else lo[2])


def build_box_lines(lo, hi):
    """The 12 edges of the AABB as a flat list of 24 line endpoints."""
    corners = [_corner(lo, hi, i) for i in range(8)]
    pts = []
    for a, b in _BOX_EDGES:
        pts.append(corners[a])
        pts.append(corners[b])
    return pts


def build_cell_lines(lo, v):
    """The sample cell: a wireframe cube of edge ``v`` anchored at the
    bounds min corner (24 line endpoints)."""
    hi = (lo[0] + v, lo[1] + v, lo[2] + v)
    return build_box_lines(lo, hi)


def build_slice_lines(lo, hi, v, cap=SLICE_MAX_LINES):
    """(points, capped) — grid lines for three mid-axis slices.

    For each axis, a plane through the box center perpendicular to it,
    ruled with lines spaced ``v`` along the two in-plane axes. A slice
    whose line count would exceed ``cap`` in either direction is
    dropped entirely (capped=True): a partial grid would misrepresent
    the density, and the annotation carries the number instead.
    """
    pts = []
    capped = False
    dims = tuple(h - l for l, h in zip(lo, hi))
    for axis in range(3):
        b_ax = (axis + 1) % 3
        c_ax = (axis + 2) % 3
        n_b = int(math.ceil(dims[b_ax] / v)) if dims[b_ax] > 0.0 else 0
        n_c = int(math.ceil(dims[c_ax] / v)) if dims[c_ax] > 0.0 else 0
        if n_b + 1 > cap or n_c + 1 > cap:
            capped = True
            continue
        mid = 0.5 * (lo[axis] + hi[axis])

        def pt(t_b, t_c):
            p = [0.0, 0.0, 0.0]
            p[axis] = mid
            p[b_ax] = t_b
            p[c_ax] = t_c
            return tuple(p)

        # Lines parallel to b, stepped along c (and the mirror set).
        for i in range(n_c + 1):
            t_c = min(lo[c_ax] + i * v, hi[c_ax])
            pts.append(pt(lo[b_ax], t_c))
            pts.append(pt(hi[b_ax], t_c))
        for i in range(n_b + 1):
            t_b = min(lo[b_ax] + i * v, hi[b_ax])
            pts.append(pt(t_b, lo[c_ax]))
            pts.append(pt(t_b, hi[c_ax]))
    return pts, capped


def build_guide(lo, hi, v, cap=SLICE_MAX_LINES):
    """{'box':, 'cell':, 'slices':, 'capped':} — every line the guide
    draws, as flat endpoint lists in OBJECT space."""
    slices, capped = build_slice_lines(lo, hi, v, cap)
    return {
        "box": build_box_lines(lo, hi),
        "cell": build_cell_lines(lo, v),
        "slices": slices,
        "capped": capped,
    }


def transformed_box_edge_lengths(lo, hi, matrix):
    """Lengths of the drawn box's local X/Y/Z edges after transformation.

    matrix is any row-major 4x4 sequence. Translation is irrelevant;
    each local edge vector is transformed by one column of the linear
    3x3 part. This remains honest under rotation, non-uniform scale and
    shear: the three values are edge lengths, not a fictitious orthogonal
    world AABB.
    """
    dims = tuple(float(h) - float(l) for l, h in zip(lo, hi))
    return tuple(
        abs(dims[i]) * math.sqrt(sum(float(matrix[r][i]) ** 2
                                     for r in range(3)))
        for i in range(3)
    )


def format_bounds_dimensions(lengths):
    """Compact stable label for the viewport annotation (Blender units)."""
    return "Bounds X %.4g  Y %.4g  Z %.4g" % tuple(lengths)


# ---------------------------------------------------------------------------
# Enable / disable / refresh
# ---------------------------------------------------------------------------

def enable(context):
    """Turn the guide on for the active mesh object. Safe in background
    mode (draw handlers may fail to register — nothing to draw there)."""
    obj = context.active_object if context is not None else None
    if obj is None or obj.type != 'MESH':
        return False
    _state.object_name = obj.name
    _state.enabled = True
    _state.dirty = True
    _state.last_draw_error = None
    if _state.handle_3d is None:
        try:
            _state.handle_3d = bpy.types.SpaceView3D.draw_handler_add(
                _draw_3d, (), 'WINDOW', 'POST_VIEW')
            _state.handle_2d = bpy.types.SpaceView3D.draw_handler_add(
                _draw_2d, (), 'WINDOW', 'POST_PIXEL')
        except Exception:
            # No viewport (background mode): stay enabled logically so
            # the toggle round-trips; there is simply nothing to draw.
            _state.handle_3d = None
            _state.handle_2d = None
    return True


def disable():
    for attr, kind in (("handle_3d", 'POST_VIEW'),
                       ("handle_2d", 'POST_PIXEL')):
        handle = getattr(_state, attr)
        if handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle,
                                                          'WINDOW')
            except Exception:
                pass
            setattr(_state, attr, None)
    _state.enabled = False
    _state.dirty = True
    _state.object_name = None
    _state.batches = None
    _state.built_key = None
    _state.capped = False
    _state.annotation = None
    _state.anchor_world = None
    _state.last_draw_error = None


# ---------------------------------------------------------------------------
# Drawing (viewport only; every gpu call guarded)
# ---------------------------------------------------------------------------

def _shader_create_info():
    """GPUShaderCreateInfo descriptor for the flat-color line shader.

    Descriptor construction/population is pure bookkeeping and works in
    --background (probed on 5.1.2 by the sibling overlay) — only
    gpu.shader.create_from_info actually touches the GPU and raises
    SystemError headless — so the test suite can build (and structurally
    check) this without a GPU. The VEC4 "line_color" push constant lets
    box/cell/slices share one shader across three uniform updates.
    """
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "ModelViewProjectionMatrix")
    info.push_constant('VEC4', "line_color")
    info.vertex_in(0, 'VEC3', "pos")
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(VERT_SHADER_SRC)
    info.fragment_source(FRAG_SHADER_SRC)
    return info


def _create_shader():
    """Compile the line shader. GPU work — draw time only, behind the
    error latch (headless this raises SystemError; a GLSL error in the
    GUI surfaces once via the panel's error row). The legacy raw-GLSL
    constructor is NOT an option: probed on 5.1.2 it raises TypeError;
    create_from_info is the supported API."""
    return gpu.shader.create_from_info(_shader_create_info())


@contextmanager
def _gpu_state_restored():
    """Save global gpu state, run the draw block, ALWAYS restore.

    Draw callbacks share global GPU state with all of Blender's own
    drawing; state set here and not restored leaks into other editors'
    draws. The finally-clause guarantees restoration even when the
    wrapped block raises mid-draw. Getters probed on 5.1.2 (by the
    sibling overlay): blend_get and depth_test_get exist.
    """
    prior_blend = gpu.state.blend_get()
    prior_depth_test = gpu.state.depth_test_get()
    try:
        yield
    finally:
        gpu.state.blend_set(prior_blend)
        gpu.state.depth_test_set(prior_depth_test)


def _resolve_draw_inputs():
    """(obj, stats, voxel_size, est) for the current draw, or None.

    Draw-safe: cache lookups, RNA reads and pure arithmetic only (the
    core.current_estimate contract). Returns None when there is nothing
    valid to draw — NOT an error state (the panel explains missing
    stats / invalid sizes)."""
    if _state.object_name is None:
        return None
    obj = bpy.data.objects.get(_state.object_name)
    if obj is None or obj.type != 'MESH':
        return None
    scene = bpy.context.scene
    settings = getattr(scene, "calipers", None)
    prefer = getattr(settings, "guide_source", 'AUTO')
    kind = core.resolve_guide_context(obj, prefer)
    if kind is None:
        return None
    stats = core.cached_stats(obj.name, kind)
    if stats is None:
        return None
    if kind == core.CONTEXT_MESH:
        v = obj.data.remesh_voxel_size
    else:
        mod = core.find_voxel_modifier(obj)
        if mod is None:
            return None
        v = mod.voxel_size
    if not (v > 0.0 and math.isfinite(v)):
        return None
    est = core.current_estimate(obj, kind, settings)
    return obj, stats, v, est


def _draw_3d():
    if not _state.enabled or _state.object_name is None:
        return
    if _state.last_draw_error is not None:
        # A previous frame already failed and logged; don't retry every
        # frame (mark_dirty()/re-enable clears the latch).
        return
    try:
        resolved = _resolve_draw_inputs()
        if resolved is None:
            _state.annotation = None
            return
        obj, stats, v, est = resolved

        # Annotation payload for the POST_PIXEL pass (computed here so
        # both passes agree on one resolution of the inputs).
        if est is not None:
            text = "{:,} cells".format(est.longest_axis_cells)
            bounds_text = None
            if getattr(settings, "show_bounds_dimensions", False):
                lengths = transformed_box_edge_lengths(
                    stats.bounds_min, stats.bounds_max, obj.matrix_world)
                bounds_text = format_bounds_dimensions(lengths)
            _state.annotation = (text, est.risk, bounds_text)
        else:
            _state.annotation = None
        lo, hi = stats.bounds_min, stats.bounds_max
        center_top = (0.5 * (lo[0] + hi[0]),
                      0.5 * (lo[1] + hi[1]), hi[2])
        from mathutils import Vector
        _state.anchor_world = obj.matrix_world @ Vector(center_top)

        key = (lo, hi, round(v, 12))
        if _state.dirty or key != _state.built_key or \
                _state.batches is None:
            if _state.shader is None:
                # Lazily compiled here so a failure (headless
                # SystemError, GLSL error) lands in the loud latch.
                _state.shader = _create_shader()
            from gpu_extras.batch import batch_for_shader
            guide = build_guide(lo, hi, v)
            _state.capped = guide["capped"]
            _state.batches = {
                name: (batch_for_shader(_state.shader, 'LINES',
                                        {"pos": guide[name]})
                       if guide[name] else None)
                for name in ("box", "cell", "slices")
            }
            _state.built_key = key
            _state.dirty = False

        cell_color = RISK_COLORS.get(
            est.risk if est is not None else estimate.RISK_RED,
            RISK_COLORS[estimate.RISK_RED])

        # All state mutations live inside the guard: priors are
        # captured first and restored in its finally-clause.
        with _gpu_state_restored():
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('LESS_EQUAL')
            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(obj.matrix_world)
                shader = _state.shader
                shader.bind()
                # Explicit MVP from gpu.matrix state (projection @ view
                # @ object world, thanks to the multiply above) — same
                # explicit binding as the sibling overlay.
                mvp = (gpu.matrix.get_projection_matrix()
                       @ gpu.matrix.get_model_view_matrix())
                shader.uniform_float("ModelViewProjectionMatrix", mvp)
                for name, color in (("slices", COLOR_SLICES),
                                    ("box", COLOR_BOX),
                                    ("cell", cell_color)):
                    batch = _state.batches.get(name)
                    if batch is not None:
                        shader.uniform_float("line_color", color)
                        batch.draw(shader)
    except Exception:
        # Never let a draw-time error take down the viewport callback —
        # but never hide it either: latch it, print ONCE, panel shows
        # an error row. GPU state needs no cleanup here:
        # _gpu_state_restored() already restored it on the way out.
        _state.last_draw_error = traceback.format_exc()
        if not bpy.app.background:
            print("[calipers] viewport guide draw failed; suspended "
                  "for %r until refresh. Traceback:"
                  % _state.object_name)
            print(_state.last_draw_error)


def _draw_2d():
    """The blf annotation: longest-axis cells + risk band, hung off the
    top of the object's bounding box. Text only — no gpu.state
    mutations, so no guard is needed; errors ride the same latch."""
    if not _state.enabled or _state.annotation is None:
        return
    if _state.last_draw_error is not None:
        return
    try:
        import blf
        from bpy_extras import view3d_utils

        region = bpy.context.region
        rv3d = bpy.context.region_data
        if region is None or rv3d is None or _state.anchor_world is None:
            return
        co2d = view3d_utils.location_3d_to_region_2d(
            region, rv3d, _state.anchor_world)
        if co2d is None:
            return
        text, risk, bounds_text = _state.annotation
        label = "%s - %s" % (text, risk.title())
        if _state.capped:
            label += "  (grid capped)"
        ui_scale = 1.0
        try:
            ui_scale = bpy.context.preferences.system.ui_scale
        except Exception:
            pass
        font_id = 0
        blf.size(font_id, ANNOTATION_FONT_SIZE * ui_scale)  # 5.1: 2 args
        width, height = blf.dimensions(font_id, label)
        blf.position(font_id, co2d.x - width * 0.5,
                     co2d.y + height * 0.8, 0.0)
        r, g, b, _a = RISK_COLORS.get(risk, RISK_COLORS['RED'])
        blf.color(font_id, r, g, b, 1.0)
        blf.draw(font_id, label)
        if bounds_text:
            bw, bh = blf.dimensions(font_id, bounds_text)
            blf.position(font_id, co2d.x - bw * 0.5,
                         co2d.y + height * 0.8 + bh * 1.35, 0.0)
            blf.color(font_id, 0.9, 0.9, 0.9, 1.0)
            blf.draw(font_id, bounds_text)
    except Exception:
        _state.last_draw_error = traceback.format_exc()
        if not bpy.app.background:
            print("[calipers] guide annotation draw failed; suspended "
                  "until refresh. Traceback:")
            print(_state.last_draw_error)
