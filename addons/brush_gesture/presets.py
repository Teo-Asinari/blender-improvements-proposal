"""Pure-Python detent/snap math for brush gesture.

Kept bpy-free so behavior can be validated without Blender.
"""

from __future__ import annotations

from typing import Iterable, Sequence


MODE_FREE = "FREE"
MODE_DETENT = "DETENT"
MODE_SNAP = "SNAP"


def _nearest_preset(value: float, presets: Sequence[float]) -> tuple[float, float] | tuple[None, None]:
    if not presets:
        return None, None
    best = min(presets, key=lambda p: abs(p - value))
    return best, abs(best - value)


def snap_to_preset(
    value: float,
    presets: Sequence[float],
    mode: str = MODE_DETENT,
    detent_radius: float = 6.0,
) -> float:
    """Apply preset influence to a candidate value.

    In SNAP mode the value is locked to the nearest preset outright.
    In FREE mode the value passes through unchanged.
    In DETENT mode we blend toward the preset with strength that peaks at the
    preset and falls off linearly to zero at detent_radius. This produces the
    "magnet" feel: small residual drift near a preset is absorbed, larger
    deviations pass through.
    """
    if mode == MODE_FREE or not presets:
        return value

    nearest, distance = _nearest_preset(value, presets)
    if nearest is None:
        return value

    if mode == MODE_SNAP:
        return nearest

    if distance >= detent_radius:
        return value

    pull = 1.0 - (distance / detent_radius)
    pull *= pull
    return value + (nearest - value) * pull


def apply_detent(
    raw_delta: float,
    current: float,
    presets: Sequence[float],
    mode: str = MODE_DETENT,
    detent_radius: float = 6.0,
    damping: float = 0.35,
) -> float:
    """Return the new value after integrating raw_delta against presets.

    In DETENT mode, motion inside a preset's radius gets damped: the closer to
    the preset, the more the delta is attenuated. This gives slow drags the
    sticky feel while fast drags (large raw_delta) still sail past.
    """
    if mode == MODE_FREE or not presets:
        return current + raw_delta

    if mode == MODE_SNAP:
        candidate = current + raw_delta
        nearest, _ = _nearest_preset(candidate, presets)
        return nearest if nearest is not None else candidate

    nearest, distance = _nearest_preset(current, presets)
    if nearest is None or distance >= detent_radius:
        return current + raw_delta

    proximity = 1.0 - (distance / detent_radius)
    resistance = damping + (1.0 - damping) * (1.0 - proximity * proximity)
    return current + raw_delta * resistance


def clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def format_presets(raw: Iterable[float]) -> list[float]:
    out: list[float] = []
    for v in raw:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out
