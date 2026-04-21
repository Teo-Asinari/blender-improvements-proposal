"""Per-brush size/strength memory backed by a Scene StringProperty.

JSON on a single StringProperty is simpler than a CollectionProperty here:
the data is small, read/written only at brush switch, and survives .blend
save/load automatically. Gating bpy behind a try-import keeps this module
importable for unit tests.
"""

from __future__ import annotations

import json
from typing import Optional

try:
    import bpy  # type: ignore
except ImportError:
    bpy = None  # type: ignore


SCENE_PROP = "brush_gesture_memory"


def _scene():
    if bpy is None:
        return None
    ctx = getattr(bpy, "context", None)
    return getattr(ctx, "scene", None) if ctx is not None else None


def _load(scene) -> dict:
    raw = scene.get(SCENE_PROP, "") if scene is not None else ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(scene, data: dict) -> None:
    if scene is None:
        return
    scene[SCENE_PROP] = json.dumps(data)


def remember(brush_name: str, size: float, strength: float) -> None:
    scene = _scene()
    if scene is None or not brush_name:
        return
    data = _load(scene)
    data[brush_name] = {"size": float(size), "strength": float(strength)}
    _store(scene, data)


def recall(brush_name: str) -> Optional[tuple[float, float]]:
    scene = _scene()
    if scene is None or not brush_name:
        return None
    data = _load(scene)
    entry = data.get(brush_name)
    if not isinstance(entry, dict):
        return None
    try:
        return float(entry["size"]), float(entry["strength"])
    except (KeyError, TypeError, ValueError):
        return None


def forget(brush_name: str) -> None:
    scene = _scene()
    if scene is None:
        return
    data = _load(scene)
    if brush_name in data:
        del data[brush_name]
        _store(scene, data)


def clear_all() -> None:
    scene = _scene()
    if scene is None:
        return
    _store(scene, {})
