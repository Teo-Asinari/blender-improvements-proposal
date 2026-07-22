# SPDX-License-Identifier: GPL-2.0-or-later
"""Focused guards for preview thumbnails and deterministic GPU teardown."""

import sys
from pathlib import Path
from types import SimpleNamespace

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import gpu_engine


class Resource:
    pass


resource_names = (
    "dab_shaders", "dab_ubos", "soften_shader", "smear_shader",
    "soften_ubo", "soften_scratch", "batch_soften", "batch_smear",
    "prepass_shader", "preview_shader", "paint_texs", "paint_fbs",
    "depth_color_tex", "depth_depth_tex", "depth_fb", "batch_dabs",
    "batch_prepass", "batch_preview", "neutral_tex", "copy_shader",
    "copy_batch", "single_fbs", "environment_tex", "base_normal_tex",
    "base_normal_gpu_ref", "stencil_tex", "stencil_preview_shader",
    "baseline_shader", "baseline_batch", "baseline_texs",
    "baseline_gpu_refs", "overlay_circle_batch", "overlay_color_shader",
)
session = SimpleNamespace(**{name: Resource() for name in resource_names},
                          depth_fb_size=(1920, 1080), gpu_ready=True)
gpu_engine._release_gpu_references(session)
for name in resource_names:
    expected = {} if name == "baseline_texs" else (
        [] if name == "baseline_gpu_refs" else None)
    assert getattr(session, name) == expected, name
assert session.depth_fb_size is None
assert session.gpu_ready is False

ui_paint_source = (Path(__file__).resolve().parents[1] /
                   "ui_paint.py").read_text(encoding="utf8")
ui_source = (Path(__file__).resolve().parents[1] /
             "ui.py").read_text(encoding="utf8")
assert "template_ID_preview" in ui_paint_source
assert "template_preview(mat" in ui_source
assert "last synchronized material" in ui_source

print("preview hardening tests PASSED")
