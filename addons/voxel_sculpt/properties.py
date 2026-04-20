"""PropertyGroup for Voxel Sculpt settings.

Attached to ``bpy.types.Scene`` in ``__init__.register``. All user-visible
settings live here so that the UI panel and operators can share state.

Units chosen for the skeleton are intentionally simple:
  * ``brush_size`` and ``voxel_size`` are in Blender units (metres by default).
  * ``brush_strength`` is a 0..1 multiplier applied per-step.
  * ``voxel_size`` drives the background grid resolution used by the VDB
    backend; smaller = more detail + more memory / CPU.

Real implementation notes are tagged with ``# TODO:``.
"""

import bpy
from bpy.props import EnumProperty, FloatProperty


class VoxelSculptSettings(bpy.types.PropertyGroup):
    """Scene-level voxel sculpting settings."""

    brush_size: FloatProperty(
        name="Brush Size",
        description="Radius of the brush in world units",
        default=0.25,
        min=0.001,
        soft_max=2.0,
        unit="LENGTH",
    )

    brush_strength: FloatProperty(
        name="Brush Strength",
        description="Per-step strength multiplier applied when sculpting",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    voxel_size: FloatProperty(
        name="Voxel Size",
        description=(
            "Size of a single voxel in world units. Smaller values give "
            "more detail at the cost of memory and compute."
        ),
        default=0.02,
        min=0.001,
        soft_max=0.5,
        unit="LENGTH",
    )

    # TODO: expand into a full enum of 3DCoat-style brush modes
    # (Grow / Carve / Smooth / Pinch / Flatten / Move / etc.) once the
    # backend can execute them.
    brush_mode: EnumProperty(
        name="Brush Mode",
        description="Which CSG-like operation the brush performs",
        items=(
            ("ADD", "Add", "Union a sphere SDF into the grid"),
            ("SUBTRACT", "Subtract", "Subtract a sphere SDF from the grid"),
            ("SMOOTH", "Smooth", "Apply a level-set smoothing filter"),
        ),
        default="ADD",
    )
