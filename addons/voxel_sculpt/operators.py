"""Operators for the Voxel Sculpt skeleton.

Only structural plumbing is provided here. Each operator either prints
what it would do or is a no-op modal handler. Real behaviour is tagged
with ``# TODO:``.
"""

import bpy
from bpy.types import Operator


class VOXEL_OT_new_voxel_object(Operator):
    """Create a new voxel sculpting object.

    In a real implementation this would:
      1. Create a new Volume (or custom) datablock bound to an OpenVDB grid.
      2. Initialise an empty narrow-band level-set grid via ``vdb_backend.create_grid``.
      3. Link an empty display mesh to the active collection so the user has
         something selectable in the viewport until the first brush stroke.
    """

    bl_idname = "voxel.new_voxel_object"
    bl_label = "New Voxel Object"
    bl_description = "Create an empty voxel object to sculpt on"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.voxel_sculpt
        # TODO: call vdb_backend.create_grid(voxel_size=settings.voxel_size)
        # TODO: wrap the returned grid handle in a Blender datablock and link
        #       it to the active collection.
        self.report(
            {"INFO"},
            f"[skeleton] would create voxel object at voxel_size={settings.voxel_size:.4f}",
        )
        return {"FINISHED"}


class VOXEL_OT_brush_modal(Operator):
    """Modal operator that captures mouse input for a brush stroke.

    Skeleton behaviour: prints every relevant event to the console and exits
    on release or Esc. No raycast, no grid edit, no re-mesh.
    """

    bl_idname = "voxel.brush_modal"
    bl_label = "Voxel Brush (Modal)"
    bl_description = "Start a voxel brush stroke (skeleton -- prints events only)"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        # Refuse to start unless we are in a 3D Viewport; real implementation
        # will also want to require an active voxel object.
        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Voxel brush must be invoked from a 3D Viewport")
            return {"CANCELLED"}

        # TODO: look up the active voxel object and ensure its VDB grid is
        #       resident on the correct device / in the correct state.
        self._stroke_active = False
        context.window_manager.modal_handler_add(self)
        print("[voxel_sculpt] brush modal: started")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "LEFTMOUSE":
            if event.value == "PRESS":
                self._stroke_active = True
                print(f"[voxel_sculpt] stroke begin at ({event.mouse_region_x}, {event.mouse_region_y})")
                # TODO: raycast into the active voxel object, convert the
                #       hit point into grid-local coordinates and queue the
                #       first dab for the VDB backend.
                return {"RUNNING_MODAL"}
            if event.value == "RELEASE":
                self._stroke_active = False
                print("[voxel_sculpt] stroke end")
                # TODO: flush any queued dabs, trigger a re-mesh of the dirty
                #       region and tag the viewport for redraw.
                return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE" and self._stroke_active:
            print(
                f"[voxel_sculpt] stroke move to ({event.mouse_region_x}, {event.mouse_region_y})"
            )
            # TODO: sample along the stroke path, call vdb_backend.add_sphere
            #       or subtract_sphere depending on settings.brush_mode, and
            #       schedule incremental re-meshing of touched tiles.
            return {"RUNNING_MODAL"}

        if event.type in {"ESC", "RIGHTMOUSE"}:
            print("[voxel_sculpt] brush modal: cancelled")
            return {"CANCELLED"}

        return {"PASS_THROUGH"}


class VOXEL_OT_remesh_to_mesh(Operator):
    """Convert the active voxel object into a regular Blender Mesh.

    Real implementation would call ``vdb_backend.grid_to_mesh`` (which wraps
    OpenVDB's ``volumeToMesh``) and replace / add a Mesh datablock with the
    resulting vertex / face arrays.
    """

    bl_idname = "voxel.remesh_to_mesh"
    bl_label = "Remesh to Mesh"
    bl_description = "Convert the voxel grid to a Blender mesh (skeleton -- no-op)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        # TODO: verts, tris, quads = vdb_backend.grid_to_mesh(grid_handle,
        #           adaptivity=0.0, iso=0.0)
        # TODO: build a bpy.data.meshes.new(...) from those arrays and either
        #       replace the active object's data or create a new object next
        #       to it for retopology handoff.
        self.report({"INFO"}, "[skeleton] would run volumeToMesh and create a Mesh")
        return {"FINISHED"}
