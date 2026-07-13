# SPDX-License-Identifier: GPL-2.0-or-later
"""Calipers — scale-aware voxel-remesh preview and safety.

Voxel remeshing can launch an unexpectedly expensive operation when the
current voxel size is badly mismatched with the mesh: the native value
is an OBJECT-space distance (probed on 5.1.2 — an unapplied 2x scale
does not change the result), the datablock default (0.1) knows nothing
about your mesh, and the number alone communicates neither resolution
nor cost. Calipers puts a preflight in front of both native entry
points — kept strictly separate, because they have different geometry
sources:

- the destructive **Voxel Remesh operation**
  (``bpy.ops.object.voxel_remesh``, settings on the Mesh datablock;
  probed: it uses ORIGINAL mesh data and ignores modifiers/shape keys),
  via a confirming wrapper that shows the estimate first. The native
  Ctrl-R keymap is untouched.
- the non-destructive **Remesh modifier** in VOXEL mode (input = the
  stack before it), via "Add Remesh Modifier (Safe)": the modifier
  arrives PENDING (``show_viewport``/``show_render`` off — probed: set
  in the same operator execution, the remesh never evaluates) with a
  bounds-derived initial voxel size, and first computes when you
  confirm — with the estimate on screen BEFORE the toggle, because the
  toggle itself is the expensive event.

Plus: a sidebar panel with live scale-aware estimates (longest-axis
cells, saturating bounding-cell score, relative surface score, risk
band, unapplied/non-uniform/shear warnings), a Set-from-World-Target
helper with a conservative documented conversion, and a GPU viewport
guide (sample cell + capped grid slices) so the cell size is visible
against the mesh instead of being an abstract number.

What an add-on cannot cover (research doc): the native Add Modifier
menu still evaluates on add, and direct ``bpy.ops.object.voxel_remesh``
calls (Ctrl-R included) still bypass any wrapper. Those need
Blender-core work; this add-on is the §5 prototype of the interaction.

Settings live in a SCENE-level PropertyGroup (``bpy.types.Scene
.calipers``): risk thresholds and the world-target size are part of how
you work on the asset and must survive save/load (kiln's rationale).
The overlay toggle is runtime module state (uv_island_overlay's
rationale) — a viewport aid should not persist into a saved file.
"""

bl_info = {
    "name": "Calipers",
    "author": "Teo Asinari",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Calipers tab",
    "description": "Scale-aware voxel-remesh preview and safety: cost "
                   "estimates, scale warnings, safe pending Remesh "
                   "modifier, confirming voxel-remesh wrapper, and a "
                   "mesh-relative viewport guide for the voxel size",
    "category": "Mesh",
}

import traceback

import bpy
from bpy.app.handlers import persistent
from bpy.props import (EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)

if "core" in locals():
    import importlib
    estimate = importlib.reload(estimate)
    live = importlib.reload(live)
    core = importlib.reload(core)
    overlay = importlib.reload(overlay)
else:
    from . import estimate
    from . import live
    from . import core
    from . import overlay


# ---------------------------------------------------------------------------
# Settings (scene-level; see the module docstring for the rationale)
# ---------------------------------------------------------------------------

def _est_display_changed(self, context):
    """Threshold/target edits change what the overlay annotates —
    repaint only, no stats rebuild (the cache is geometry-only)."""
    overlay.mark_dirty()
    _tag_redraw_view3d()


class CalipersSettings(bpy.types.PropertyGroup):
    yellow_exp: FloatProperty(
        name="Yellow At (log10 cells)",
        description="Risk turns YELLOW when log10 of the bounding-cell "
                    "score reaches this (default 7 ~= 215 cells per "
                    "axis). A relative-cost band, not a memory promise",
        default=estimate.DEFAULT_YELLOW_EXP, min=0.0, max=18.9,
        update=_est_display_changed,
    )
    red_exp: FloatProperty(
        name="Red At (log10 cells)",
        description="Risk turns RED when log10 of the bounding-cell "
                    "score reaches this (default 9 ~= 1000 cells per "
                    "axis). Warnings never hard-block: confirming is "
                    "always allowed",
        default=estimate.DEFAULT_RED_EXP, min=0.0, max=18.9,
        update=_est_display_changed,
    )
    world_target: FloatProperty(
        name="World Target",
        description="Desired WORLD-space cell size for Set from World "
                    "Target. Conversion divides by the largest "
                    "effective axis scale (singular value — correct "
                    "under shear), so no transformed axis comes out "
                    "coarser than this",
        default=0.1, min=1e-6, soft_max=100.0, subtype='DISTANCE',
        update=_est_display_changed,
    )
    safe_add_cells: IntProperty(
        name="Initial Cells",
        description="Add Remesh Modifier (Safe) derives its initial "
                    "voxel size as longest-bounding-axis / this many "
                    "cells — mesh-relative instead of the scale-blind "
                    "datablock default",
        default=64, min=1, soft_max=512,
    )
    guide_source: EnumProperty(
        name="Guide Source",
        description="Which entry point's voxel size the viewport guide "
                    "shows",
        items=(
            ('AUTO', "Auto",
             "The VOXEL Remesh modifier when one exists, else the "
             "mesh's destructive-remesh settings"),
            (core.CONTEXT_MESH, "Mesh (Destructive)",
             "Mesh.remesh_voxel_size — the sculpt/Ctrl-R operation"),
            (core.CONTEXT_MODIFIER, "Modifier",
             "The active VOXEL Remesh modifier"),
        ),
        default='AUTO',
        update=_est_display_changed,
    )


# ---------------------------------------------------------------------------
# Shared estimate presentation (panel + confirm dialogs)
# ---------------------------------------------------------------------------

_RISK_ICONS = {
    estimate.RISK_GREEN: 'CHECKMARK',
    estimate.RISK_YELLOW: 'ERROR',       # Blender's warning triangle
    estimate.RISK_RED: 'CANCEL',
}

_WARNING_TEXT = {
    estimate.WARN_UNAPPLIED_SCALE:
        "Unapplied object scale: world cell differs from the value",
    estimate.WARN_NON_UNIFORM_SCALE:
        "Non-uniform scale: world cells are anisotropic",
    estimate.WARN_NEGATIVE_SCALE:
        "Negative scale (mirrored transform)",
    estimate.WARN_SHEARED:
        "Sheared transform: sizes derived from singular values",
}


def _fmt_cells(n, saturated=False):
    if saturated:
        return "> %.1e (saturated)" % float(estimate.SATURATION_CAP)
    return "{:,}".format(n)


def estimate_rows(est):
    """[(text, icon), ...] presenting an Estimate. Shared by the panel
    and the confirm dialogs so every surface speaks the same risk
    language. The native object-space voxel size is shown by the
    PROPERTY WIDGET next to these rows, verbatim — never reprinted in a
    reinterpreted space."""
    rows = []
    rows.append(("Risk: %s  (%s cells on longest axis)"
                 % (est.risk.title(),
                    _fmt_cells(est.longest_axis_cells,
                               est.longest_axis_cells
                               >= estimate.SATURATION_CAP)),
                 _RISK_ICONS[est.risk]))
    rows.append(("Bounding cells: %s  [%s x %s x %s]"
                 % (_fmt_cells(est.bounding_cells, est.saturated),
                    _fmt_cells(est.axis_cells[0]),
                    _fmt_cells(est.axis_cells[1]),
                    _fmt_cells(est.axis_cells[2])),
                 'MESH_GRID'))
    rows.append(("Surface score: %.3g (relative, not a face count)"
                 % est.surface_cells, 'SURFACE_NSURFACE'))
    ws = est.world_axis_sizes
    if est.scale_warnings:
        rows.append(("World cell: %.4g / %.4g / %.4g m (X/Y/Z)"
                     % ws, 'ORIENTATION_GLOBAL'))
    else:
        rows.append(("World cell: %.4g m (scale applied)" % ws[0],
                     'ORIENTATION_GLOBAL'))
    for code in est.scale_warnings:
        rows.append((_WARNING_TEXT.get(code, code), 'ERROR'))
    rows.append(("Source: %s  -  %s" % (est.source,
                                        est.confidence.lower()),
                 'INFO'))
    return rows


def _draw_rows(layout, rows):
    col = layout.column(align=True)
    for text, icon in rows:
        col.label(text=text, icon=icon)


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


def _active_mesh(context):
    obj = context.active_object
    if obj is not None and obj.type == 'MESH':
        return obj
    return None


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class CALIPERS_OT_refresh(bpy.types.Operator):
    """Recompute the geometry statistics behind the estimates for the
    active object (bounds, surface area, modifier-input source)"""
    bl_idname = "object.calipers_refresh"
    bl_label = "Refresh Estimates"

    @classmethod
    def poll(cls, context):
        return _active_mesh(context) is not None

    def execute(self, context):
        if not core.refresh_for(context):
            self.report({'WARNING'}, "Nothing to estimate")
            return {'CANCELLED'}
        core.reset_debounce()
        overlay.mark_dirty()
        _tag_redraw_view3d()
        return {'FINISHED'}


class OBJECT_OT_calipers_set_from_world(bpy.types.Operator):
    """Write the object-space voxel size that realizes the World Target
    cell size: target divided by the LARGEST effective axis scale
    (singular value), so no transformed axis comes out coarser"""
    bl_idname = "object.calipers_set_from_world"
    bl_label = "Set from World Target"
    bl_options = {'REGISTER', 'UNDO'}

    target: EnumProperty(
        name="Target",
        items=(
            (core.CONTEXT_MESH, "Mesh (Destructive Operation)", ""),
            (core.CONTEXT_MODIFIER, "Remesh Modifier", ""),
        ),
        default=core.CONTEXT_MESH,
    )

    @classmethod
    def poll(cls, context):
        return _active_mesh(context) is not None

    def execute(self, context):
        obj = _active_mesh(context)
        s = context.scene.calipers
        try:
            v = estimate.world_target_to_object(
                s.world_target, [list(row) for row in obj.matrix_world])
        except estimate.EstimateError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if self.target == core.CONTEXT_MESH:
            # Real RNA property assignment (never an idprop write).
            obj.data.remesh_voxel_size = v
            where = "Mesh.remesh_voxel_size"
        else:
            mod = core.find_voxel_modifier(obj)
            if mod is None:
                self.report({'ERROR'}, "No VOXEL Remesh modifier on %r"
                            % obj.name)
                return {'CANCELLED'}
            mod.voxel_size = v
            where = "modifier %r voxel size" % mod.name
        overlay.mark_dirty()
        _tag_redraw_view3d()
        self.report({'INFO'},
                    "%s = %.6g (object space) for a %.6g world target"
                    % (where, v, s.world_target))
        return {'FINISHED'}


class OBJECT_OT_calipers_add_remesh_safe(bpy.types.Operator):
    """Add a VOXEL Remesh modifier in a PENDING state: viewport and
    render evaluation off, voxel size derived from the evaluated bounds
    — nothing is computed until you confirm from the Calipers panel.
    (The native Add Modifier menu still evaluates immediately on add)"""
    bl_idname = "object.calipers_add_remesh_safe"
    bl_label = "Add Remesh Modifier (Safe)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_mesh(context) is not None

    def execute(self, context):
        obj = _active_mesh(context)
        s = context.scene.calipers
        # Evaluated bounds BEFORE adding: the current stack output is
        # exactly the geometry that will enter the new trailing
        # modifier. Depsgraph reads live in operators, never in draw.
        depsgraph = context.evaluated_depsgraph_get()
        st = core.evaluated_stats(
            obj, depsgraph, "evaluated stack entering the modifier",
            True)
        mod = obj.modifiers.new("Remesh", 'REMESH')
        if mod is None:
            self.report({'ERROR'}, "Could not add a Remesh modifier")
            return {'CANCELLED'}
        mod.mode = 'VOXEL'
        # Same-execution disable, BEFORE any depsgraph pull: probed on
        # 5.1.2 — the remesh never evaluates this way. Render is
        # disabled too: a render must not stall on a modifier whose
        # cost was never confirmed.
        mod.show_viewport = False
        mod.show_render = False
        mod.voxel_size = core.bounds_derived_voxel_size(
            st.bounds_min, st.bounds_max, s.safe_add_cells)
        core.refresh_stats(obj, context.evaluated_depsgraph_get())
        overlay.mark_dirty()
        _tag_redraw_view3d()
        self.report({'INFO'},
                    "Remesh modifier added PENDING (voxel size %.6g = "
                    "longest axis / %d cells). Review the estimate, "
                    "then Enable in the Calipers panel"
                    % (mod.voxel_size, s.safe_add_cells))
        return {'FINISHED'}


class OBJECT_OT_calipers_enable_modifier(bpy.types.Operator):
    """Enable viewport/render evaluation of the pending Remesh modifier
    — THE expensive event. The estimate is shown for confirmation
    first; warnings never hard-block"""
    bl_idname = "object.calipers_enable_modifier"
    bl_label = "Enable Remesh Modifier"
    bl_options = {'REGISTER', 'UNDO'}

    modifier_name: StringProperty(
        name="Modifier",
        description="Name of the Remesh modifier to enable (empty = "
                    "the panel's active VOXEL Remesh modifier)",
        default="",
    )

    @classmethod
    def poll(cls, context):
        obj = _active_mesh(context)
        mod = core.find_voxel_modifier(obj) if obj else None
        return mod is not None and not mod.show_viewport

    def _resolve(self, context):
        obj = _active_mesh(context)
        if obj is None:
            return None, None
        if self.modifier_name:
            mod = obj.modifiers.get(self.modifier_name)
            if (mod is None or mod.type != 'REMESH'
                    or mod.mode != 'VOXEL'):
                return obj, None
            return obj, mod
        return obj, core.find_voxel_modifier(obj)

    def invoke(self, context, event):
        obj, mod = self._resolve(context)
        if mod is None:
            self.report({'ERROR'}, "No VOXEL Remesh modifier")
            return {'CANCELLED'}
        # Estimate BEFORE the toggle (the toggle is the expensive
        # event). Disabled trailing modifier => exact evaluated input.
        core.refresh_stats(obj, context.evaluated_depsgraph_get())
        s = context.scene.calipers
        est = core.current_estimate(obj, core.CONTEXT_MODIFIER, s)
        self._rows = estimate_rows(est) if est is not None else [
            ("No estimate (invalid voxel size?)", 'ERROR')]
        self._risk = est.risk if est is not None else estimate.RISK_RED
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        _draw_rows(layout, getattr(self, "_rows", []))
        if getattr(self, "_risk", None) == estimate.RISK_RED:
            box = layout.box()
            box.label(text="RED band: this evaluation may stall or "
                           "exhaust memory.", icon='CANCEL')
            box.label(text="OK runs it anyway (warnings never "
                           "hard-block).")
        layout.label(text="OK enables viewport + render evaluation "
                          "now.", icon='PLAY')

    def execute(self, context):
        obj, mod = self._resolve(context)
        if mod is None:
            self.report({'ERROR'}, "No VOXEL Remesh modifier")
            return {'CANCELLED'}
        mod.show_viewport = True
        mod.show_render = True
        overlay.mark_dirty()
        _tag_redraw_view3d()
        self.report({'INFO'}, "Remesh modifier %r is now evaluating"
                    % mod.name)
        return {'FINISHED'}


class OBJECT_OT_calipers_voxel_remesh(bpy.types.Operator):
    """Run the destructive Voxel Remesh operation with a preflight: the
    estimate (original-mesh geometry source, exact) is shown for
    confirmation first. The native operator and its Ctrl-R keymap are
    untouched — this is an additional, safer front door"""
    bl_idname = "object.calipers_voxel_remesh"
    bl_label = "Voxel Remesh (Preflight)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Defer to the native operator's own poll (probed: active mesh
        # in Object Mode true, Edit Mode false) so the wrapper is
        # runnable exactly when the wrapped operation is.
        try:
            return bpy.ops.object.voxel_remesh.poll()
        except Exception:
            return False

    def invoke(self, context, event):
        obj = _active_mesh(context)
        if obj is None:
            return {'CANCELLED'}
        # Original mesh datablock stats: the probed geometry source of
        # the destructive operation. No depsgraph needed.
        core.refresh_stats(obj)
        s = context.scene.calipers
        est = core.current_estimate(obj, core.CONTEXT_MESH, s)
        self._rows = estimate_rows(est) if est is not None else [
            ("No estimate (voxel size must be > 0)", 'ERROR')]
        self._risk = est.risk if est is not None else estimate.RISK_RED
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        obj = _active_mesh(context)
        if obj is not None:
            # The native object-space value, verbatim, editable in the
            # dialog itself.
            layout.prop(obj.data, "remesh_voxel_size")
        _draw_rows(layout, getattr(self, "_rows", []))
        if getattr(self, "_risk", None) == estimate.RISK_RED:
            box = layout.box()
            box.label(text="RED band: this remesh may stall or exhaust "
                           "memory.", icon='CANCEL')
            box.label(text="OK runs it anyway (warnings never "
                           "hard-block).")
        layout.label(text="OK destructively replaces the mesh "
                          "(reprojects masks/face sets/colors; other "
                          "data layers are lost).", icon='INFO')

    def execute(self, context):
        obj = _active_mesh(context)
        if obj is None:
            return {'CANCELLED'}
        v = obj.data.remesh_voxel_size
        if not v > 0.0:
            # Probed: 0.0 is assignable (hard_min 0.0) and the native
            # op RAISES on it — reject with a clean message instead.
            self.report({'ERROR'},
                        "Voxel size must be > 0 (got %.6g)" % v)
            return {'CANCELLED'}
        try:
            ret = bpy.ops.object.voxel_remesh()
        except RuntimeError as exc:
            self.report({'ERROR'}, "Voxel Remesh failed: %s"
                        % str(exc).strip().splitlines()[-1])
            return {'CANCELLED'}
        if 'FINISHED' not in ret:
            self.report({'WARNING'}, "Voxel Remesh returned %r" % ret)
            return {'CANCELLED'}
        core.refresh_stats(obj)
        overlay.mark_dirty()
        _tag_redraw_view3d()
        self.report({'INFO'}, "Voxel remeshed %r: %d vertices"
                    % (obj.name, len(obj.data.vertices)))
        return {'FINISHED'}


class CALIPERS_OT_overlay_toggle(bpy.types.Operator):
    """Toggle the mesh-relative voxel-size guide in the 3D viewport (a
    sample cell plus capped grid slices — never every voxel)"""
    bl_idname = "view3d.calipers_overlay_toggle"
    bl_label = "Toggle Voxel Size Guide"

    @classmethod
    def poll(cls, context):
        if overlay.is_enabled():
            return True     # always allow toggling OFF
        return _active_mesh(context) is not None

    def execute(self, context):
        if overlay.is_enabled():
            overlay.disable()
            self.report({'INFO'}, "Voxel size guide disabled")
        else:
            if not overlay.enable(context):
                self.report({'WARNING'}, "Select a mesh object first")
                return {'CANCELLED'}
            # Give the guide something to draw immediately.
            core.refresh_for(context)
            overlay.mark_dirty()
            self.report({'INFO'}, "Voxel size guide enabled")
        _tag_redraw_view3d()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI: one N-panel tab, both entry points as separate boxes
# ---------------------------------------------------------------------------

class VIEW3D_PT_calipers(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_calipers"
    bl_label = "Calipers"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Calipers"

    def draw(self, context):
        # Draw-safe by construction: cache lookups + pure arithmetic
        # via core.current_estimate; stats extraction happens in the
        # operators and the debounced timer.
        layout = self.layout
        obj = _active_mesh(context)
        if obj is None:
            layout.label(text="Select a mesh object", icon='INFO')
            return
        s = context.scene.calipers

        row = layout.row(align=True)
        row.operator(CALIPERS_OT_refresh.bl_idname, icon='FILE_REFRESH')

        # --- destructive operation ------------------------------------
        box = layout.box()
        box.label(text="Voxel Remesh (Destructive Operation)",
                  icon='SCULPTMODE_HLT')
        # The native OBJECT-space value, verbatim — never reinterpreted.
        box.prop(obj.data, "remesh_voxel_size")
        if not obj.data.remesh_voxel_size > 0.0:
            box.label(text="Voxel size must be > 0", icon='ERROR')
        else:
            est = core.current_estimate(obj, core.CONTEXT_MESH, s)
            if est is None:
                box.label(text="No estimate yet - press Refresh "
                               "Estimates", icon='QUESTION')
            else:
                _draw_rows(box, estimate_rows(est))
        row = box.row(align=True)
        row.prop(s, "world_target", text="World Target")
        row.operator(OBJECT_OT_calipers_set_from_world.bl_idname,
                     text="Set", icon='WORLD').target = core.CONTEXT_MESH
        opr = box.row()
        opr.scale_y = 1.2
        opr.operator(OBJECT_OT_calipers_voxel_remesh.bl_idname,
                     icon='MOD_REMESH')

        # --- Remesh modifier -------------------------------------------
        box = layout.box()
        box.label(text="Remesh Modifier (Voxel Mode)",
                  icon='MODIFIER')
        mod = core.find_voxel_modifier(obj)
        if mod is None:
            box.operator(OBJECT_OT_calipers_add_remesh_safe.bl_idname,
                         icon='ADD')
            box.label(text="Arrives pending: no remesh until you "
                           "confirm", icon='INFO')
        else:
            box.label(text="Modifier: %s" % mod.name, icon='DOT')
            box.prop(mod, "voxel_size")
            if not mod.voxel_size > 0.0:
                box.label(text="Voxel size must be > 0", icon='ERROR')
            else:
                est = core.current_estimate(obj, core.CONTEXT_MODIFIER,
                                            s)
                if est is None:
                    box.label(text="No estimate yet - press Refresh "
                                   "Estimates", icon='QUESTION')
                else:
                    _draw_rows(box, estimate_rows(est))
            row = box.row(align=True)
            row.prop(s, "world_target", text="World Target")
            row.operator(
                OBJECT_OT_calipers_set_from_world.bl_idname,
                text="Set", icon='WORLD').target = core.CONTEXT_MODIFIER
            if not mod.show_viewport:
                box.label(text="PENDING - not evaluating",
                          icon='PAUSE')
                opr = box.row()
                opr.scale_y = 1.2
                opr.operator(
                    OBJECT_OT_calipers_enable_modifier.bl_idname,
                    icon='PLAY').modifier_name = mod.name
            else:
                box.label(text="Evaluating live", icon='PLAY')

        # --- viewport guide ---------------------------------------------
        box = layout.box()
        box.label(text="Visual Guide", icon='MESH_GRID')
        row = box.row(align=True)
        row.operator(CALIPERS_OT_overlay_toggle.bl_idname,
                     text="Voxel Size Guide",
                     icon='HIDE_OFF' if overlay.is_enabled()
                     else 'HIDE_ON',
                     depress=overlay.is_enabled())
        box.prop(s, "guide_source", text="Source")
        err = overlay.last_draw_error()
        if err is not None:
            box.label(text="Guide draw failed - see console",
                      icon='ERROR')

        # --- thresholds ----------------------------------------------------
        box = layout.box()
        box.label(text="Risk Thresholds", icon='PREFERENCES')
        col = box.column(align=True)
        col.prop(s, "yellow_exp")
        col.prop(s, "red_exp")
        col.prop(s, "safe_add_cells")


def _menu_draw(self, context):
    """Object-menu entries so F3 search finds the wrappers (sibling
    lesson: never popover/panel-only). Prefixed — these appear without
    the panel's context."""
    self.layout.separator()
    self.layout.operator(OBJECT_OT_calipers_add_remesh_safe.bl_idname,
                         text="Calipers: Add Remesh Modifier (Safe)")
    self.layout.operator(OBJECT_OT_calipers_voxel_remesh.bl_idname,
                         text="Calipers: Voxel Remesh (Preflight)")


# ---------------------------------------------------------------------------
# Handlers + timer (the debounce driver; timers never fire headless)
# ---------------------------------------------------------------------------

@persistent
def _on_depsgraph_update(scene, depsgraph):
    # O(1): a timestamp note. The actual stats refresh happens in the
    # debounced timer after the edit burst goes quiet.
    core.note_activity()


def _timer_cb():
    try:
        if core.poll_debounce(bpy.context):
            overlay.mark_dirty()
            _tag_redraw_view3d()
    except Exception:
        # Never let the timer die silently mid-session.
        traceback.print_exc()
    return core.POLL_S


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    CalipersSettings,
    CALIPERS_OT_refresh,
    OBJECT_OT_calipers_set_from_world,
    OBJECT_OT_calipers_add_remesh_safe,
    OBJECT_OT_calipers_enable_modifier,
    OBJECT_OT_calipers_voxel_remesh,
    CALIPERS_OT_overlay_toggle,
    VIEW3D_PT_calipers,
)

_MENUS = ("VIEW3D_MT_object",)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.calipers = PointerProperty(type=CalipersSettings)
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            menu.append(_menu_draw)
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(
            _on_depsgraph_update)
    try:
        if not bpy.app.timers.is_registered(_timer_cb):
            bpy.app.timers.register(_timer_cb,
                                    first_interval=core.POLL_S,
                                    persistent=True)
    except Exception:
        pass    # background mode: timers never fire anyway (probed)


def unregister():
    try:
        if bpy.app.timers.is_registered(_timer_cb):
            bpy.app.timers.unregister(_timer_cb)
    except Exception:
        pass
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(
            _on_depsgraph_update)
    overlay.disable()
    core.invalidate()
    core.reset_debounce()
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            try:
                menu.remove(_menu_draw)
            except Exception:
                pass
    del bpy.types.Scene.calipers
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
