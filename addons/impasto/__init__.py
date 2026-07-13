# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto — non-destructive PBR layer stacks for Blender."""

bl_info = {
    "name": "Impasto",
    "author": "Teo Asinari",
    "version": (0, 5, 0),
    "blender": (5, 1, 0),
    "location": "3D Viewport > Sidebar (N) > Impasto tab",
    "description": "Non-destructive PBR material layer stacks",
    "category": "Paint",
}

if "model" in locals():
    import importlib
    model = importlib.reload(model)
    debounce = importlib.reload(debounce)
    compat = importlib.reload(compat)
    reconcile = importlib.reload(reconcile)
    snapshot = importlib.reload(snapshot)
    engine = importlib.reload(engine)
    if "visibility" in locals():
        visibility = importlib.reload(visibility)
    else:
        from . import visibility
    if "brush_adapter" in locals():
        brush_adapter = importlib.reload(brush_adapter)
        tile_undo = importlib.reload(tile_undo)
    else:
        from . import brush_adapter
        from . import tile_undo
    gpu_engine = importlib.reload(gpu_engine)
    props = importlib.reload(props)
    if "paint" in locals():
        paint = importlib.reload(paint)
    else:
        from . import paint
    ops = importlib.reload(ops)
    ui = importlib.reload(ui)
else:
    from . import model
    from . import debounce
    from . import compat
    from . import reconcile
    from . import snapshot
    from . import engine
    from . import visibility
    from . import brush_adapter
    from . import tile_undo
    from . import gpu_engine
    from . import props
    from . import paint
    from . import ops
    from . import ui


def register():
    props.register()
    ops.register()
    ui.register()
    engine.register()


def unregister():
    engine.unregister()
    ui.unregister()
    ops.unregister()
    props.unregister()


if __name__ == "__main__":
    register()
