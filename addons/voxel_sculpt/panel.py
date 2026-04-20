"""Sidebar panel for the Voxel Sculpt skeleton.

Registers a single panel in the 3D Viewport sidebar (N-panel) under a
dedicated "Voxel Sculpt" tab. Contents are placeholder buttons that call
the stub operators from ``operators.py`` plus the property sliders from
``properties.py``.
"""

import bpy
from bpy.types import Panel


class VOXEL_PT_main_panel(Panel):
    bl_label = "Voxel Sculpt"
    bl_idname = "VOXEL_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Voxel Sculpt"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.voxel_sculpt

        # --- Object management -------------------------------------------------
        box = layout.box()
        box.label(text="Object", icon="VOLUME_DATA")
        box.operator("voxel.new_voxel_object", icon="ADD")
        # TODO: add "Duplicate Voxel Object", "Bake Grid", etc. once the
        #       backend supports persistent grids.

        # --- Brush settings ---------------------------------------------------
        box = layout.box()
        box.label(text="Brush Settings", icon="BRUSH_DATA")
        box.prop(settings, "brush_mode")
        box.prop(settings, "brush_size")
        box.prop(settings, "brush_strength")
        box.operator("voxel.brush_modal", text="Start Stroke", icon="PAINT_BRUSH")

        # --- Grid settings ----------------------------------------------------
        box = layout.box()
        box.label(text="Grid", icon="MESH_GRID")
        box.prop(settings, "voxel_size")
        # TODO: surface resolution presets (Low / Medium / High / Custom) that
        #       translate to voxel_size under the hood.

        # --- Conversion -------------------------------------------------------
        box = layout.box()
        box.label(text="Conversion", icon="OUTLINER_OB_MESH")
        box.operator("voxel.remesh_to_mesh", icon="MOD_REMESH")
        # TODO: "Send to Retopology" button that creates a low-poly mesh next
        #       to the voxel object and switches into a retopo-friendly mode.

        # --- Status -----------------------------------------------------------
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Status: skeleton (no real voxel ops)", icon="INFO")
        col.label(text="See docs/VOXEL_SCULPT_DESIGN.md")
