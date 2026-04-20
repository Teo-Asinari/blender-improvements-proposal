"""Voxel Sculpt add-on for Blender 4.x.

This is a SKELETON. No real voxel operations are implemented yet.
The module registers a UI panel, a scene-level PropertyGroup and a stub
modal operator so that the add-on installs and shows up in the sidebar.

All real VDB work is deferred to ``vdb_backend.py`` which currently only
contains TODO stubs. See ``README.md`` and ``docs/VOXEL_SCULPT_DESIGN.md``
at the repository root for the design.
"""

bl_info = {
    "name": "Voxel Sculpt",
    "author": "blender-improvements-proposal contributors",
    "version": (0, 0, 1),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Voxel Sculpt",
    "description": (
        "Skeleton add-on for 3DCoat-style voxel sculpting built on OpenVDB. "
        "No real voxel operations yet -- UI, property group and modal stub only."
    ),
    "warning": "Skeleton / prototype. Not functional.",
    "doc_url": "",
    "category": "Sculpt",
}

import bpy

# Submodules are imported lazily inside register() so that a broken
# submodule during development does not prevent Blender from even showing
# the add-on in the preferences list.
from . import properties
from . import operators
from . import panel


# Ordered list of classes to register. Keep PropertyGroups first because
# other classes may reference them via PointerProperty.
_classes = (
    properties.VoxelSculptSettings,
    operators.VOXEL_OT_new_voxel_object,
    operators.VOXEL_OT_brush_modal,
    operators.VOXEL_OT_remesh_to_mesh,
    panel.VOXEL_PT_main_panel,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    # Attach the settings PropertyGroup to Scene. Using Scene keeps the
    # skeleton simple; a real implementation may want per-object settings
    # or a custom datablock -- see docs/VOXEL_SCULPT_DESIGN.md.
    bpy.types.Scene.voxel_sculpt = bpy.props.PointerProperty(
        type=properties.VoxelSculptSettings
    )


def unregister():
    # Remove the PointerProperty before unregistering the class it points
    # at, otherwise Blender complains about dangling references.
    if hasattr(bpy.types.Scene, "voxel_sculpt"):
        del bpy.types.Scene.voxel_sculpt

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    # Allows running "Run Script" on this file inside Blender's text editor
    # during development. Not used when installed as an add-on.
    register()
