"""Modal operator that implements the size/strength drag gesture."""

from __future__ import annotations

from typing import Optional

import bpy
from bpy.types import Operator

from . import memory, overlay
from .presets import (
    MODE_DETENT,
    MODE_FREE,
    MODE_SNAP,
    apply_detent,
    clamp,
    snap_to_preset,
)
from .preferences import (
    ensure_defaults,
    get_size_presets,
    get_strength_presets,
)


_MODE_TO_TOOL_SETTINGS = {
    "SCULPT": "sculpt",
    "PAINT_TEXTURE": "image_paint",
    "PAINT_WEIGHT": "weight_paint",
    "PAINT_VERTEX": "vertex_paint",
}


def _resolve_paint(context) -> Optional[object]:
    mode = getattr(context, "mode", None)
    attr = _MODE_TO_TOOL_SETTINGS.get(mode)
    if attr is None:
        return None
    ts = context.tool_settings
    return getattr(ts, attr, None)


def _active_brush(context):
    paint = _resolve_paint(context)
    if paint is None:
        return None
    return getattr(paint, "brush", None)


def _mode_label(event_shift: bool, event_ctrl: bool) -> str:
    if event_shift:
        return MODE_SNAP
    if event_ctrl:
        return MODE_FREE
    return MODE_DETENT


class BRUSH_GESTURE_OT_modal(Operator):
    bl_idname = "brush_gesture.modal"
    bl_label = "Brush Gesture"
    bl_description = "Drag to adjust brush size (horizontal) and strength (vertical)"
    bl_options = {"REGISTER", "GRAB_CURSOR", "BLOCKING"}

    @classmethod
    def poll(cls, context):
        if context.area is None or context.area.type != "VIEW_3D":
            return False
        return _resolve_paint(context) is not None

    def invoke(self, context, event):
        brush = _active_brush(context)
        if brush is None:
            self.report({"WARNING"}, "No active brush for current mode")
            return {"CANCELLED"}

        prefs = context.preferences.addons[__package__].preferences
        ensure_defaults(prefs)

        self._prefs = prefs
        self._brush = brush
        self._start_x = event.mouse_region_x
        self._start_y = event.mouse_region_y
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y

        recalled = memory.recall(brush.name)
        if recalled is not None:
            size, strength = recalled
            self._apply_size(size)
            self._apply_strength(strength)

        self._size = float(brush.size)
        self._strength = float(brush.strength)
        self._size_presets = get_size_presets(prefs)
        self._strength_presets = get_strength_presets(prefs)

        if prefs.show_hud:
            overlay.start(self._hud_payload(event))

        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set("SCROLL_XY")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        prefs = self._prefs

        if event.type in {"ESC"} or (
            event.type == prefs.gesture_mouse and event.value == "RELEASE"
        ):
            return self._commit(context, cancelled=event.type == "ESC")

        if event.type == prefs.hold_key and event.value == "RELEASE":
            return self._commit(context, cancelled=False)

        if event.type == "MOUSEMOVE":
            self._process_move(context, event)
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}

    def _process_move(self, context, event):
        prefs = self._prefs
        mode = _mode_label(event.shift, event.ctrl)

        dx = event.mouse_region_x - self._last_x
        dy = event.mouse_region_y - self._last_y
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y

        size_delta = dx / max(prefs.size_sensitivity, 0.001)
        strength_delta = dy / max(prefs.strength_sensitivity, 0.001)
        if not prefs.invert_strength_axis:
            strength_delta = -strength_delta

        self._size = apply_detent(
            size_delta, self._size, self._size_presets, mode,
            prefs.detent_radius_px, prefs.detent_damping,
        )
        self._size = snap_to_preset(
            self._size, self._size_presets, mode, prefs.detent_radius_px,
        )
        self._size = clamp(self._size, 1.0, 2000.0)

        self._strength = apply_detent(
            strength_delta, self._strength, self._strength_presets, mode,
            prefs.detent_radius_px / 200.0, prefs.detent_damping,
        )
        self._strength = snap_to_preset(
            self._strength, self._strength_presets, mode,
            prefs.detent_radius_px / 200.0,
        )
        self._strength = clamp(self._strength, 0.0, 1.0)

        self._apply_size(self._size)
        self._apply_strength(self._strength)

        if prefs.show_hud:
            overlay.update(self._hud_payload(event, mode))
            overlay.tag_redraw(context)

    def _commit(self, context, cancelled: bool):
        overlay.stop()
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass
        if not cancelled and self._brush is not None:
            memory.remember(self._brush.name, float(self._brush.size), float(self._brush.strength))
        if context.area is not None:
            context.area.tag_redraw()
        return {"CANCELLED"} if cancelled else {"FINISHED"}

    def cancel(self, context):
        overlay.stop()
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass

    def _apply_size(self, value: float) -> None:
        if self._brush is None:
            return
        # VERIFY: brush.size is an IntProperty in Blender 4.x; round to int on assign.
        try:
            self._brush.size = int(round(value))
        except Exception:
            pass

    def _apply_strength(self, value: float) -> None:
        if self._brush is None:
            return
        try:
            self._brush.strength = float(value)
        except Exception:
            pass

    def _hud_payload(self, event, mode: str = MODE_DETENT) -> dict:
        region = getattr(bpy.context, "region", None)
        return {
            "region": region,
            "cursor_x": event.mouse_region_x,
            "cursor_y": event.mouse_region_y,
            "size_px": float(self._brush.size) if self._brush else 1.0,
            "size_value": self._size,
            "strength": self._strength,
            "mode_label": mode,
            "size_presets": self._size_presets,
            "strength_presets": self._strength_presets,
            "text_size": int(self._prefs.hud_text_size),
        }


classes = (BRUSH_GESTURE_OT_modal,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
