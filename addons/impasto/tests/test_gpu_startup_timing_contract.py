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

print("IMPASTO_GPU_STARTUP_TIMING_CONTRACT_PASSED")
