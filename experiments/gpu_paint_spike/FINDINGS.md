# GPU Paint Spike — Findings

**Status: dab-latency verdict CONFIRMED in the GUI run (2026-07-11). The
sync-back bottleneck that run exposed (~1.24 s at 4K) is diagnosed and fixed
in v0.2.0; post-fix numbers pending re-measurement (slots marked
`GUI-TBD (post-fix)`).**
Environment: Blender **5.1.2** (`ec6e62d40fa9`, 2026-05-19), Windows binary run
from WSL2; numpy 2.3.4 bundled. GUI run: **NVIDIA Quadro RTX 5000 Max-Q,
OpenGL backend**, 4096×4096 RGBA, 2026-07-11 (from the user's
`~/gpu_paint_spike_stats.log`).

## Spike question

> Can brush dabs be rasterized into a texture **on the GPU** from a Python
> modal operator at interactive rates (target: **< 2 ms/dab**, hundreds of
> dabs/sec) with occlusion-correct 3D projection and live viewport feedback —
> with CPU cost paid only **once per stroke** (sync-back)?

A clear "no, because X, measured Y" is a fully successful outcome.

## Technique

1. **Dab rasterization in UV space.** The mesh triangle soup (positions +
   per-corner UVs) is drawn into the paint texture's framebuffer with a
   vertex shader that emits `vec4(uv * 2 - 1, 0, 1)` as the clip-space
   position while passing the world position through. Every covered fragment
   *is* one texel of the paint texture and knows the 3D point it textures.
   The fragment shader projects that point through the current view, tests
   the screen-space brush disc (radius + hardness smoothstep falloff +
   pressure), tests occlusion, and emits the brush color; fixed-function
   `'ALPHA'` blending accumulates the dab into the attachment — **no
   ping-pong** (runtime probe confirms/refutes; see GUI probes below).
2. **Occlusion without the viewport depth buffer** (which Python cannot
   read): once per *view change* — not per dab — the mesh is rendered into a
   private framebuffer: `DEPTH_COMPONENT32F` depth attachment for z-testing
   plus an `R32F` color attachment storing `clip.z / clip.w` computed in the
   fragment shader. The dab shader recomputes the identical quantity from
   the identical matrix, so backend NDC-convention differences (GL −1..1 vs
   Vulkan/Metal 0..1, y-flip) cancel out by construction. Direct sampling of
   the depth attachment is deliberately **not** relied on.
3. **Live feedback:** a `POST_VIEW` handler draws the mesh with the paint
   `GPUTexture` (create-info shader, clip-space depth bias against
   z-fighting — the `uv_island_overlay` mechanism).
4. **Sync-back once per stroke:** on mouse release the draw callback drains
   the GPU (1×1 read), then reads the framebuffer back **once** — preferably
   `fb.read_color(..., data=Buffer)` straight into pre-allocated numpy memory
   (probed at session start), else `fb.read_color(..., 'FLOAT')` → Buffer →
   numpy via the fastest probed rung of a conversion ladder — and the modal
   operator writes `image.pixels.foreach_set(arr)` — never from a draw
   callback. (v0.1.0 used a bare `np.asarray(buf)` here; see the sync-back
   section below for why that cost ~1.05 s at 4K.)
5. **Threading of GPU work:** the modal operator never touches `gpu`; it
   enqueues dabs and tags a redraw. The `POST_VIEW` callback — where a GPU
   context is guaranteed on every backend — flushes the queue. This is the
   spike's answer to "is there a GPU context inside a modal event handler?":
   *don't need one; don't gamble on one.*

## Probe results — Blender 5.1.2, headless (measured facts)

| Probe | Result |
|---|---|
| `gpu.types.GPUShader(vert, frag)` legacy constructor | **REMOVED** — `TypeError: cannot create 'GPUShader' instances`. `create_from_info` is the only path. |
| `GPUShaderCreateInfo` population (incl. `sampler()`) headless | **Works** — descriptors are pure bookkeeping; only `create_from_info` touches the GPU. |
| Any GPU object creation headless (`GPUTexture`, `GPUOffScreen`, `create_from_info`, even `gpu.state.blend_get`) | **Raises** `SystemError: GPU functions for drawing are not available in background mode`. Hence: all gpu work lazy + latch-guarded. |
| `GPUOffScreen` | Color formats limited to `RGBA8/RGBA16/RGBA16F/RGBA32F`; has `texture_color`, `draw_view3d`. Not needed — see next row. |
| `GPUFrameBuffer(depth_slot=…, color_slots=…)` | **Present** — arbitrary attachments, incl. `DEPTH_COMPONENT32F` depth slot and `R32F`/`RGBA16F` color. Has `bind()` (context manager), `clear()`, `read_color(x,y,w,h,channels,slot,format)`, `read_depth()`, `viewport_set()`. This is the spike's workhorse; `GPUOffScreen` is unnecessary. |
| `GPUTexture` | Full format list incl. `R32F`, `RGBA16F`, `DEPTH_COMPONENT32F`; `read()`, `clear()`; constructor takes a `Buffer` for initial data (used to seed the paint texture from the Image). |
| `GPUShaderCreateInfo.image()` (load/store images) | **Present** on 5.1.2 — a future alternative (imageStore-style painting, no framebuffer at all). Not exercised by this spike. |
| `GPUShaderCreateInfo.define()/typedef_source()/depth_write()` | Present. |
| `gpu.state` | `blend/depth_test/depth_mask/viewport/scissor` have get+set; **`face_culling_get` does not exist** (set-only) — restore the documented default `'NONE'`. `GPUFrameBuffer` binds do **not** save the viewport (documented) — capture/restore it manually. |
| `gpu.capabilities` | Module present, but every getter **raises headless** — capability checks are GUI-only. |

## Measurements — headless (real numbers)

CPU side of sync-back: writing a full float32 RGBA buffer into
`Image.pixels`, measured on this machine (single run, `--factory-startup`):

| Texture | `pixels.foreach_set` | naive `pixels[:] = list` | `image.update()` | `pixels.foreach_get` |
|---|---|---|---|---|
| 1024² | **4.7 ms** | 20.6 ms (4.4× slower) | ~0.1 ms | 3.8 ms |
| 2048² | **19.1 ms** | — | ~0.0 ms | 15.6 ms |
| 4096² | **75.2 ms** | — | ~0.0 ms | 59.7 ms |

CPU-side Buffer→numpy conversion costs at 4K (16.78M floats), measured
headless on this machine (2026-07-11, best of 5 unless noted):

| Conversion | Cost | Meaning |
|---|---|---|
| element-wise (16.7M-float Python list → `np.asarray`) | **1858 ms** (single run) | the v0.1.0 trap: what numpy's sequence-protocol fallback / `to_list()` costs |
| `float16 → float32` (`astype`) | **119 ms** | what a half-float `tex.read()` + CPU convert would ADD — rejects the half-read idea |
| `float32` zero-copy view + `ravel` | **0.0005 ms** | what the fixed path costs |

Conclusions already safe to draw headless:

- `foreach_set` is the right write path (≈4× faster than slice assignment);
  cost scales linearly with texel count (~4.6 ms per Mpixel).
- Even at 4K the **CPU share of a stroke-end sync-back is < 80 ms** — paid
  once per stroke, this alone cannot sink the spike question. The open
  half is the GPU→CPU transfer (`fb.read_color`), which is GUI-only.
- **Half-float readback is not worth it**: GUI data shows `tex.read()`
  (RGBA16F, 32 MB) ≈ `fb.read_color(..., 'FLOAT')` (64 MB) ≈ 100 ms — the
  transfer is not conversion-bound — and the CPU-side `astype` alone costs
  119 ms. The production path stays `'FLOAT'` (which `foreach_set` needs
  anyway).

## Measurements — GUI (protocol in README.md)

The add-on prints one `GPU_PAINT_SPIKE_STROKE key=value …` line per stroke
and one `GPU_PAINT_SPIKE_PROBE …` block on session start (both also appended
to `~/gpu_paint_spike_stats.log`).

**Measured run: Quadro RTX 5000 Max-Q, OpenGL, 4096×4096 RGBA, 2026-07-11
(v0.1.0 build; numbers from the user's stats log).**

### Runtime capability probes (printed on first draw) — MEASURED 2026-07-11

| Probe line | Expected | Measured (Quadro RTX 5000 Max-Q, OpenGL) |
|---|---|---|
| `backend=… vendor=… renderer=…` | (records the GPU/backend under test) | OpenGL, NVIDIA Quadro RTX 5000 Max-Q |
| `rgba16f_color_fb` | yes | **yes** |
| `blend_alpha_into_offscreen_attachment` | yes (center ≈ `[0.5, 0, 0.5, …]`) — if **NO**, ping-pong fallback becomes necessary and per-dab cost roughly doubles | **yes** — no ping-pong needed |
| `gputexture_read_rgba16f` | records numpy dtype/shape of `tex.read()` | **yes** (works; cost ≈ `fb.read_color`, see below) |
| `r32f_color_fb` | yes | **yes** |
| `depth32f_attach_clear_read` | yes (cleared=1.000) | **yes** |
| `paint_tex_seeded_from_image` | yes (Buffer-from-numpy path works) | yes (stroke sync-back round-tripped correctly) |
| `buffer_to_numpy_path` (new in 0.2.0) | fastest zero-copy rung | GUI-TBD (post-fix) |
| `fb_read_into_numpy_buffer` (new in 0.2.0) | yes → conversion step disappears | GUI-TBD (post-fix) |

### Per-dab / per-stroke timings — 4K MEASURED 2026-07-11; 1K/2K TBD

All from the `GPU_PAINT_SPIKE_STROKE` lines. Note `submit_*` are CPU
**submission** times (the GPU runs asynchronously); `drain_ms` (1×1 read
after the last dab) bounds true GPU completion of the whole stroke.

| Metric | Target | 1K | 2K | 4K (measured) |
|---|---|---|---|---|
| `submit_avg_ms` (per dab) | < 2 ms | GUI-TBD | GUI-TBD | **0.02–0.03 steady state** (first stroke 0.62 avg — shader compile) |
| `submit_max_ms` | — | GUI-TBD | GUI-TBD | **23 ms on the first stroke only** (one-time shader compile) |
| `dabs_per_s` (fast scribble, ≥ 2 s) | 100s | GUI-TBD | GUI-TBD | (not transcribed; submission cost implies it is event-rate-bound, not GPU-bound) |
| `drain_ms` (stroke-end GPU drain) | small vs stroke | GUI-TBD | GUI-TBD | **~3 ms** |
| `prepass_ms` (per view change) | ≪ frame budget | GUI-TBD | GUI-TBD | (not transcribed) |
| `fb_read_ms` (full readback) | — | GUI-TBD | GUI-TBD | **~100 ms** |
| `tex_read_ms` (A/B probe, removed from production path in 0.2.0) | — | GUI-TBD | GUI-TBD | **~105 ms** |
| `to_numpy_ms` | ~0 | GUI-TBD | GUI-TBD | **~1050–1100 ms — THE BOTTLENECK** (diagnosed + fixed, next section) |
| `pixels_write_ms` | ≈ headless table | GUI-TBD | GUI-TBD | **74 ms** (headless table said 75.2 — confirmed) |
| `syncback_total_ms` (once per stroke) | < 150 ms @2K | GUI-TBD | GUI-TBD | **~1240 ms (pre-fix)** |

**Per-dab verdict: CONFIRMED.** 0.02–0.03 ms submission per dab at 4K is
~two orders of magnitude under the < 2 ms target; the 0.62 ms first-stroke
average (23 ms max) is one-time shader compilation. `drain_ms` ≈ 3 ms shows
the GPU finishes the whole stroke essentially as it is submitted — falsifiers
1–3 are all dead. The dab path is not to be touched.

Also record: mesh (name, triangle count), viewport size, GPU model.

## Sync-back: diagnosis and fix (v0.2.0)

The measured decomposition of the pre-fix ~1240 ms sync-back at 4K:

| Stage | ms | Verdict |
|---|---|---|
| `drain_ms` | 3 | fine |
| `fb_read_ms` | ~100 | real GPU→CPU transfer cost (64 MB ≈ 0.64 GB/s incl. driver) |
| `tex_read_ms` | ~105 | **A/B probe artifact** — v0.1.0 read the texture TWICE per stroke |
| `to_numpy_ms` | ~1050–1100 | **the bottleneck** |
| `pixels_write_ms` | 74 | fine (matches headless prediction) |
| `image_update_ms` | ~0 | fine |

**Diagnosis.** v0.1.0 converted the readback with a bare
`np.asarray(buf, dtype=np.float32)`. On this build `gpu.types.Buffer`
evidently exposes neither `__array_interface__` nor a buffer-protocol view
numpy accepts, so numpy silently degraded to **element-wise sequence
iteration over 16.7M Python floats** (or the `except` branch's `to_list()`
did the same). The headless corroboration: a 16.7M-float Python list →
`np.asarray` costs **1858 ms** on this machine — same order as the measured
~1050 ms. The cost was never the GPU transfer; it was Python object churn.

**Fix (v0.2.0):**

1. **One read per stroke.** `tex.read()` is removed from the production
   path (they measured ~equal; `fb.read_color` stays because it guarantees
   float32 — which `foreach_set` needs — and accepts a target Buffer). The
   A/B comparison survives behind `engine.DEBUG_COMPARE_READS = True`.
2. **Read directly into numpy memory.** Probed at session start:
   `fb.read_color(..., data=gpu.types.Buffer('FLOAT', shape, ndarray))` —
   if the Buffer *wraps* (not copies) the numpy memory, the pixels land in
   the exact array `foreach_set` consumes and the conversion step disappears
   (`readback_path=read_into_numpy`, `to_numpy_ms` ≈ 0). Probe line:
   `fb_read_into_numpy_buffer=yes|NO`.
3. **Gated conversion ladder** for the fallback: `asarray` (requires
   `__array_interface__`) → `frombuffer` (requires the C buffer protocol) →
   `memoryview` → `to_list_fallback`. Each rung is *verified against known
   values on a small Buffer* at session start, and rungs that would "work"
   only via numpy's slow sequence protocol are structurally impossible to
   select. Probe line: `buffer_to_numpy_path=<rung>`.
4. **Half-float read rejected on the numbers** (see the headless table):
   the reads cost the same, and `float16→float32` `astype` alone adds
   119 ms of CPU at 4K.

**Expected post-fix sync-back at 4K: ~180 ms** (3 drain + ~100 read + ~0
convert + 74 write) if either the direct-read probe or a zero-copy rung
passes; worst case (all probes NO) it stays ~1.15 s on the honest
`to_list_fallback`, and the probe lines say so explicitly.

### Post-fix re-measurement — `GUI-TBD (post-fix)`

One session per texture size, one stroke each (second stroke onward; the
first compiles shaders), then read `~/gpu_paint_spike_stats.log`:

| Metric | 1K | 2K | 4K |
|---|---|---|---|
| `buffer_to_numpy_path` (probe line, once) | GUI-TBD | GUI-TBD | GUI-TBD |
| `fb_read_into_numpy_buffer` (probe line, once) | GUI-TBD | GUI-TBD | GUI-TBD |
| `readback_path` | GUI-TBD | GUI-TBD | GUI-TBD |
| `drain_ms` | GUI-TBD | GUI-TBD | GUI-TBD |
| `fb_read_ms` | GUI-TBD | GUI-TBD | GUI-TBD |
| `to_numpy_ms` | GUI-TBD | GUI-TBD | GUI-TBD |
| `pixels_write_ms` | GUI-TBD | GUI-TBD | GUI-TBD |
| `syncback_total_ms` | GUI-TBD | GUI-TBD | GUI-TBD |

### Correctness spot-checks — `GUI-TBD`

- [ ] Paint lands under the cursor (no vertical mirroring / offset — would
      indicate an NDC y-convention leak; toggle *Occlusion Test* off to
      isolate depth vs projection).
- [ ] Back side of a sphere stays clean while painting the front
      (occlusion works); with *Occlusion Test* off it paints through.
- [ ] Orbiting mid-session re-renders the prepass (console `prepass_ms`
      changes) and painting still lands correctly.
- [ ] After RMB-stop, the Image editor shows the stroke (sync-back wrote
      the datablock).

## Limitations discovered / accepted (spike scope)

- **No undo** — the operator is not `'UNDO'`; painted pixels are only in the
  Image datablock after stroke-end sync. Out of scope by design.
- **No color management** — `image.pixels` round-trips raw stored values;
  brush colors blend in storage space, not scene-linear-composited space.
- **No seam padding/dilation** — UV-space rasterization paints exactly the
  covered texels; island edges will show bleed gaps under mipmapping/filtering.
- **UV overlaps double-paint** (both islands receive the dab) and texels
  outside any UV chart are never painted — inherent to the technique.
- **Occlusion epsilon in NDC** is non-linear with distance: grazing angles
  and distant geometry can self-shadow or leak. A real tool would compare
  view-space depth. `DEPTH_EPSILON = 2e-3`.
- **Dabs use the prepass matrices**: painting *while* orbiting uses the
  latest prepass view (correct), but object transforms mid-stroke are
  intentionally ignored.
- **Per-dab cost scales with mesh triangle count** (the whole mesh is
  re-rasterized in UV space per dab). Scissoring the dab's UV bounding box
  or coarse per-island culling would fix this; out of scope.
- Texture memory: RGBA16F at 4K = 128 MB per attachment. Fine for a spike.
- `pixels.foreach_set` needs the readback as float32; `fb.read_color(...,
  'FLOAT')` guarantees that regardless of the RGBA16F attachment.

## Verdict

**CONFIRMED for the core thesis (GUI run, Quadro RTX 5000 Max-Q, OpenGL,
2026-07-11): GPU dab rasterization from Python is interactive-rate.**
Measured `submit_avg_ms` = **0.02–0.03 ms/dab** at 4K steady state (target
was < 2 ms — beaten by ~100×), `drain_ms` ≈ 3 ms (the GPU keeps up with
submission), first stroke 0.62 ms avg / 23 ms max = one-time shader compile.

Falsifier status from that run:

1. `blend_alpha_into_offscreen_attachment=NO` + ping-pong ≥ 2 ms — **dead**
   (probe = yes, no ping-pong needed).
2. `submit_avg_ms` ≥ 2 ms — **dead** (0.02–0.03 ms measured).
3. `drain_ms` growing into hundreds of ms — **dead** (~3 ms).
4. Paint mirrored/offset from the cursor — not reported; strokes
   round-tripped to the Image correctly.

The one problem the run surfaced was **not** the spike question: the
stroke-end sync-back cost ~1.24 s at 4K, of which ~1.05 s was Python-side
Buffer→numpy conversion and ~105 ms a double-read probe artifact — both
diagnosed and fixed in v0.2.0 (see the sync-back section). Remaining risk
is narrow: if BOTH the direct-read probe and every zero-copy ladder rung
report NO on this build, sync-back stays slow on the honest fallback and the
proposal escalates to "the gpu module needs a zero-copy readback API" — the
post-fix probe lines answer that in one session.
