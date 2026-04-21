"""Brush Gesture: 3DCoat-style interactive brush size/strength drag for Blender."""

bl_info = {
    "name": "Brush Gesture",
    "author": "Blender Improvements Proposal",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "Sculpt / Paint modes (hold D + Right Mouse drag)",
    "description": "Interactive brush size and strength via mouse gesture with preset detents",
    "category": "Paint",
}


import bpy

from . import operators, overlay, preferences


_keymaps: list = []


def _install_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    prefs = bpy.context.preferences.addons[__name__].preferences
    preferences.ensure_defaults(prefs)

    # VERIFY: Each paint mode has its own keymap name in 4.x; Window is used as a
    # catch-all so the gesture fires regardless of active tool keymap context.
    km = kc.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
    kmi = km.keymap_items.new(
        operators.BRUSH_GESTURE_OT_modal.bl_idname,
        type=prefs.gesture_mouse,
        value="PRESS",
    )
    setattr(kmi, prefs.hold_key.lower(), True) if prefs.hold_key in {"LEFT_CTRL", "LEFT_ALT"} else None
    _keymaps.append((km, kmi))

    # A separate keymap item listens for the hold key itself when combined with
    # the mouse button. Relying on a modifier flag works for Alt/Ctrl/Shift; the
    # D/S/F/Q letters have to be caught via the hold_key kmi.key_modifier slot.
    if prefs.hold_key not in {"LEFT_CTRL", "LEFT_ALT"}:
        kmi.key_modifier = prefs.hold_key


def _uninstall_keymap():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _keymaps.clear()


def register():
    preferences.register()
    operators.register()
    _install_keymap()


def unregister():
    _uninstall_keymap()
    overlay.stop()
    operators.unregister()
    preferences.unregister()
