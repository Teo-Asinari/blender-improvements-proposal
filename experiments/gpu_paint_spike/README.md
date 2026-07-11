# GPU Paint Spike (Experimental)

A **feasibility spike**, not a paint tool: can brush dabs be rasterized into
a texture **on the GPU** from a Python modal operator at interactive rates
(< 2 ms/dab, hundreds of dabs/sec), with occlusion-correct 3D projection and
live viewport feedback, paying CPU cost only once per stroke?

Everything here exists to produce **measurements**. The deliverable is
[FINDINGS.md](FINDINGS.md) — technique, probe results, timing tables and the
verdict. Undo, color management, seam padding, multi-object support and
every other real-tool concern are deliberately out of scope.

> **Experimental disclaimer:** this is a research prototype living under
> `experiments/`, not `addons/`. It writes into its own Image datablocks
> (`GPUPaintSpike_<size>`), offers no undo, and may leave the painted
> texture behind. Do not run it on files you care about without saving
> first.

## Requirements

- Blender **5.1.x** (developed and probed against 5.1.2; uses
  `GPUShaderCreateInfo` / `create_from_info` only — the legacy
  `GPUShader(vert, frag)` constructor is removed in 5.1).
- A mesh object **with UVs** (the default cube works).

## Install & run

1. Zip the `gpu_paint_spike` folder (the folder itself, so the zip contains
   `gpu_paint_spike/__init__.py`), then Blender:
   `Edit > Preferences > Add-ons > Install from Disk…`, enable
   **GPU Paint Spike (Experimental)**. (For development, symlink/copy the
   folder into `scripts/addons/` instead.)
2. Select a UV'd mesh object in Object Mode.
3. Open a **system console** (`Window > Toggle System Console` on Windows) —
   the measurements print there.
4. 3D Viewport: press `N` → **GPU Paint** tab → pick a **Texture** size →
   **Start GPU Paint**. (Also reachable via `Object > GPU Paint Spike
   (Experimental)` or menu search.)
5. **LMB-drag** paints. Orbit/zoom freely between strokes (MMB/wheel pass
   through; the depth prepass re-renders itself on view changes).
6. **RMB or Esc** stops the session (finishing any pending sync-back first).

While painting, a small text overlay (bottom-left) shows live dab counts and
the last stroke's timings; the N-panel shows the full stats table; the
console gets one machine-readable line per stroke, and every PROBE/STROKE
line is also appended to `~/gpu_paint_spike_stats.log` (console output is
hidden by default on Windows):

```
GPU_PAINT_SPIKE_PROBE  blend_alpha_into_offscreen_attachment=yes (…)
GPU_PAINT_SPIKE_PROBE  buffer_to_numpy_path=frombuffer
GPU_PAINT_SPIKE_PROBE  fb_read_into_numpy_buffer=yes
GPU_PAINT_SPIKE_STROKE dabs=214 dabs_per_s=163.1 submit_avg_ms=0.41 …
```

## Measurement protocol (fills the GUI-TBD slots in FINDINGS.md)

Setup: default scene is fine for the baseline; also repeat on a subdivided
sphere (~100k tris, Smart UV Project) for the mesh-scaling data point.
Record GPU model and viewport size once.

1. **Probes.** Start a session at 2K and copy the whole
   `GPU_PAINT_SPIKE_PROBE` block from the console/log into the FINDINGS
   probe table. If `blend_alpha_into_offscreen_attachment=NO`, stop — that
   alone is a finding (ping-pong fallback required). The two readback
   probes (`buffer_to_numpy_path=…`, `fb_read_into_numpy_buffer=…`) decide
   which stroke-end path production uses; copy them verbatim.
2. **Per-dab + dabs/sec.** For each texture size (1024 / 2048 / 4096 —
   restart the session per size):
   - Paint one **slow, steady** stroke (~3 s). Record `submit_avg_ms`,
     `submit_max_ms`, `prepass_ms`.
   - Paint one **fast scribble** (~3 s of continuous zig-zag). Record
     `dabs_per_s`, `submit_avg_ms`, `drain_ms`. The first stroke after
     starting compiles shaders — discard it and measure the second onward.
3. **Sync-back.** From the same strokes record `readback_path`,
   `fb_read_ms`, `to_numpy_ms`, `pixels_write_ms`, `syncback_total_ms`
   per size. (`readback_path=read_into_numpy` means the readback landed
   directly in numpy memory and `to_numpy_ms` should be ~0.) `tex_read_ms`
   no longer appears by default: since 0.2.0 the texture is read **once**
   per stroke; set `engine.DEBUG_COMPARE_READS = True` to restore the
   fb.read_color-vs-tex.read A/B comparison (adds a second full transfer
   per stroke).
4. **Occlusion correctness.** On the sphere: paint across the silhouette,
   orbit to the back — it must be clean. Toggle **Occlusion Test** off,
   repeat, confirm it paints through (proves the test was doing the work).
5. **Cursor registration.** Confirm paint appears exactly under the cursor.
   Vertical mirroring or a constant offset is a *finding* (backend NDC
   convention leak) — note the backend from the probe line.
6. Tick the correctness checklist in FINDINGS.md and fill the tables.

## How it works (one paragraph)

The mesh is rendered **into UV space** (vertex shader emits the UV as the
clip position, world position rides along); each fragment is one texel that
projects itself through the current view, tests the screen-space brush disc
and a depth prepass (own `R32F` NDC-depth render, refreshed per view change,
never per dab), and alpha-blends the brush color into an `RGBA16F`
framebuffer attachment. A `POST_VIEW` handler previews the GPU texture on
the mesh; on mouse-release the framebuffer is read back once and written to
the Image datablock via `pixels.foreach_set`. The modal operator never
touches `gpu` — it queues dabs, and the draw callback (where a GPU context
is guaranteed) flushes them. Details and rationale: module docstring of
[engine.py](engine.py) and [FINDINGS.md](FINDINGS.md).

## Tests

```
./tests/run_tests.sh
```

Headless (`--background`) has **no GPU** — the suite covers registration
lifecycle, pure math (falloff, dab spacing), mesh-soup extraction,
create-info descriptor population and GLSL structural consistency, and
re-asserts the environment facts the design leans on (gpu raises headless;
legacy `GPUShader` constructor removed). The GUI protocol above is the real
test of the spike question.

## Known limitations

See the *Limitations* section of [FINDINGS.md](FINDINGS.md) — notably: no
undo, no color management, no seam dilation, UV overlaps double-paint,
NDC-space occlusion epsilon, per-dab cost scales with mesh triangle count.

## License

GPL-2.0-or-later (SPDX headers in source files).
