# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless contract for bounded GPU-session startup diagnostics."""

import inspect
import sys
from pathlib import Path


ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import gpu_engine


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("  ok  " + name)


source = inspect.getsource(gpu_engine._ensure_gpu)
session_source = inspect.getsource(gpu_engine.start_session)
phase_keys = (
    "probe", "shaders_ubos", "ibl", "gutters", "paint_textures",
    "batches", "stack_baselines", "remaining", "total",
)

check("startup phases remain independently measurable",
      all('startup_phases["%s"]' % key in source for key in phase_keys))
check("startup diagnostics are emitted as one bounded summary",
      source.count('"GPU_PAINT_STARTUP total_ms=') == 1
      and "s.gpu_startup_phases_ms = dict(startup_phases)" in source)
check("ready sessions avoid timing and resource reconstruction",
      source.index("if s.gpu_ready:") < source.index("startup_started"))
check("object and image resources are not process-global cached",
      "s.paint_texs = []" in source
      and "_build_stack_baselines(s)" in source
      and "_build_active_preview_textures(s)" in source)
check("active base-normal UV reuses the paint UV soup",
      "requested_base_uv == active_uv_layer.name" in session_source
      and "s.base_normal_uvs = uvs" in session_source
      and session_source.count(
          "s.base_normal_uvs = build_uv_soup(obj, requested_base_uv)") == 1)

print("IMPASTO_GPU_STARTUP_TIMING_CONTRACT_PASSED")
