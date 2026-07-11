# GPU Paint Spike — Findings

**Status: dab-latency verdict CONFIRMED; the v0.2.0 zero-copy sync-back
fix CONFIRMED; and the v0.3.0 four-channel MRT path GUI-MEASURED on
2026-07-11. Four-channel dabs remain interactive-rate, while 4K pen-lift
sync averages ~235 ms and is dominated by Blender's full-image
`Image.pixels` writes.**
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

### Post-fix re-measurement — MEASURED 2026-07-11 (4K; 1K/2K extrapolated)

Measured on the same Quadro RTX 5000 Max-Q / OpenGL session set, ~19
strokes at 4096², from `~/gpu_paint_spike_stats.log`. 1K/2K columns are
extrapolations (readback + pixels-write scale linearly with pixel count;
headless table corroborates the pixels-write scaling), not measured.

| Metric | 1K (extrap.) | 2K (extrap.) | 4K (measured) |
|---|---|---|---|
| `buffer_to_numpy_path` (probe line, once) | — | — | **`to_list_fallback`** — Blender 5.1 `gpu.types.Buffer` exposes NO fast conversion mechanism (no `__array_interface__`, no C buffer protocol). Ladder correctly refused the silent-degradation paths. |
| `fb_read_into_numpy_buffer` (probe line, once) | — | — | **`yes`** — the direct-read path works and is the production path |
| `readback_path` | — | — | **`read_into_numpy`** (every stroke) |
| `drain_ms` | ~3 | ~3 | **~2.8–3.0** |
| `fb_read_ms` | ~6 | ~23 | **~88–101** |
| `to_numpy_ms` | ~0 | ~0 | **~0.0002 — eliminated** (was ~1050–1100) |
| `pixels_write_ms` | ~4.7 | ~19 | **~66–74** |
| `syncback_total_ms` | ~15 | ~50 | **~158–174** (was ~1240; 7.5× improvement, matches the ~180 ms prediction) |

Additional measured point: a 6.6 s fast scribble sustained **1342 dabs at
203 dabs/s** with `submit_avg_ms=0.022` — dab throughput remains
event-rate-bound, not GPU-bound, during real fast painting.

**Sync-back verdict: FIXED and acceptable.** The remaining 4K cost is
~90 ms of raw GPU→CPU transfer + ~70 ms of Blender's `image.pixels`
write — both irreducible from Python and paid once per stroke. Upstream
note: the `buffer_to_numpy_path=to_list_fallback` probe result means
`fb.read_color(data=Buffer-wrapping-numpy)` is the ONLY viable bulk
readback route in the 5.1 Python API; a zero-copy `Buffer`→numpy bridge
(buffer protocol on `gpu.types.Buffer`) is a concrete, citable API gap
for the upstream pitch.

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

## v0.3.0 — multi-channel painting (MRT)

**New spike question:** what does N-channel painting cost? A real material
paint tool writes color + roughness + height + mask (+ more) per stroke.
v0.3.0 attaches N (1/2/4/8) RGBA16F textures to ONE `GPUFrameBuffer`; the
dab fragment shader emits a distinct value per attachment (color, a scalar
packed in R, an inverted color, the falloff as a height-ish scalar), all
modulated by the **same** brush falloff alpha. N=1 is the untouched v0.2.0
path (`dab_frag_src(1)` returns the 0.2.0 source byte-for-byte).

### Design constraint found at the API level (not a probe surprise)

`gpu.state.blend_set` is **global** — the Python API has no per-attachment
blend form (no `glBlendFunci` equivalent). One blend mode applies to every
MRT attachment, so a stroke has **same-blend-per-stroke semantics across
all channels**. That matches the shared-mask model this spike implements
anyway (one disc test, one falloff, N values); it would only pinch a design
where e.g. color blends ALPHA while height blends ADD within one dab. The
`mrt_blend_alpha_all_attachments` probe verifies the global mode really is
applied to attachment 1 (and that output-slot routing works).

### Runtime probes (new in v0.3.0; printed on first draw)

| Probe line | Expected | Measured |
|---|---|---|
| `fb_max_color_slots` (tries 8, then 4, then 2) | 8 (GL/VK minimum guarantee is 8) | **4** exposed by Blender's wrapper (8 failed: maximum reported as 6) |
| `r16f_color_fb` | yes | **yes** |
| `mixed_format_mrt_rgba16f_r16f` (RGBA16F slot 0 + R16F slot 1, clear + read both) | yes → scalar channels can be 4× smaller | **NO** on this build (framebuffer read extent error) |
| `mrt_blend_alpha_all_attachments` (att0 ≈ (0.5,0,0.5), att1 ≈ (0.25,0,0.25)) | yes — blend is global; routing distinct | **yes**; expected values measured on both attachments |
| `fb_read_color_subrect` (x/y offsets honored; non-square read into numpy-wrapping Buffer is (h, w, 4) row-major) | yes | **yes** |
| `gpu_capabilities_memory` | none_exposed (analytic VRAM below) | **none_exposed** |

### Q1 — MRT dab cost (per-dab submission vs N=1 baseline)

Per-dab submission is uniform-upload + draw-call bound (v0.2.0 measured
0.02–0.03 ms at 4K, N=1); MRT adds fragment output work on the GPU side but
nothing per-dab on the CPU side, so the prediction is **no change in
`submit_avg_ms` with N** — the GPU absorbs the extra writes asynchronously,
bounded by `drain_ms`.

| Metric | N=1 (v0.2.0 baseline) | N=2 | N=4 | N=8 |
|---|---|---|---|---|
| `submit_avg_ms` @4K | **0.014 mean** (58 strokes) | not run | **0.019 mean** (12 strokes) | unavailable (4-slot ceiling) |
| `drain_ms` @4K | **2.25 mean** | not run | **2.43 mean** | unavailable |
| `submit_avg_ms` @2K | GUI-TBD | GUI-TBD | GUI-TBD | GUI-TBD |
| `drain_ms` @2K | GUI-TBD | GUI-TBD | GUI-TBD | GUI-TBD |

Falsifier: `submit_avg_ms` or `drain_ms` growing ~linearly with N would
mean MRT dabs are bandwidth-bound already at dab time (unlikely — a 50 px
dab touches ~10⁴ texels × N × 8 bytes ≈ 0.6 MB at N=8).

### Q2 — region-of-interest (sub-rect) readback

`fb.read_color` takes an x/y/w/h sub-rect per the API docs; the
`fb_read_color_subrect` probe verifies offsets and row order against known
content. Production now tracks a **conservative dirty rect**: per-triangle
screen bboxes (numpy, cached per prepass with the same matrices the dab
shader uses) are intersected with each flush's dab-disc bbox; the union of
hit triangles' UV bboxes bounds every texel the rasterizer can have
touched. Triangles crossing the near plane count as always-dirty;
occlusion-discarded texels only make the rect conservative, never wrong.
`readback_rect=WxH` appears in the stroke stats (`full` when disabled or
not smaller). Toggle: *Sub-rect Readback* in the panel.

One-time per-session characterization (logged as
`GPU_PAINT_SPIKE_READBACK_CHAR`, best of 2):

| Texture | 100% area | 25% area | 5% area |
|---|---|---|---|
| 2048² read ms | GUI-TBD (v0.2.0 measured ~23) | GUI-TBD | GUI-TBD |
| 4096² read ms | **90.52** | **24.91** | **5.05** |

The result is approximately linear in area, so the driver does not read the
whole surface regardless of the requested rectangle. Across the 12 measured
four-channel strokes, the dirty rectangle averaged **9.6%** of the texture
area (range **5.0–17.0%**).

**CPU-side finding (headless, measured 2026-07-11, this machine):** the
sub-rect only shrinks the GPU→CPU transfer — `Image.pixels` has **no
partial write**, so `foreach_set` stays full-cost per synced channel. The
sub-rect path's own CPU overhead is small:

| CPU cost (4K) | 100% | 25% | 5% |
|---|---|---|---|
| scatter sub-rect → full mirror | 19.1 ms | 5.7 ms | 1.1 ms |

(One-time: allocating 4× 4K float32 mirrors costs ~175 ms at first
finalize. Dirty-rect math: 0.02 ms/flush at 12 tris, **1.5 ms/flush +
39.6 ms/prepass at 100k tris** — visible in the new `dirty_ms` stat; at
heavy mesh counts the tracking, not the dabs, dominates flush CPU. Design
note for a real tool: coarse tile grid or GPU-side dirty mask instead.)

### Q3 — multi-channel sync-back (N reads + N Image writes)

Headless-measured CPU half (N × `foreach_set` + `update`, this machine):

| Texture | N=1 | N=4 | N=8 |
|---|---|---|---|
| 2048² pixels write total | 16.8 ms | 69.2 ms | 136.8 ms |
| 4096² pixels write total | 73.7 ms | 294.6 ms | 582.1 ms |

Perfectly linear (~17 ms/channel at 2K, ~74 ms at 4K) — no batching win or
penalty from multiple Image datablocks.

Per-stroke table to fill (one session per channel count per size):

| Metric | 2K N=4 | 2K N=8 | 4K N=4 | 4K N=8 |
|---|---|---|---|---|
| `fb_read_ms` (full) | GUI-TBD | GUI-TBD | GUI-TBD | GUI-TBD |
| `fb_read_ms` (typical sub-rect) | GUI-TBD | unavailable | **20.6–59.8; 33.2 mean** | unavailable |
| `pixels_write_ms` | ~69 (headless) | unavailable | **183.1–219.7; 187.3 mean** | unavailable |
| `syncback_total_ms` | GUI-TBD | unavailable | **212.5–297.8; 234.7 mean** | unavailable |

**Provisional arithmetic** (v0.2.0 measured reads: ~23 ms full at 2K,
~95 ms at 4K; assume linear-in-area sub-rect, typical stroke ≈ 25% of the
map):

- **2K, N=4, full reads:** 3 (drain) + 4×23 (reads) + ~0 (convert) +
  69 (writes) ≈ **165 ms** — inside the < 200 ms target with no sub-rect
  needed.
- **2K, N=8, full:** 3 + 8×23 + 137 ≈ **324 ms**; with 25% sub-rect reads
  ≈ 3 + 8×6 + 137 ≈ **188 ms** — sub-rect pulls N=8 under the bar.
- **4K, N=4, full:** 3 + 4×95 + 295 ≈ **678 ms**. With 25% sub-rect:
  3 + 4×24 + 23 (scatter) + 295 ≈ **417 ms** — still over budget, and now
  **dominated by the irreducible `foreach_set`**, not the transfer.
- **4K, N=8, full:** ≈ **1.35 s**. Sub-rect cannot rescue it alone.

**Measured verdict:** multi-channel painting is effectively dab-side free
and sync-side linear. At 2K, the arithmetic still predicts 4 channels fit the
< 200 ms pen-lift budget (N=8 needs sub-rect). At 4K the binding constraint
is not the GPU→CPU read (sub-rect fixes that) but Blender's full-image
`Image.pixels` write per channel — **dirty-channel-only sync plus a partial
Image write API (or deferring Image sync off the pen-lift path entirely,
e.g. background/idle sync) is what 4K multi-channel needs.** That is a
second concrete, citable API gap for the upstream pitch (alongside the
zero-copy `Buffer`→numpy bridge from v0.2.0).

### Q4 — VRAM (analytic; `gpu.capabilities` probe expected `none_exposed`)

RGBA16F = 8 bytes/texel per attachment; logged per session as
`GPU_PAINT_SPIKE_VRAM`:

| Texture | N=1 | N=2 | N=4 | N=8 |
|---|---|---|---|---|
| 2048² | 32 MB | 64 MB | 128 MB | 256 MB |
| 4096² | 128 MB | 256 MB | 512 MB | 1024 MB |

Plus per session: R32F + DEPTH32F prepass at viewport size (~16 MB at
1920×1080) and N full-size float32 CPU mirrors (64 MB each at 4K — 512 MB
of RAM at N=8/4K; a real tool would keep half-float or per-tile mirrors).
If `mixed_format_mrt_rgba16f_r16f=yes`, scalar channels (roughness,
height, mask) drop to R16F at 2 bytes/texel — a 4× saving on those
channels (4K scalar channel: 32 MB instead of 128 MB).

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
- **Push constants exceed 128 bytes** on the dab shader (2× MAT4 + the
  brush uniforms): Blender warns "minimum supported size of 128 bytes …
  Consider using UBO" at create-info population. Worked on the GL/Quadro
  run; a real tool should move the matrices to a UBO.
- **Sub-rect y-convention risk on non-GL backends:** full-frame reads
  round-trip by construction, but the dirty rect maps UV v → framebuffer y
  directly. Verified on OpenGL; if a Vulkan/Metal build ever clips strokes
  near the top/bottom of UV space, disable *Sub-rect Readback* and file the
  y-flip as a finding.
- **All channels are always dirty** in this spike (one shader writes all N
  per dab); per-channel dirty flags (paint only color+height, skip the
  rest at sync) are a real-tool concern, simulated here by comparing
  channel counts.

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

### v0.3.0 multi-channel verdict

- **MRT feasibility:** confirmed for **4 channels**, which is the maximum
  exposed by this Blender/OpenGL configuration. Eight-channel MRT could not
  be tested through the current wrapper.
- **Dab cost:** N=4 measured **0.019 ms/dab** versus **0.014 ms** at N=1;
  drain remained essentially flat (**2.43 vs 2.25 ms**).
- **Sub-rect:** confirmed and approximately area-linear. Measured N=4 stroke
  rectangles averaged **9.6%** of the 4K map.
- **Pen lift:** N=4 at 4K measured **234.7 ms mean**, **212.5–297.8 ms**
  range. This misses the <200 ms target narrowly even with small dirty
  rectangles because four full `Image.pixels` writes cost ~187 ms alone.
- **API escalation:** a partial Image write or deferred/background sync API
  is the clearest remaining requirement. Mixed RGBA16F/R16F attachments also
  failed on this build, preventing the expected scalar-channel VRAM saving.
