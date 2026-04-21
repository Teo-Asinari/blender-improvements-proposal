"""Add-on preferences: preset lists, detent tuning, keybind."""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator, PropertyGroup, UIList


DEFAULT_SIZES = (5.0, 15.0, 40.0, 100.0, 250.0)
DEFAULT_STRENGTHS = (0.25, 0.5, 0.75, 1.0)


class BrushGesturePresetValue(PropertyGroup):
    value: FloatProperty(name="Value", default=0.0, min=0.0, soft_max=500.0)


class BG_UL_preset_list(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        layout.prop(item, "value", text="")


class BRUSH_GESTURE_OT_preset_add(Operator):
    bl_idname = "brush_gesture.preset_add"
    bl_label = "Add Preset"
    bl_options = {"INTERNAL"}

    target: StringProperty()

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        coll = getattr(prefs, self.target)
        item = coll.add()
        item.value = coll[-2].value if len(coll) >= 2 else 1.0
        return {"FINISHED"}


class BRUSH_GESTURE_OT_preset_remove(Operator):
    bl_idname = "brush_gesture.preset_remove"
    bl_label = "Remove Preset"
    bl_options = {"INTERNAL"}

    target: StringProperty()
    index: IntProperty()

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        coll = getattr(prefs, self.target)
        if 0 <= self.index < len(coll):
            coll.remove(self.index)
        return {"FINISHED"}


class BRUSH_GESTURE_OT_reset_defaults(Operator):
    bl_idname = "brush_gesture.reset_defaults"
    bl_label = "Reset Presets to Defaults"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        prefs.size_presets.clear()
        for v in DEFAULT_SIZES:
            prefs.size_presets.add().value = v
        prefs.strength_presets.clear()
        for v in DEFAULT_STRENGTHS:
            prefs.strength_presets.add().value = v
        return {"FINISHED"}


class BrushGesturePreferences(AddonPreferences):
    bl_idname = __package__

    size_presets: CollectionProperty(type=BrushGesturePresetValue)
    size_active: IntProperty(default=0)

    strength_presets: CollectionProperty(type=BrushGesturePresetValue)
    strength_active: IntProperty(default=0)

    detent_radius_px: FloatProperty(
        name="Detent Radius (px)",
        description="Pixel radius around a preset where motion is damped",
        default=6.0, min=0.0, soft_max=40.0,
    )
    detent_damping: FloatProperty(
        name="Detent Damping",
        description="Minimum motion attenuation at the preset center (1.0 = fully stuck)",
        default=0.35, min=0.0, max=1.0,
    )

    size_sensitivity: FloatProperty(
        name="Size Sensitivity",
        description="Pixels of horizontal drag per size unit",
        default=1.0, min=0.05, soft_max=10.0,
    )
    strength_sensitivity: FloatProperty(
        name="Strength Sensitivity",
        description="Pixels of vertical drag per full strength (0..1)",
        default=250.0, min=10.0, soft_max=2000.0,
    )
    invert_strength_axis: BoolProperty(
        name="Invert Strength Axis",
        description="Drag up increases strength when enabled",
        default=True,
    )

    hold_key: EnumProperty(
        name="Hold Key",
        items=[
            ("D", "D", ""),
            ("S", "S", ""),
            ("F", "F", ""),
            ("Q", "Q", ""),
            ("LEFT_ALT", "Left Alt", ""),
            ("LEFT_CTRL", "Left Ctrl", ""),
        ],
        default="D",
    )
    gesture_mouse: EnumProperty(
        name="Gesture Mouse Button",
        items=[
            ("RIGHTMOUSE", "Right Mouse", ""),
            ("LEFTMOUSE", "Left Mouse", ""),
            ("MIDDLEMOUSE", "Middle Mouse", ""),
        ],
        default="RIGHTMOUSE",
    )

    show_hud: BoolProperty(name="Show HUD Overlay", default=True)
    hud_text_size: IntProperty(name="HUD Text Size", default=14, min=8, max=48)

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Gesture Binding")
        row = box.row(align=True)
        row.prop(self, "hold_key")
        row.prop(self, "gesture_mouse")

        box = layout.box()
        box.label(text="Feel")
        col = box.column(align=True)
        col.prop(self, "detent_radius_px")
        col.prop(self, "detent_damping")
        col.prop(self, "size_sensitivity")
        col.prop(self, "strength_sensitivity")
        col.prop(self, "invert_strength_axis")

        split = layout.split(factor=0.5)
        self._draw_preset_column(split.column(), "Size Presets", "size_presets", "size_active")
        self._draw_preset_column(split.column(), "Strength Presets", "strength_presets", "strength_active")

        row = layout.row()
        row.operator("brush_gesture.reset_defaults", icon="LOOP_BACK")

        box = layout.box()
        box.label(text="HUD")
        box.prop(self, "show_hud")
        box.prop(self, "hud_text_size")

    def _draw_preset_column(self, col, title, prop_name, active_name):
        col.label(text=title)
        row = col.row()
        row.template_list(
            "BG_UL_preset_list", prop_name,
            self, prop_name, self, active_name, rows=4,
        )
        side = row.column(align=True)
        op = side.operator("brush_gesture.preset_add", icon="ADD", text="")
        op.target = prop_name
        op = side.operator("brush_gesture.preset_remove", icon="REMOVE", text="")
        op.target = prop_name
        op.index = getattr(self, active_name)


def ensure_defaults(prefs: BrushGesturePreferences) -> None:
    if len(prefs.size_presets) == 0:
        for v in DEFAULT_SIZES:
            prefs.size_presets.add().value = v
    if len(prefs.strength_presets) == 0:
        for v in DEFAULT_STRENGTHS:
            prefs.strength_presets.add().value = v


def get_size_presets(prefs) -> list[float]:
    return sorted(float(p.value) for p in prefs.size_presets)


def get_strength_presets(prefs) -> list[float]:
    return sorted(float(p.value) for p in prefs.strength_presets)


classes = (
    BrushGesturePresetValue,
    BG_UL_preset_list,
    BRUSH_GESTURE_OT_preset_add,
    BRUSH_GESTURE_OT_preset_remove,
    BRUSH_GESTURE_OT_reset_defaults,
    BrushGesturePreferences,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
