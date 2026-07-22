# SPDX-License-Identifier: GPL-2.0-or-later
"""Project-owned preview icons used by Impasto's compact controls."""

from pathlib import Path

import bpy.utils.previews


_icons = None
_NAMES = ('soften', 'erase')


def register():
    global _icons
    unregister()
    _icons = bpy.utils.previews.new()
    root = Path(__file__).with_name("assets") / "icons"
    for name in _NAMES:
        _icons.load(name, str(root / (name + ".png")), 'IMAGE')


def unregister():
    global _icons
    if _icons is not None:
        bpy.utils.previews.remove(_icons)
        _icons = None


def icon_value(name):
    icon = _icons.get(name) if _icons is not None else None
    return icon.icon_id if icon is not None else 0


def is_loaded(name):
    """Whether an asset is registered, including headless Blender runs.

    Background Blender does not allocate UI-atlas icon IDs, so ``icon_id`` can
    validly remain zero there even though the preview asset loaded correctly.
    """
    return _icons is not None and name in _icons
