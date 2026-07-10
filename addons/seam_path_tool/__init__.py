# SPDX-License-Identifier: GPL-2.0-or-later
"""Seam Path Tool — interactive shortest-path UV seam marking.

Click two (or more) points on a mesh; the shortest path between
consecutive clicks is marked as a UV seam, with a live preview of the
candidate path under the mouse.

Commit-performance design (1.3.0, see also core.py): a click commits the
edge list the preview already computed (preview == commit by
construction, at zero pathfinding cost), and the next anchor's
shortest-path tree is built INCREMENTALLY — a small synchronous slice at
commit time, then ~12 ms slices on modal TIMER ticks until complete.
Hovers inside the settled region read the partial tree (identical to the
full tree there); hovers beyond it fall back to an early-exit A* /
Dijkstra query (core.astar_path). The pick BVH is built once per tool
session (geometry cannot change while the modal runs).
"""

bl_info = {
    "name": "Seam Path Tool",
    "author": "Teo Asinari",
    "version": (1, 3, 0),
    "blender": (4, 2, 0),
    "location": "Edit Mode > Edge menu > Seam Path",
    "description": "Interactive shortest-path UV seam marking with live preview",
    "category": "Mesh",
}

import bpy
import bmesh
from bpy.props import BoolProperty, EnumProperty
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree

if "core" in locals():
    import importlib
    core = importlib.reload(core)
    picking = importlib.reload(picking)
    session = importlib.reload(session)
    preview = importlib.reload(preview)
else:
    from . import core
    from . import picking
    from . import session
    from . import preview

MODE_ITEMS = (
    ('LENGTH', "Length", "Weight edges by 3D length (geometric shortest path)"),
    ('TOPOLOGY', "Topology", "Unit weight per edge (fewest edges)"),
)

# Screen-space vertex picking radius, in pixels.
PICK_RADIUS_PX = 60.0

# Incremental shortest-path-tree fill tuning (seconds). Measured on a
# 300k-vert grid (Blender 5.1.2): the full single-source tree costs
# ~0.7 s in pure Python at ~400 settled verts/ms, so it is built in
# bounded slices instead of synchronously inside the click handler.
TREE_FILL_INITIAL_BUDGET = 0.015  # synchronous slice right after a commit
TREE_FILL_SLICE_BUDGET = 0.012    # per TIMER tick while filling
TREE_FILL_HOVER_BOOST = 0.008     # extra slice when an unsettled vert is hovered
TREE_FILL_TIMER_STEP = 0.02       # TIMER period while a fill is pending


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_endpoint_verts(bm):
    """Resolve the two endpoint verts for the non-modal operator.

    Preference order:
    1. Last two vertices in the selection history (active + previous-active).
    2. Exactly two selected vertices.
    Returns (v_from, v_to) or (None, error_message).
    """
    hist = [ele for ele in bm.select_history if isinstance(ele, bmesh.types.BMVert)]
    if len(hist) >= 2:
        return (hist[-2], hist[-1]), None
    selected = [v for v in bm.verts if v.select and not v.hide]
    if len(selected) == 2:
        return (selected[0], selected[1]), None
    return None, ("Select exactly 2 vertices (or click two vertices so the "
                  "selection history holds them)")


def _screen_candidates(context, obj, bm, coord2d, radius_px=PICK_RADIUS_PX):
    """Unhidden vertices within radius_px of a 2D region coordinate,
    sorted nearest-first: [(screen_dist, vert, co2d), ...].

    O(verts) per call, which is fine for the mousemove rates and mesh
    sizes this tool targets.
    """
    region = context.region
    rv3d = context.region_data
    mat = obj.matrix_world
    out = []
    for v in bm.verts:
        if v.hide:
            continue
        co2d = view3d_utils.location_3d_to_region_2d(region, rv3d, mat @ v.co)
        if co2d is None:
            continue
        d = (co2d - coord2d).length
        if d < radius_px:
            out.append((d, v, co2d))
    # BMVerts are not orderable; tie-break equal distances by index.
    out.sort(key=lambda item: (item[0], item[1].index))
    return out


def _xray_enabled(context):
    """Viewport X-ray state; with X-ray on, picking through the mesh is
    Blender's own selection convention, so occlusion is skipped."""
    space = getattr(context, "space_data", None)
    shading = getattr(space, "shading", None)
    if shading is None:
        return False
    if shading.type == 'WIREFRAME':
        return getattr(shading, "show_xray_wireframe", True)
    return getattr(shading, "show_xray", False)


def _build_pick_bvh(bm):
    """BVH over the VISIBLE faces of the edit bmesh, for occlusion-aware
    picking. Built from the live bmesh (obj.ray_cast sees only the stale
    pre-edit evaluated mesh in Edit Mode — verified on 5.1.2). Hidden
    faces are excluded so they neither snap nor occlude.

    :return: (BVHTree or None, [bm face index per BVH polygon index]).
    """
    coords = [v.co for v in bm.verts]
    polys = []
    face_map = []
    for f in bm.faces:
        if f.hide:
            continue
        polys.append(tuple(v.index for v in f.verts))
        face_map.append(f.index)
    if not polys:
        return None, []
    return BVHTree.FromPolygons(coords, polys), face_map


def _pick_vertex_occluded(context, obj, bm, bvh, face_map, coord2d,
                          radius_px=PICK_RADIUS_PX):
    """Occlusion-aware vertex pick (see picking.py for the semantics).

    Ray-casts the mouse ray against the visible-face BVH: on a hit, snaps
    to the hit face's vertex nearest to the mouse in screen space; on a
    miss, falls back to the screen-space nearest vertex that passes the
    epsilon-tolerant occlusion test.
    """
    region = context.region
    rv3d = context.region_data
    mat = obj.matrix_world
    inv = mat.inverted_safe()
    inv3 = inv.to_3x3()

    origin_w = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord2d)
    dir_w = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord2d)
    origin_o = inv @ origin_w
    dir_o = inv3 @ dir_w
    if dir_o.length_squared > 0.0:
        dir_o.normalize()
    hit_loc, _normal, poly_index, _dist = bvh.ray_cast(origin_o, dir_o)

    if poly_index is not None:
        bm.faces.ensure_lookup_table()
        face = bm.faces[face_map[poly_index]]
        best_v = None
        best_key = None
        for v in face.verts:
            if v.hide:
                continue
            co2d = view3d_utils.location_3d_to_region_2d(
                region, rv3d, mat @ v.co)
            screen_d = (co2d - coord2d).length if co2d is not None \
                else float('inf')
            # Screen distance decides; 3D distance to the hit point only
            # breaks ties / covers unprojectable verts.
            key = (screen_d, (v.co - hit_loc).length, v.index)
            if best_key is None or key < best_key:
                best_key, best_v = key, v
        if best_v is not None:
            return best_v
        # All the hit face's verts are hidden; fall through to the
        # occlusion-tested fallback.

    # Off-silhouette (or degenerate hit): nearest on screen, but reject
    # candidates whose view ray is blocked meaningfully before the vertex.
    candidates = []
    for _d, v, co2d in _screen_candidates(context, obj, bm, coord2d,
                                          radius_px):
        # Per-candidate ray origin (matters in orthographic views, where
        # the origin depends on the screen position).
        o_w = view3d_utils.region_2d_to_origin_3d(region, rv3d, co2d)
        delta_o = v.co - (inv @ o_w)
        dist = delta_o.length
        if dist <= 0.0:
            return v
        candidates.append((v, inv @ o_w, delta_o / dist, dist))
    return picking.first_visible_candidate(bvh, candidates)


# ---------------------------------------------------------------------------
# Non-modal operator (headless-testable entry point)
# ---------------------------------------------------------------------------

class MESH_OT_seam_path_mark(bpy.types.Operator):
    """Mark a UV seam along the shortest path between two selected vertices"""
    bl_idname = "mesh.seam_path_mark"
    bl_label = "Mark Seam Path"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Path Mode",
        items=MODE_ITEMS,
        default='LENGTH',
    )
    clear: BoolProperty(
        name="Clear",
        description="Clear seams along the path instead of marking them",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and context.mode == 'EDIT_MESH'

    def execute(self, context):
        obj = context.active_object
        me = obj.data
        bm = bmesh.from_edit_mesh(me)

        verts, err = _get_endpoint_verts(bm)
        if verts is None:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        v_from, v_to = verts

        edges = core.mark_seam_path(bm, v_from, v_to,
                                    mode=self.mode, clear=self.clear)
        if edges is None:
            self.report({'ERROR'},
                        "Vertices are in disconnected parts of the mesh")
            return {'CANCELLED'}
        if not edges:
            self.report({'WARNING'}, "Start and end vertex are the same")
            return {'CANCELLED'}

        bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        action = "Cleared" if self.clear else "Marked"
        self.report({'INFO'}, "%s seam along %d edge(s)" % (action, len(edges)))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Modal interactive operator (the stay-in-tool workflow)
# ---------------------------------------------------------------------------

class MESH_OT_seam_path_interactive(bpy.types.Operator):
    """Interactively mark UV seams: click points, shortest paths between
    consecutive clicks become seams. Ctrl+Click erases, Backspace undoes
    the last segment, RMB/Esc/Enter finishes"""
    bl_idname = "mesh.seam_path_interactive"
    bl_label = "Seam Path (Interactive)"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Path Mode",
        items=MODE_ITEMS,
        default='LENGTH',
    )
    erase: BoolProperty(
        name="Erase",
        description="Erase seams along paths instead of marking them "
                    "(Ctrl+Click temporarily inverts this)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and context.mode == 'EDIT_MESH'

    # -- helpers ---------------------------------------------------------

    def _bm(self, context):
        """(Re-)acquire the edit bmesh. Cheap; also survives undo pushes,
        which can swap the edit-mesh wrapper out from under us."""
        bm = bmesh.from_edit_mesh(context.active_object.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        return bm

    def _get_bvh(self, bm):
        """Visible-face BVH for occlusion picking, built ONCE per tool
        session. Geometry and hide flags cannot change while the tool runs
        (the modal swallows edit keys), and BVHTree.FromPolygons copies
        the coordinates at build time, so the tree stays valid even if an
        undo push swaps the edit-mesh wrapper — and at ~0.5 s for a
        300k-vert mesh we never want to pay the build twice."""
        if not self.pick_bvh_built:
            self.pick_bvh, self.pick_face_map = _build_pick_bvh(bm)
            self.pick_bvh_built = True
        return self.pick_bvh, self.pick_face_map

    def _pick(self, context, bm, coord2d):
        """Vertex the cursor snaps to; exactly what a click would commit.
        Occlusion-aware unless the viewport is in X-ray shading."""
        obj = context.active_object
        if not _xray_enabled(context):
            bvh, face_map = self._get_bvh(bm)
            if bvh is not None:
                return _pick_vertex_occluded(context, obj, bm, bvh,
                                             face_map, coord2d)
        # X-ray (Blender's own through-the-mesh selection convention) or a
        # faceless mesh (nothing can occlude): plain screen-space nearest.
        cands = _screen_candidates(context, obj, bm, coord2d)
        return cands[0][1] if cands else None

    def _reset_tree(self, context, bm, prefer_seams):
        """Start an INCREMENTAL single-source Dijkstra from the current
        anchor. Called once per committed anchor / in-tool undo (and on
        mark/erase toggles, which change the tie-break weighting).

        Only a small synchronous slice runs here (TREE_FILL_INITIAL_BUDGET,
        enough to settle the anchor's neighbourhood); the rest fills in
        ~12 ms slices on modal TIMER ticks, so a commit click returns
        immediately instead of paying the full ~0.7 s tree (300k verts) up
        front. Mousemove previews read the partial tree where settled and
        fall back to core.astar_path elsewhere (see _preview_edges)."""
        # Anchor / weighting changed: any cached hover path is stale.
        self.hover_edges = None
        self.hover_key = None
        anchor = self.session.current_anchor
        if anchor is None:
            self.tree = None
            self.tree_root = None
            self.tree_prefers_seams = False
            self._stop_fill_timer(context)
            return
        self.tree_root = bm.verts[anchor]
        self.tree_prefers_seams = prefer_seams
        self.tree = core.DijkstraSlicer(bm, self.tree_root, mode=self.mode,
                                        prefer_seams=prefer_seams)
        if self.tree.advance(time_budget=TREE_FILL_INITIAL_BUDGET):
            self._stop_fill_timer(context)
        else:
            self._ensure_fill_timer(context)

    def _ensure_fill_timer(self, context):
        if self._fill_timer is None:
            self._fill_timer = context.window_manager.event_timer_add(
                TREE_FILL_TIMER_STEP, window=context.window)

    def _stop_fill_timer(self, context):
        if self._fill_timer is not None:
            try:
                context.window_manager.event_timer_remove(self._fill_timer)
            except Exception:
                pass
            self._fill_timer = None

    def _advance_fill(self, context):
        """One background slice of the pending tree fill (TIMER handler)."""
        if self.tree is None or self.tree.done:
            self._stop_fill_timer(context)
            return
        if self.tree.advance(time_budget=TREE_FILL_SLICE_BUDGET):
            self._stop_fill_timer(context)

    def _preview_edges(self, context, bm, v_target, clearing):
        """Candidate path from the current anchor to v_target. This exact
        edge list is what a click commits (_commit reuses it via the
        hover cache, so preview == commit by construction and the commit
        itself does no pathfinding).

        Answered from the incremental tree where v_target is already
        settled (identical to the full tree there); otherwise the fill
        gets a small synchronous boost and, if the target is still
        unsettled, an early-exit A*/Dijkstra query (core.astar_path)
        answers just this target while the tree keeps filling on TIMER
        ticks.

        Erase candidates (clearing=True) use seam-preferring tie-breaking
        so retracing a marked segment erases exactly its edges (Bug 2);
        the tree is keyed on that flag and on the anchor root, so it is
        also restarted after a wrapper swap (stale BMesh references).
        """
        if self.session.current_anchor is None:
            return None
        v_anchor = bm.verts[self.session.current_anchor]
        if (self.tree is None or v_anchor is not self.tree_root
                or self.tree_prefers_seams != clearing):
            self._reset_tree(context, bm, clearing)

        key = (self.session.current_anchor, v_target.index, clearing)
        if self.hover_key == key:
            return self.hover_edges  # mouse resting / click after mousemove

        if not (self.tree.settled(v_target) or self.tree.done):
            # Nearby targets usually settle within one extra small slice.
            self.tree.advance(time_budget=TREE_FILL_HOVER_BOOST)
        if self.tree.settled(v_target) or self.tree.done:
            edges = core.path_from_tree(self.tree.tree, v_anchor, v_target)
        else:
            edges = core.astar_path(bm, v_anchor, v_target, mode=self.mode,
                                    prefer_seams=clearing)
        self.hover_key = key
        self.hover_edges = edges
        return edges

    def _refresh_committed_overlays(self, context, bm):
        """Project the session's overlay model (committed polylines +
        anchor dots) to world coordinates for the preview."""
        mat = context.active_object.matrix_world
        self.preview.committed_segments = [
            ([mat @ bm.verts[i].co for i in chain], is_erase)
            for chain, is_erase in self.session.overlay_polylines()]
        self.preview.anchor_coords = [
            mat @ bm.verts[i].co
            for i in self.session.overlay_anchor_indices()]

    def _update_preview(self, context, event):
        bm = self._bm(context)
        coord = Vector((event.mouse_region_x, event.mouse_region_y))
        v = self._pick(context, bm, coord)
        self.hover_index = v.index if v is not None else None
        clearing = self.erase != event.ctrl  # Ctrl inverts mark/erase

        mat = context.active_object.matrix_world
        coords = []
        if v is not None and self.session.current_anchor is not None:
            edges = self._preview_edges(context, bm, v, clearing)
            if edges:
                v_anchor = bm.verts[self.session.current_anchor]
                coords = [mat @ vv.co
                          for vv in _path_vert_chain(v_anchor, edges)]
        self.preview.path_coords = coords
        self._refresh_committed_overlays(context, bm)
        # Snap marker: always show which vertex a click would hit (drawn as
        # a white core dot in 3D plus a cyan pixel-space ring).
        self.preview.snap_coord = (mat @ v.co) if v is not None else None
        self.preview.set_status(
            mode=self.mode,
            anchors=len(self.session.anchors),
            segments=len(self.session.segments),
            erase_active=clearing)
        context.area.tag_redraw()

    def _commit(self, context, event):
        if self.hover_index is None:
            return
        bm = self._bm(context)
        v = bm.verts[self.hover_index]
        clearing = self.erase != event.ctrl  # Ctrl inverts mark/erase

        if self.session.current_anchor is None:
            # First anchor: nothing to commit yet.
            self.session.add_anchor(v.index)
            self._reset_tree(context, bm, clearing)
            return

        if v.index == self.session.current_anchor:
            return  # zero-length segment; ignore

        # Normally a hover-cache hit (the path the preview just showed);
        # recomputed only if preview state is stale (e.g. click with no
        # preceding mousemove after a Ctrl flip).
        edges = self._preview_edges(context, bm, v, clearing)
        if edges is None:
            self.report({'WARNING'},
                        "Vertex is in a disconnected part of the mesh")
            return
        v_anchor = bm.verts[self.session.current_anchor]
        self.session.commit_segment(v_anchor, edges, clearing)
        self._reset_tree(context, bm, clearing)
        self._refresh_committed_overlays(context, bm)

        bmesh.update_edit_mesh(context.active_object.data,
                               loop_triangles=False, destructive=False)
        # One undo step per committed segment, so Ctrl+Z after the tool
        # exits steps back segment by segment.
        bpy.ops.ed.undo_push(message="Seam Path Segment")

    def _undo_segment(self, context, event):
        bm = self._bm(context)
        had_segments = bool(self.session.segments)
        if not self.session.undo_last(bm):
            return
        self._reset_tree(context, bm, self.erase != event.ctrl)
        self._refresh_committed_overlays(context, bm)
        if had_segments:
            # Seam flags changed; before the first segment, Backspace only
            # removes the initial anchor (no mesh change, no undo step).
            bmesh.update_edit_mesh(context.active_object.data,
                                   loop_triangles=False, destructive=False)
            bpy.ops.ed.undo_push(message="Seam Path Segment Undo")
        context.area.tag_redraw()

    def _finish(self, context):
        # preview.stop() first (it never raises), so the draw handlers are
        # always removed even if the UI teardown below fails.
        self.preview.stop()
        self._stop_fill_timer(context)
        try:
            if context.area is not None:
                context.area.header_text_set(None)
                context.area.tag_redraw()
            if context.window is not None:
                context.window.cursor_modal_restore()
        except Exception:
            pass

    # -- operator entry points -------------------------------------------

    def invoke(self, context, event):
        if context.space_data is None or context.space_data.type != 'VIEW_3D':
            self.report({'ERROR'}, "Seam Path tool must be run in a 3D Viewport")
            return {'CANCELLED'}

        self.session = session.SeamSession()  # commit/erase/undo bookkeeping
        self.hover_index = None
        self.tree = None                  # incremental single-source Dijkstra
        self.tree_root = None             # BMVert the slicer is rooted at
        self.tree_prefers_seams = False   # tie-break weighting the slicer uses
        self.hover_edges = None           # last previewed path (commit reuses it)
        self.hover_key = None             # (anchor, target, clearing) it belongs to
        self._fill_timer = None           # TIMER driving the background fill
        self.pick_bvh = None              # visible-face BVH for occlusion
        self.pick_face_map = []
        self.pick_bvh_built = False       # built once per session (see _get_bvh)

        self.preview = preview.PathPreview()
        self.preview.start()

        context.window.cursor_modal_set('CROSSHAIR')
        context.area.header_text_set(
            "Seam Path — LMB: add point/commit segment | Ctrl+LMB: erase | "
            "Backspace: undo segment | Wheel/MMB: navigate | "
            "Enter/RMB/Esc: finish")
        context.window_manager.modal_handler_add(self)
        self._update_preview(context, event)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        # Guard the whole modal body: an unexpected exception must never
        # leave orphaned draw handlers or a stuck modal cursor.
        try:
            return self._modal_impl(context, event)
        except Exception:
            import traceback
            traceback.print_exc()
            self._finish(context)
            self.report({'ERROR'}, "Seam Path tool aborted (see console)")
            return {'CANCELLED'}

    def cancel(self, context):
        # Blender calls this if the modal is cancelled externally (e.g.
        # window close / file load); clean up the overlays here too.
        self._finish(context)

    def _modal_impl(self, context, event):
        # Let navigation through so the user can orbit/zoom mid-tool.
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}
        if event.type in {'NUMPAD_1', 'NUMPAD_2', 'NUMPAD_3', 'NUMPAD_4',
                          'NUMPAD_5', 'NUMPAD_6', 'NUMPAD_7', 'NUMPAD_8',
                          'NUMPAD_9', 'NUMPAD_0', 'NUMPAD_PERIOD'}:
            return {'PASS_THROUGH'}

        if event.type == 'TIMER':
            # Background slice of the next-anchor shortest-path tree
            # (see _reset_tree); bounded, so the UI stays responsive.
            self._advance_fill(context)
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            self._update_preview(context, event)
            return {'RUNNING_MODAL'}

        if event.type in {'LEFT_CTRL', 'RIGHT_CTRL'}:
            # Update the [ERASE] indicator in the help panel immediately.
            self._update_preview(context, event)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self._commit(context, event)
            self._update_preview(context, event)
            return {'RUNNING_MODAL'}

        if event.type == 'BACK_SPACE' and event.value == 'PRESS':
            self._undo_segment(context, event)
            # Refresh overlays (anchors retreated, segment overlay popped,
            # status counts changed).
            self._update_preview(context, event)
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC', 'RET', 'NUMPAD_ENTER'} \
                and event.value == 'PRESS':
            self._finish(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


def _path_vert_chain(v_from, edges):
    """Ordered vertex chain along a path's edge list, starting at v_from."""
    verts = [v_from]
    v = v_from
    for e in edges:
        v = e.other_vert(v)
        verts.append(v)
    return verts


# ---------------------------------------------------------------------------
# Menu / keymap / registration
# ---------------------------------------------------------------------------

def _edge_menu_draw(self, context):
    layout = self.layout
    layout.separator()
    layout.operator(MESH_OT_seam_path_interactive.bl_idname,
                    text="Seam Path (Interactive)")
    layout.operator(MESH_OT_seam_path_mark.bl_idname,
                    text="Mark Seam Path (2 Verts)")


_classes = (
    MESH_OT_seam_path_mark,
    MESH_OT_seam_path_interactive,
)

_keymaps = []


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_edit_mesh_edges.append(_edge_menu_draw)

    # Default keymap: Ctrl+Alt+E in mesh edit mode (unbound in stock 5.1;
    # Ctrl+E is the Edge menu, Alt+E the Extrude menu). Change it in
    # Preferences > Keymap if it clashes with another add-on.
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is not None:  # None in background mode
        km = kc.keymaps.new(name='Mesh', space_type='EMPTY')
        kmi = km.keymap_items.new(MESH_OT_seam_path_interactive.bl_idname,
                                  'E', 'PRESS', ctrl=True, alt=True)
        _keymaps.append((km, kmi))


def unregister():
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()

    bpy.types.VIEW3D_MT_edit_mesh_edges.remove(_edge_menu_draw)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
