"""GPU + blf HUD rendering for the active gesture.

A single module-level state dict is intentional: there is only ever one
gesture modal running (it grabs mouse capture), so a per-call handle would
just add ceremony.
"""

from __future__ import annotations

import math
from typing import Optional

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader


_state: dict = {
    "handle": None,
    "data": None,
}


def _shader():
    # VERIFY: Blender 4.x renamed some builtin shaders; UNIFORM_COLOR is present
    # in 4.0+, falls back to 2D_UNIFORM_COLOR if an older path is ever hit.
    try:
        return gpu.shader.from_builtin("UNIFORM_COLOR")
    except Exception:
        return gpu.shader.from_builtin("2D_UNIFORM_COLOR")


def _circle_verts(cx: float, cy: float, radius: float, segments: int = 64) -> list[tuple[float, float]]:
    verts = []
    step = (2.0 * math.pi) / segments
    for i in range(segments + 1):
        a = i * step
        verts.append((cx + math.cos(a) * radius, cy + math.sin(a) * radius))
    return verts


def _draw_circle(cx: float, cy: float, radius: float, color: tuple[float, float, float, float]) -> None:
    shader = _shader()
    verts = _circle_verts(cx, cy, radius)
    batch = batch_for_shader(shader, "LINE_STRIP", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_rect(x: float, y: float, w: float, h: float, color: tuple[float, float, float, float]) -> None:
    shader = _shader()
    verts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    batch = batch_for_shader(shader, "TRI_FAN", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_line(x0: float, y0: float, x1: float, y1: float, color: tuple[float, float, float, float]) -> None:
    shader = _shader()
    batch = batch_for_shader(shader, "LINES", {"pos": [(x0, y0), (x1, y1)]})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_text(x: float, y: float, text: str, size: int, color: tuple[float, float, float, float]) -> None:
    font_id = 0
    # VERIFY: blf.size signature changed across 3.x -> 4.x; 4.x uses (id, size).
    try:
        blf.size(font_id, size)
    except TypeError:
        blf.size(font_id, size, 72)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0.0)
    blf.draw(font_id, text)


def _draw_callback():
    data = _state.get("data")
    if not data:
        return

    region = data.get("region")
    if region is None:
        return

    cx = data["cursor_x"]
    cy = data["cursor_y"]
    size_px = data["size_px"]
    strength = data["strength"]
    size_val = data["size_value"]
    mode_label = data["mode_label"]
    size_presets = data["size_presets"]
    text_size = data["text_size"]

    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(1.5)

    _draw_circle(cx, cy, max(size_px, 1.0), (1.0, 1.0, 1.0, 0.85))
    _draw_circle(cx, cy, max(size_px * 0.5, 0.5), (1.0, 1.0, 1.0, 0.25))

    _draw_strength_bar(cx, cy, strength)
    _draw_size_scale(region, size_val, size_presets)

    label = f"Size: {size_val:.1f}   Strength: {strength:.2f}   [{mode_label}]"
    _draw_text(20, 20, label, text_size, (1.0, 1.0, 1.0, 0.95))

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def _draw_strength_bar(cx: float, cy: float, strength: float) -> None:
    bar_w = 8.0
    bar_h = 120.0
    x = cx + 40.0
    y = cy - bar_h * 0.5
    _draw_rect(x, y, bar_w, bar_h, (0.0, 0.0, 0.0, 0.35))
    fill = max(0.0, min(1.0, strength)) * bar_h
    _draw_rect(x, y, bar_w, fill, (1.0, 0.8, 0.2, 0.85))


def _draw_size_scale(region, size_val: float, presets: list[float]) -> None:
    if not presets:
        return

    margin = 40.0
    y = 60.0
    width = region.width - margin * 2.0
    if width <= 0:
        return

    lo = min(presets[0], size_val, 1.0)
    hi = max(presets[-1], size_val)
    span = max(hi - lo, 1.0)

    _draw_line(margin, y, margin + width, y, (1.0, 1.0, 1.0, 0.35))

    for p in presets:
        px = margin + (p - lo) / span * width
        _draw_line(px, y - 6, px, y + 6, (1.0, 1.0, 1.0, 0.55))
        _draw_text(px - 10, y + 10, f"{p:.0f}", 10, (1.0, 1.0, 1.0, 0.7))

    cx = margin + (size_val - lo) / span * width
    _draw_line(cx, y - 12, cx, y + 12, (1.0, 0.6, 0.2, 0.95))


def start(data: dict) -> None:
    if _state["handle"] is not None:
        stop()
    _state["data"] = data
    _state["handle"] = bpy.types.SpaceView3D.draw_handler_add(
        _draw_callback, (), "WINDOW", "POST_PIXEL",
    )


def update(data: dict) -> None:
    _state["data"] = data


def stop() -> None:
    handle = _state.get("handle")
    if handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
        except Exception:
            pass
    _state["handle"] = None
    _state["data"] = None


def tag_redraw(context) -> None:
    area = getattr(context, "area", None)
    if area is not None:
        area.tag_redraw()
