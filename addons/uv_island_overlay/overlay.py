# SPDX-License-Identifier: GPL-2.0-or-later
"""GPU overlay: draws each face of the active mesh tinted by its UV
island color in the 3D viewport.

Design (mirrors seam_path_tool/preview.py):
- ALL gpu shader/batch work is deferred to draw time and exception-guarded,
  because it raises in `--background` mode. Enabling the overlay headlessly
  is a harmless no-op.
- Geometry extraction and island computation are pure (no gpu) and run
  either eagerly (refresh operator / enable) or lazily at the next draw
  after something marks the overlay dirty. Never per-frame: a clean
  overlay redraws from the cached batch.

Island sources (v1.1.0):
- 'UV'   — true UV-space connectivity (the actual current unwrap).
  Rebuilds happen at the next draw after a geometry update (bmesh path).
- 'SEAM' — seam-predicted islands (the partition the next Unwrap will
  produce). Rebuilds are LIVE while marking seams, via a debounced
  app-timer + cheap checksum, and use a vectorized numpy path over Mesh
  arrays (~5x faster than the bmesh path at 300k verts).

The SEAM live path deliberately never calls Object.update_from_editmode():
probed on 5.1.2, that tags the depsgraph and would make the refresh loop
self-triggering. Instead, edit-mode state is snapshotted with
bm.to_mesh(scratch) into a private non-depsgraph Mesh datablock, which
fires no depsgraph events (probed) and costs ~27 ms at 300k verts.
"""

import time
import traceback
from contextlib import contextmanager

import bpy
import gpu

# Overall tint strength. Baked into the vertex colors so the shader stays
# a stock builtin.
ALPHA = 0.4

# Fraction of the mesh bounding-box diagonal each triangle is pushed along
# its face normal, to sit just in front of the surface (avoids z-fighting
# while still being occluded correctly by nearer geometry).
NORMAL_OFFSET_FACTOR = 1.5e-3
NORMAL_OFFSET_MIN = 1e-5

# Live SEAM-mode refresh: recompute only after this much quiet since the
# last geometry update (a whole seam-marking burst costs ONE recompute),
# polling the debounce at LIVE_POLL_S. Per depsgraph tick the cost is
# O(1) (a timestamp); the checksum (~0.1 s at 300k verts) runs once per
# quiet period; the rebuild (~0.65 s at 300k verts) only when the
# checksum actually changed.
LIVE_QUIET_S = 0.30
LIVE_POLL_S = 0.10

# Private snapshot datablock for reading edit-mode state without tagging
# the depsgraph (leading dot keeps it out of UI datablock lists).
_SCRATCH_NAME = ".uv_island_overlay.snapshot"

from . import live


class _State:
    handle = None          # SpaceView3D draw handler
    enabled = False
    dirty = True           # geometry/batch needs rebuilding at next draw
    object_name = None     # active mesh object being overlaid
    source = 'SEAM'        # 'UV' or 'SEAM' (synced from the WM enum)
    island_count = 0
    coords = None          # triangle-soup positions (object space)
    colors = None          # per-vertex RGBA, flat per face
    batch = None           # gpu batch (lazily built, viewport only)
    shader = None
    seam_checksum = None   # checksum of the state the SEAM build used
    debounce = live.Debounce(LIVE_QUIET_S)
    # First draw-time error since the last enable/refresh, as a formatted
    # traceback string. Latched so the console is not spammed every frame,
    # but the failure stays LOUD: it is printed once and shown in the UI.
    last_draw_error = None


_state = _State()


def is_enabled():
    return _state.enabled


def island_count():
    return _state.island_count


def tracked_object_name():
    return _state.object_name


def active_source():
    return _state.source


def last_draw_error():
    return _state.last_draw_error


def mark_dirty():
    _state.dirty = True


def set_source(source, context=None):
    """Switch the island source ('UV'/'SEAM'). Invalidates and rebuilds
    immediately when the overlay is enabled."""
    if source == _state.source:
        return
    _state.source = source
    _state.seam_checksum = None
    _state.debounce.reset()
    if _state.enabled:
        refresh(context)


def on_tracked_geometry_update():
    """Called by the depsgraph handler when the tracked object reports a
    geometry update. UV mode keeps the classic behavior (dirty -> rebuild
    at next draw). SEAM mode goes through the debounced live pipeline so
    a burst of seam edits costs one recompute, after the burst."""
    if _state.source == 'SEAM':
        note_activity()
    else:
        mark_dirty()


# ---------------------------------------------------------------------------
# Geometry extraction (pure — safe headless)
# ---------------------------------------------------------------------------

def build_geometry(obj, source='UV'):
    """Extract (coords, colors, island_count) for a mesh object using the
    given island source. Kept for compatibility; refresh() uses _build to
    also receive the SEAM-state checksum."""
    coords, colors, count, _checksum = _build(obj, source)
    return coords, colors, count


def _build(obj, source):
    """(coords, colors, island_count, seam_checksum-or-None). SEAM uses
    the vectorized Mesh-array path with a loud bmesh fallback; UV uses
    the bmesh path (loop-UV comparisons do not vectorize cheaply)."""
    if source == 'SEAM':
        try:
            return _build_seam_arrays(obj)
        except Exception:
            print("[uv_island_overlay] fast seam-island path failed for "
                  "%r; falling back to bmesh:" % obj.name)
            traceback.print_exc()
            coords, colors, count = _build_bmesh(obj, force_seam=True)
            return coords, colors, count, None
    coords, colors, count = _build_bmesh(obj, force_seam=False)
    return coords, colors, count, None


def _snapshot_mesh(obj):
    """A Mesh datablock reflecting the object's CURRENT state, readable
    with foreach_get. In Edit Mode this snapshots the edit bmesh into a
    private scratch mesh — unlike update_from_editmode() this fires no
    depsgraph update (probed on 5.1.2), so the live refresh can never
    trigger itself. In Object Mode the datablock is already current."""
    if obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        scratch = bpy.data.meshes.get(_SCRATCH_NAME)
        if scratch is None:
            scratch = bpy.data.meshes.new(_SCRATCH_NAME)
        bm.to_mesh(scratch)   # ~27 ms at 300k verts; reusable in place
        return scratch
    return obj.data


def _remove_scratch_mesh():
    try:
        scratch = bpy.data.meshes.get(_SCRATCH_NAME)
        if scratch is not None:
            bpy.data.meshes.remove(scratch)
    except Exception:
        pass


def _checksum_from_arrays(n_verts, n_edges, n_faces, seam, co):
    return hash((n_verts, n_edges, n_faces, seam.tobytes(), co.tobytes()))


def seam_state_checksum(obj):
    """Cheap checksum of everything the SEAM overlay depends on: counts,
    seam flags and vertex positions (~0.1 s at 300k verts, dominated by
    the edit-mode snapshot + foreach_get). Selection/UV/shading changes
    do not affect it, so no-op depsgraph events skip the rebuild."""
    import numpy as np
    me = _snapshot_mesh(obj)
    n_verts = len(me.vertices)
    n_edges = len(me.edges)
    n_faces = len(me.polygons)
    seam = np.empty(n_edges, dtype=bool)
    if n_edges:
        me.edges.foreach_get("use_seam", seam)
    co = np.empty(n_verts * 3, dtype=np.float32)
    if n_verts:
        me.vertices.foreach_get("co", co)
    return _checksum_from_arrays(n_verts, n_edges, n_faces, seam, co)


def _build_seam_arrays(obj):
    """Vectorized SEAM-mode build over Mesh arrays: islands via
    islands.compute_islands_by_seam_arrays, triangle soup via
    loop_triangles. Measured 0.64 s at 302k faces vs 3.08 s for the
    bmesh path (islands 0.06 s, the rest is foreach_get + numpy).
    Returns (coords, colors, count, checksum); coords/colors are
    contiguous float32 numpy arrays (batch_for_shader accepts them)."""
    import numpy as np
    from . import islands as islands_mod

    me = _snapshot_mesh(obj)
    n_verts = len(me.vertices)
    n_edges = len(me.edges)
    n_faces = len(me.polygons)

    seam = np.empty(n_edges, dtype=bool)
    if n_edges:
        me.edges.foreach_get("use_seam", seam)
    co = np.empty(n_verts * 3, dtype=np.float32)
    if n_verts:
        me.vertices.foreach_get("co", co)
    checksum = _checksum_from_arrays(n_verts, n_edges, n_faces, seam, co)
    if n_faces == 0:
        return [], [], 0, checksum

    loop_edge = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("edge_index", loop_edge)
    loop_total = np.empty(n_faces, dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_total)
    loop_face = np.repeat(np.arange(n_faces, dtype=np.int32), loop_total)

    face_to_island, count = islands_mod.compute_islands_by_seam_arrays(
        n_faces, loop_edge, loop_face, seam)
    palette = np.asarray(islands_mod.island_colors(count, alpha=ALPHA),
                         dtype=np.float32).reshape(count, 4)

    co = co.reshape(-1, 3)
    diag = float(np.linalg.norm(co.max(axis=0) - co.min(axis=0)))
    offset = max(diag * NORMAL_OFFSET_FACTOR, NORMAL_OFFSET_MIN)

    normals = np.empty(n_faces * 3, dtype=np.float32)
    me.polygons.foreach_get("normal", normals)
    normals = normals.reshape(-1, 3)
    hide = np.empty(n_faces, dtype=bool)
    me.polygons.foreach_get("hide", hide)

    # loop_triangles is computed lazily on access in Blender 4.1+
    # (Mesh.calc_loop_triangles was removed).
    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    tris = me.loop_triangles
    n_tris = len(tris)
    tri_verts = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("vertices", tri_verts)
    tri_verts = tri_verts.reshape(-1, 3)
    tri_polys = np.empty(n_tris, dtype=np.int32)
    tris.foreach_get("polygon_index", tri_polys)

    visible = ~hide[tri_polys]
    tri_verts = tri_verts[visible]
    tri_polys = tri_polys[visible]

    coords = co[tri_verts.ravel()] \
        + np.repeat(normals[tri_polys] * offset, 3, axis=0)
    colors = np.repeat(palette[face_to_island[tri_polys]], 3, axis=0)
    return (np.ascontiguousarray(coords, dtype=np.float32),
            np.ascontiguousarray(colors, dtype=np.float32),
            count, checksum)


def _build_bmesh(obj, force_seam=False):
    """bmesh-path build: (coords, colors, island_count) for a mesh object.

    coords: object-space loop-triangle soup, each vertex pushed slightly
    along its face normal. colors: matching per-vertex RGBA (flat per
    face). Uses the edit bmesh in Edit Mode so the overlay tracks live
    edits, a throwaway bmesh otherwise. force_seam=True computes
    seam-predicted islands even when a UV layer exists (SEAM-mode
    fallback path).
    """
    import bmesh
    from . import islands as islands_mod

    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        owned = False
    else:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        owned = True
    try:
        # Topology edits in Edit Mode can leave stale (duplicate/holey)
        # face indices on the shared edit bmesh; the island lookup below
        # is indexed by face.index, so renumber first. O(faces), trivial
        # next to the union-find pass.
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()

        uv_layer = None if force_seam else bm.loops.layers.uv.active
        if uv_layer is not None:
            isl = islands_mod.compute_islands(bm, uv_layer)
        else:
            isl = islands_mod.compute_islands_by_seam(bm)
        if not isl:
            return [], [], 0

        palette = islands_mod.island_colors(len(isl), alpha=ALPHA)
        face_to_island = islands_mod.face_index_to_island(isl)

        # Normal offset scaled to the mesh so it works at any scene scale.
        diag = _bbox_diagonal(bm)
        offset = max(diag * NORMAL_OFFSET_FACTOR, NORMAL_OFFSET_MIN)

        coords = []
        colors = []
        for tri in bm.calc_loop_triangles():
            face = tri[0].face
            if face.hide:
                continue
            color = palette[face_to_island[face.index]]
            normal = face.normal
            for loop in tri:
                co = loop.vert.co + normal * offset
                coords.append((co.x, co.y, co.z))
                colors.append(color)
        return coords, colors, len(isl)
    finally:
        if owned:
            bm.free()


def _bbox_diagonal(bm):
    it = iter(bm.verts)
    try:
        first = next(it).co
    except StopIteration:
        return 0.0
    lo = [first.x, first.y, first.z]
    hi = list(lo)
    for v in it:
        co = v.co
        for k in range(3):
            c = co[k]
            if c < lo[k]:
                lo[k] = c
            elif c > hi[k]:
                hi[k] = c
    return ((hi[0] - lo[0]) ** 2 + (hi[1] - lo[1]) ** 2
            + (hi[2] - lo[2]) ** 2) ** 0.5


# ---------------------------------------------------------------------------
# Live SEAM-mode refresh (debounce is pure — live.py; this is the thin
# bpy.app.timers driver plus the checksum gate)
# ---------------------------------------------------------------------------

def note_activity(now=None):
    """Record that the tracked object's geometry MAY have changed and make
    sure the live timer is running. O(1); safe to call per depsgraph
    tick, even during a transform drag."""
    if now is None:
        now = time.monotonic()
    _state.debounce.note_change(now)
    _ensure_live_timer()


def _ensure_live_timer():
    try:
        if not bpy.app.timers.is_registered(_live_timer_cb):
            bpy.app.timers.register(_live_timer_cb,
                                    first_interval=LIVE_POLL_S)
    except Exception:
        # Headless test paths drive _live_tick directly instead.
        pass


def _stop_live_timer():
    try:
        if bpy.app.timers.is_registered(_live_timer_cb):
            bpy.app.timers.unregister(_live_timer_cb)
    except Exception:
        pass


def _live_timer_cb():
    try:
        return _live_tick(time.monotonic())
    except Exception:
        print("[uv_island_overlay] live refresh tick failed:")
        traceback.print_exc()
        return None


def _live_tick(now):
    """One debounce poll. Returns the next poll interval, or None to stop
    the timer. Takes the clock as an argument so tests can drive it with
    fake time (app timers never fire in --background)."""
    if not _state.enabled or _state.source != 'SEAM' \
            or not _state.debounce.pending:
        _state.debounce.reset()
        return None
    if not _state.debounce.try_fire(now):
        return LIVE_POLL_S
    # Quiet period over: one cheap checksum decides whether anything the
    # overlay depends on actually changed (seam flags, topology counts,
    # vertex positions). No-op events (mode switches, re-marking an
    # existing seam, selection-only updates) stop here.
    obj = None
    if _state.object_name is not None:
        obj = bpy.data.objects.get(_state.object_name)
    if obj is None or obj.type != 'MESH':
        return None
    try:
        checksum = seam_state_checksum(obj)
    except Exception:
        print("[uv_island_overlay] seam checksum failed:")
        traceback.print_exc()
        return None
    if checksum != _state.seam_checksum:
        refresh(None)
    return LIVE_POLL_S if _state.debounce.pending else None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _resolve_active_mesh(context):
    """Active mesh object, robust to sparse contexts (e.g. the property
    update firing from a popover): falls back from the given context to
    its view layer, then to bpy.context."""
    for ctx in (context, bpy.context):
        if ctx is None:
            continue
        obj = getattr(ctx, "active_object", None)
        if obj is None:
            view_layer = getattr(ctx, "view_layer", None)
            if view_layer is not None:
                obj = view_layer.objects.active
        if obj is not None and obj.type == 'MESH':
            return obj
    return None


def enable(context):
    """Turn the overlay on for the active mesh object and compute its
    island data immediately. Safe in background mode."""
    obj = _resolve_active_mesh(context)
    if obj is None:
        return False
    _state.object_name = obj.name
    _state.enabled = True
    _state.last_draw_error = None
    wm = getattr(bpy.context, "window_manager", None)
    source = getattr(wm, "uv_island_overlay_source", None)
    if source in {'UV', 'SEAM'}:
        _state.source = source
    refresh(context)
    if _state.handle is None:
        try:
            _state.handle = bpy.types.SpaceView3D.draw_handler_add(
                _draw, (), 'WINDOW', 'POST_VIEW')
        except Exception:
            # No viewport (background mode): stay enabled logically so the
            # toggle round-trips; there is simply nothing to draw.
            _state.handle = None
    _tag_redraw_view3d()
    return True


def disable():
    if _state.handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_state.handle, 'WINDOW')
        except Exception:
            pass
        _state.handle = None
    _stop_live_timer()
    _state.debounce.reset()
    _state.seam_checksum = None
    _remove_scratch_mesh()
    _state.enabled = False
    _state.dirty = True
    _state.object_name = None
    _state.island_count = 0
    _state.coords = None
    _state.colors = None
    _state.batch = None
    _state.last_draw_error = None
    _tag_redraw_view3d()


def refresh(context):
    """Recompute islands + geometry for the tracked object right now
    (pure work only) and invalidate the gpu batch."""
    obj = None
    if _state.object_name is not None:
        obj = bpy.data.objects.get(_state.object_name)
    if obj is None or obj.type != 'MESH':
        obj = context.active_object if context is not None else None
        if obj is None or obj.type != 'MESH':
            _state.island_count = 0
            _state.coords = None
            _state.colors = None
            _state.batch = None
            return False
        _state.object_name = obj.name
    _state.last_draw_error = None    # give drawing a fresh chance to log
    try:
        coords, colors, count, checksum = _build(obj, _state.source)
    except Exception:
        # refresh() is user/handler triggered (never per-frame), so a
        # failure here can afford to be loud every time.
        print("[uv_island_overlay] geometry rebuild failed for %r:"
              % _state.object_name)
        traceback.print_exc()
        coords, colors, count, checksum = None, None, 0, None
    _state.coords = coords
    _state.colors = colors
    _state.island_count = count
    _state.seam_checksum = checksum
    _state.batch = None      # rebuild from the new data at next draw
    _state.dirty = False
    _tag_redraw_view3d()
    return True


def _tag_redraw_view3d():
    wm = bpy.context.window_manager
    if wm is None:
        return
    try:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Drawing (viewport only; every gpu call guarded)
# ---------------------------------------------------------------------------

@contextmanager
def _gpu_state_restored():
    """Save global gpu state, run the draw block, ALWAYS restore.

    Draw callbacks share global GPU state with all of Blender's own
    drawing; any state set here and not restored leaks into other
    editors' subsequent draws (a past leak of depth_test LESS_EQUAL from
    this very overlay corrupted other overlays). The finally-clause
    guarantees restoration even when the wrapped block raises mid-draw —
    the callback's try/except guard alone would silently skip restore
    calls placed after the draw.

    Getters probed on 5.1.2: blend_get and depth_test_get exist, so the
    actual prior values are restored. face_culling_get does NOT exist;
    Blender's default ('NONE') is restored instead.
    """
    prior_blend = gpu.state.blend_get()
    prior_depth_test = gpu.state.depth_test_get()
    try:
        yield
    finally:
        gpu.state.blend_set(prior_blend)
        gpu.state.depth_test_set(prior_depth_test)
        # No face_culling_get on 5.1.2: restore the documented default.
        gpu.state.face_culling_set('NONE')


def _draw():
    if not _state.enabled or _state.object_name is None:
        return
    if _state.last_draw_error is not None:
        # A previous frame already failed and logged; don't retry every
        # frame (refresh()/re-enable clears the latch and tries again).
        return
    try:
        obj = bpy.data.objects.get(_state.object_name)
        if obj is None or obj.type != 'MESH':
            return

        if _state.dirty:
            if _state.source == 'SEAM':
                # SEAM rebuilds snapshot into a datablock, which must not
                # happen inside a draw callback — hand off to the live
                # timer (main loop) and keep drawing the cached batch.
                _state.dirty = False
                note_activity()
            else:
                # Something (depsgraph hook, mode change) invalidated the
                # cached geometry: rebuild once, not per-frame.
                _state.coords, _state.colors, _state.island_count = \
                    build_geometry(obj, _state.source)
                _state.batch = None
                _state.dirty = False

        # NOTE: explicit None/len test — coords may be a numpy array,
        # whose truth value is ambiguous.
        if _state.coords is None or len(_state.coords) == 0:
            return

        if _state.shader is None:
            # SMOOTH_COLOR (pos + per-vertex color) exists on 4.x/5.x —
            # verified against the 5.0/5.1 release notes: no builtin
            # color shaders were removed. Identical colors per face give
            # the flat look we want. FLAT_COLOR (used by Blender 5.1's
            # own bundled scripts) is the belt-and-braces fallback.
            try:
                _state.shader = gpu.shader.from_builtin('SMOOTH_COLOR')
            except SystemError:
                raise            # background mode: bail via outer guard
            except Exception:
                _state.shader = gpu.shader.from_builtin('FLAT_COLOR')

        if _state.batch is None:
            from gpu_extras.batch import batch_for_shader
            _state.batch = batch_for_shader(
                _state.shader, 'TRIS',
                {"pos": _state.coords, "color": _state.colors})

        # All state mutations live inside the guard: the priors are
        # captured first and restored in its finally-clause, so even an
        # exception mid-draw cannot leak blend/depth/culling state into
        # Blender's own subsequent drawing.
        with _gpu_state_restored():
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.face_culling_set('NONE')
            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(obj.matrix_world)
                _state.shader.bind()
                _state.batch.draw(_state.shader)
    except Exception:
        # Never let a draw-time error take down the viewport callback —
        # but never hide it either: latch it, print the traceback ONCE,
        # and let the UI panels show an error row (background test runs
        # latch quietly; there is no viewport to fix there). GPU state
        # needs no cleanup here: _gpu_state_restored() already restored
        # it on the way out.
        _state.last_draw_error = traceback.format_exc()
        if not bpy.app.background:
            print("[uv_island_overlay] viewport draw failed; overlay "
                  "suspended for %r until Refresh. Traceback:"
                  % _state.object_name)
            print(_state.last_draw_error)
