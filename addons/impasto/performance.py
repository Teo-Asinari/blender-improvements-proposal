# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless-safe high-resolution session estimates."""

from dataclasses import dataclass

MIB = 1024 * 1024
BENCHMARK_MODES = ("PAINT", "ERASE", "SOFTEN", "SMEAR")
BENCHMARK_CHANNELS = (1, 4, 8)


@dataclass(frozen=True)
class SessionEstimate:
    resident_bytes: int
    cpu_image_bytes: int
    copy_bytes_per_dab: int
    warnings: tuple


def estimate_session(size, channels):
    size, channels = int(size), int(channels)
    if size <= 0 or channels <= 0:
        raise ValueError("size and channels must be positive")
    texels = size * size
    warnings = ()
    if size >= 4096:
        warnings = ("Soften/Smear copy full selected textures per dab",)
    return SessionEstimate(
        texels * 8 * (channels + 1),
        texels * 16 * channels,
        texels * 8 * channels,
        warnings,
    )


def texture_bytes(size):
    size = int(size)
    if size <= 0:
        raise ValueError("size must be positive")
    return size * size * 8


def full_copy_bytes_per_dab(size, target_channels):
    target_channels = int(target_channels)
    if target_channels <= 0:
        raise ValueError("target_channels must be positive")
    return texture_bytes(size) * target_channels


def session_memory_estimate(size, channels):
    estimate = estimate_session(size, channels)
    canvas = texture_bytes(size) * int(channels)
    return {
        "canvas_bytes": canvas,
        "scratch_bytes": texture_bytes(size),
        "resident_bytes": estimate.resident_bytes,
        "cpu_image_bytes": estimate.cpu_image_bytes,
    }


def benchmark_matrix(size=4096):
    return tuple({"mode": mode, "channels": count, "size": int(size)}
                 for mode in BENCHMARK_MODES
                 for count in BENCHMARK_CHANNELS)
