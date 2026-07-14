# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto-owned image-based lighting atlas and shader contracts."""

import sys
import time
from pathlib import Path

import numpy as np

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import gpu_engine, ibl


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


t0 = time.perf_counter()
atlas = ibl.build_environment_atlas()
build_ms = (time.perf_counter() - t0) * 1000.0
expected_shape = (ibl.ATLAS_PANEL_HEIGHT * ibl.ATLAS_PANELS,
                  ibl.ATLAS_WIDTH, 4)
check("environment atlas has stable compact RGBA layout",
      atlas.shape == expected_shape and atlas.dtype == np.float32,
      repr(atlas.shape))
check("environment atlas is finite linear HDR",
      bool(np.isfinite(atlas).all())
      and float(atlas[..., :3].min()) >= 0.0
      and float(atlas[..., :3].max()) > 1.0)
check("atlas cache returns the resident CPU source without regeneration",
      ibl.build_environment_atlas() is atlas)

panels = atlas.reshape(ibl.ATLAS_PANELS, ibl.ATLAS_PANEL_HEIGHT,
                       ibl.ATLAS_WIDTH, 4)[..., :3]
spec_variance = [float(np.var(panel)) for panel in panels[1:]]
check("roughness-prefiltered specular panels lose high-frequency contrast",
      spec_variance[0] > spec_variance[-1], repr(spec_variance))
check("diffuse irradiance is distinct from mirror radiance",
      not np.allclose(panels[0], panels[1]))
check("atlas panel V mapping remains inside its assigned strip",
      0.0 <= ibl.atlas_v(0.0, 0) < ibl.atlas_v(1.0, 0)
      <= ibl.atlas_v(0.0, 1) < 1.0)

src = gpu_engine.PREVIEW_FRAG_SRC
check("Lit preview samples a prefiltered image environment",
      "sample_prefiltered_environment" in src
      and "texture(environment_atlas" in src)
check("Lit preview uses split-sum GGX and roughness-aware Fresnel",
      "environment_brdf_ggx" in src
      and "fresnel_schlick_roughness" in src
      and "vec3 kd = (vec3(1.0) - fresnel) * (1.0 - metallic)" in src)
check("Lit preview tone maps linear HDR output",
      "aces_fitted(rgb)" in src)
check("missing environment has a graceful energy-conserving fallback",
      "if (environment_ready > 0.5)" in src
      and "albedo * kd + fresnel" in src)
check("diagnostic branches still return before IBL work",
      src.index("if (preview_mode == 1)")
      < src.index("sample_prefiltered_environment(\n            reflection")
      and src.index("if (preview_mode == 3)")
      < src.index("sample_prefiltered_environment(\n            reflection"))
check("atlas construction stays a one-time modest CPU cost",
      build_ms < 250.0, "%.2f ms" % build_ms)

print("IMPASTO_IBL_PREVIEW_PASSED build_ms=%.3f bytes=%d" %
      (build_ms, atlas.nbytes))
