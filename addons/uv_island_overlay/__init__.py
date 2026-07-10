# SPDX-License-Identifier: GPL-2.0-or-later
"""UV Island Overlay — per-island UV coloring in the 3D viewport.

Each UV island's faces are tinted with a distinct color drawn over the
mesh in the 3D viewport, so island boundaries are instantly visible —
most useful in Edit Mode right after unwrapping.
"""

bl_info = {
    "name": "UV Island Overlay",
    "author": "Teo Asinari",
    "version": (1, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > UV Islands tab; also in the "
                "Overlays popover",
    "description": "Color each UV island distinctly in the 3D viewport "
                   "with live seam-predicted islands",
    "category": "UV",
}

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty

if "overlay" in locals():
    import importlib
    islands = importlib.reload(islands)
    live = importlib.reload(live)
    overlay = importlib.reload(overlay)
else:
    from . import islands
    from . import live
    from . import overlay


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class UV_OT_island_overlay_toggle(bpy.types.Operator):
    """Toggle the UV island color overlay for the active mesh object"""
    bl_idname = "uv.island_overlay_toggle"
    bl_label = "Toggle UV Island Overlay"

    @classmethod
    def poll(cls, context):
        if overlay.is_enabled():
            return True  # always allow toggling OFF
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        wm = context.window_manager
        want = not wm.uv_island_overlay
        wm.uv_island_overlay = want
        if want and not wm.uv_island_overlay:
            # enable() failed and the update callback reverted the
            # property — tell the user instead of failing silently.
            self.report({'WARNING'},
                        "No active mesh object — overlay not enabled")
            return {'CANCELLED'}
        if wm.uv_island_overlay:
            self.report({'INFO'},
                        "UV island overlay on (%d islands)"
                        % overlay.island_count())
        return {'FINISHED'}


class UV_OT_island_overlay_refresh(bpy.types.Operator):
    """Recompute UV islands for the overlaid mesh (use after re-unwrapping)"""
    bl_idname = "uv.island_overlay_refresh"
    bl_label = "Refresh UV Island Overlay"

    @classmethod
    def poll(cls, context):
        return overlay.is_enabled()

    def execute(self, context):
        overlay.refresh(context)
        self.report({'INFO'},
                    "UV islands recomputed (%d islands)"
                    % overlay.island_count())
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
#
# Two surfaces for the same controls, so the feature is discoverable:
# - The Overlays popover (probed on Blender 5.1.2: bpy.types.
#   VIEW3D_PT_overlay accepts draw-function append()), where overlay
#   toggles conventionally live.
# - A sidebar (N-panel) tab "UV Islands", which is far easier to find.
# Plus menu entries (View menu, Edit Mode UV menu) so F3 menu search
# finds the toggle operator.
# ---------------------------------------------------------------------------

def _draw_overlay_controls(layout, context):
    """Shared body: checkbox + island source + refresh button + island
    count + a loud error row if the draw handler ever failed (see
    overlay._draw)."""
    obj = context.active_object
    have_mesh = obj is not None and obj.type == 'MESH'
    col = layout.column()
    row = col.row(align=True)
    row.enabled = have_mesh or overlay.is_enabled()
    row.prop(context.window_manager, "uv_island_overlay",
             text="UV Island Colors")
    sub = row.row(align=True)
    sub.enabled = overlay.is_enabled()
    sub.operator(UV_OT_island_overlay_refresh.bl_idname,
                 text="", icon='FILE_REFRESH')
    # Island source as a dropdown (full item label stays readable in the
    # narrow Overlays popover, unlike an expanded two-button row).
    src_row = col.row(align=True)
    src_row.enabled = have_mesh or overlay.is_enabled()
    src_row.prop(context.window_manager, "uv_island_overlay_source",
                 text="Source")
    if overlay.is_enabled():
        col.label(text="%d island%s (%s)"
                  % (overlay.island_count(),
                     "" if overlay.island_count() == 1 else "s",
                     "predicted" if overlay.active_source() == 'SEAM'
                     else "actual"))
        if overlay.last_draw_error() is not None:
            col.label(text="Draw failed - see system console",
                      icon='ERROR')
    elif not have_mesh:
        col.label(text="Select a mesh object", icon='INFO')


def _overlay_popover_draw(self, context):
    obj = context.active_object
    # Keep showing while enabled even if a non-mesh became active, so the
    # off-switch never disappears from under the user.
    if not overlay.is_enabled() and (obj is None or obj.type != 'MESH'):
        return
    layout = self.layout
    layout.separator()
    _draw_overlay_controls(layout, context)


class VIEW3D_PT_uv_island_overlay(bpy.types.Panel):
    """Sidebar home for the overlay controls (always visible, so the
    feature is discoverable without knowing about the Overlays popover)"""
    bl_idname = "VIEW3D_PT_uv_island_overlay"
    bl_label = "UV Island Colors"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Islands"

    def draw(self, context):
        _draw_overlay_controls(self.layout, context)


def _menu_draw(self, context):
    self.layout.separator()
    self.layout.operator(UV_OT_island_overlay_toggle.bl_idname)


# ---------------------------------------------------------------------------
# Property + handlers
# ---------------------------------------------------------------------------

def _on_enabled_update(self, context):
    if self.uv_island_overlay:
        if not overlay.enable(context):
            # No active mesh: revert so the checkbox never lies. NOTE:
            # this must be a real property assignment — writing the
            # idprop (self["uv_island_overlay"] = False) stopped working
            # in Blender 5.0 (bpy.props storage is no longer idprop-
            # accessible), leaving the checkbox stuck on True. The
            # re-entrant update this triggers is bounded: it takes the
            # else-branch below and stops.
            self.uv_island_overlay = False
    else:
        overlay.disable()


def _on_source_update(self, context):
    """Island source changed: invalidate and rebuild (overlay.set_source
    is a no-op when the value did not actually change)."""
    overlay.set_source(self.uv_island_overlay_source, context)


@persistent
def _on_depsgraph_update(scene, depsgraph):
    """Cheap auto-refresh: when the overlaid object's geometry changes
    (mesh edits, seam marking, mode switches), notify the overlay. In UV
    mode that marks it dirty (recompute once at the next draw — never per
    frame); in SEAM mode it feeds the debounced live pipeline (O(1) here;
    checksum + rebuild happen after a quiet period, off the hot path).
    Probed on 5.1.2: seam-flag-only edits DO report is_updated_geometry,
    selection-only changes do NOT — exactly the filter we want."""
    if not overlay.is_enabled():
        return
    name = overlay.tracked_object_name()
    if name is None:
        return
    # Match the object OR its mesh datablock: depending on the edit,
    # the geometry update can be reported on either ID (and the two can
    # be named differently).
    names = {name}
    obj = bpy.data.objects.get(name)
    data = getattr(obj, "data", None)
    if data is not None:
        names.add(data.name)
    try:
        for update in depsgraph.updates:
            if update.is_updated_geometry and \
                    getattr(update.id, "name", None) in names:
                overlay.on_tracked_geometry_update()
                return
    except Exception:
        pass


@persistent
def _on_load_pre(*args):
    """Drop the overlay before loading a new file so no draw handler or
    object reference goes stale."""
    try:
        wm = bpy.context.window_manager
        if wm is not None and wm.uv_island_overlay:
            wm.uv_island_overlay = False  # update callback disables
        else:
            overlay.disable()
    except Exception:
        overlay.disable()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    UV_OT_island_overlay_toggle,
    UV_OT_island_overlay_refresh,
    VIEW3D_PT_uv_island_overlay,
)

# Menus that get a "Toggle UV Island Overlay" entry. F3 search only finds
# operators that live in a menu, so these double as search keywords:
# View menu (all modes) and the Edit Mode UV menu (right where the user
# just ran Unwrap).
_MENUS = ("VIEW3D_MT_view", "VIEW3D_MT_uv_map")


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.uv_island_overlay = BoolProperty(
        name="UV Island Colors",
        description="Tint each UV island of the active mesh with a "
                    "distinct color in the 3D viewport",
        default=False,
        update=_on_enabled_update,
    )

    # Default 'SEAM': the primary workflow this overlay serves is
    # interactive seam marking — islands update live as
    # seams are added, no Unwrap needed, and the prediction equals what
    # the next seam-respecting Unwrap produces. It is also much cheaper
    # on large meshes. Switch to 'UV' to see the actual current unwrap
    # (e.g. Smart UV Project charts, which have no seam flags).
    bpy.types.WindowManager.uv_island_overlay_source = EnumProperty(
        name="Island Source",
        description="How islands are determined for the overlay colors",
        items=(
            ('SEAM', "Seams (predicted)",
             "Regions bounded by seam edges — updates live while you "
             "mark seams and predicts the islands the next Unwrap will "
             "produce (no UV data needed)"),
            ('UV', "UVs (actual)",
             "True UV-space connectivity of the current unwrap (follows "
             "Smart UV Project / manual UV edits; needs a UV layer, "
             "updates after unwrapping)"),
        ),
        default='SEAM',
        update=_on_source_update,
    )

    if hasattr(bpy.types, "VIEW3D_PT_overlay"):
        bpy.types.VIEW3D_PT_overlay.append(_overlay_popover_draw)
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            menu.append(_menu_draw)

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_on_load_pre)


def unregister():
    overlay.disable()

    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            try:
                menu.remove(_menu_draw)
            except Exception:
                pass
    if hasattr(bpy.types, "VIEW3D_PT_overlay"):
        try:
            bpy.types.VIEW3D_PT_overlay.remove(_overlay_popover_draw)
        except Exception:
            pass

    del bpy.types.WindowManager.uv_island_overlay_source
    del bpy.types.WindowManager.uv_island_overlay

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
