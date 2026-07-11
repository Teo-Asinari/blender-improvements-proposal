# GPU Paint Spike — Findings

**Status: headless facts measured; GUI numbers pending (slots marked `GUI-TBD`).**
Environment: Blender **5.1.2** (`ec6e62d40fa9`, 2026-05-19), Windows binary run
from WSL2; numpy 2.3.4 bundled.

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
   the GPU (1×1 read), reads the framebuffer back
   (`fb.read_color(..., 'FLOAT')` → `gpu.types.Buffer` → numpy), and the
   modal operator writes `image.pixels.foreach_set(arr)` — never from a draw
   callback.
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

Conclusions already safe to draw headless:

- `foreach_set` is the right write path (≈4× faster than slice assignment);
  cost scales linearly with texel count (~4.6 ms per Mpixel).
- Even at 4K the **CPU share of a stroke-end sync-back is < 80 ms** — paid
  once per stroke, this alone cannot sink the spike question. The open
  half is the GPU→CPU transfer (`fb.read_color`), which is GUI-only.

## Measurements — GUI (protocol in README.md)

The add-on prints one `GPU_PAINT_SPIKE_STROKE key=value …` line per stroke
and one `GPU_PAINT_SPIKE_PROBE …` block on session start. Paste results here.

### Runtime capability probes (printed on first draw) — `GUI-TBD`

| Probe line | Expected | Measured |
|---|---|---|
| `backend=… vendor=… renderer=…` | (records the GPU/backend under test) | GUI-TBD |
| `rgba16f_color_fb` | yes | GUI-TBD |
| `blend_alpha_into_offscreen_attachment` | yes (center ≈ `[0.5, 0, 0.5, …]`) — if **NO**, ping-pong fallback becomes necessary and per-dab cost roughly doubles | GUI-TBD |
| `gputexture_read_rgba16f` | records numpy dtype/shape of `tex.read()` | GUI-TBD |
| `r32f_color_fb` | yes | GUI-TBD |
| `depth32f_attach_clear_read` | yes (cleared=1.000) | GUI-TBD |
| `paint_tex_seeded_from_image` | yes (Buffer-from-numpy path works) | GUI-TBD |

### Per-dab / per-stroke timings — `GUI-TBD`

All from the `GPU_PAINT_SPIKE_STROKE` console lines. Note `submit_*` are CPU
**submission** times (the GPU runs asynchronously); `drain_ms` (1×1 read
after the last dab) bounds true GPU completion of the whole stroke.

| Metric | Target | 1K | 2K | 4K |
|---|---|---|---|---|
| `submit_avg_ms` (per dab) | < 2 ms | GUI-TBD | GUI-TBD | GUI-TBD |
| `submit_max_ms` | — | GUI-TBD | GUI-TBD | GUI-TBD |
| `dabs_per_s` (fast scribble, ≥ 2 s) | 100s | GUI-TBD | GUI-TBD | GUI-TBD |
| `drain_ms` (stroke-end GPU drain) | small vs stroke | GUI-TBD | GUI-TBD | GUI-TBD |
| `prepass_ms` (per view change) | ≪ frame budget | GUI-TBD | GUI-TBD | GUI-TBD |
| `fb_read_ms` (full readback) | — | GUI-TBD | GUI-TBD | GUI-TBD |
| `tex_read_ms` (probe alternative) | — | GUI-TBD | GUI-TBD | GUI-TBD |
| `to_numpy_ms` | ~0 | GUI-TBD | GUI-TBD | GUI-TBD |
| `pixels_write_ms` | ≈ headless table | GUI-TBD | GUI-TBD | GUI-TBD |
| `syncback_total_ms` (once per stroke) | < 150 ms @2K | GUI-TBD | GUI-TBD | GUI-TBD |

Also record: mesh (name, triangle count), viewport size, GPU model.

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

**Provisional: YES — feasible, with medium confidence, pending the GUI
numbers above.**

Reasoning from measured facts:

- Everything the technique needs **exists and is probed present** on 5.1.2:
  arbitrary framebuffers with float color + depth32F attachments, samplers in
  create-info shaders, Buffer-seeded textures, framebuffer readback with
  format conversion, blend/depth state control around offscreen passes.
- The **CPU half of the pipeline is already measured and cheap**: 19 ms
  `foreach_set` at 2K once per stroke; dab bookkeeping is O(dabs) Python
  arithmetic.
- The per-dab GPU work (one mesh draw + trivial fragment math at 2K) is the
  kind of load a viewport draws hundreds of times per frame; the open risks
  are Python *submission* overhead per dab (uniform sets + `batch.draw`)
  and whether fixed-function blending works in custom framebuffers on the
  active backend (probe line decides; fallback = ping-pong at ~2× cost).

Falsifiers to watch for in the GUI run — any of these flips the verdict:

1. `blend_alpha_into_offscreen_attachment=NO` **and** ping-pong pushes
   `submit_avg_ms` ≥ 2 ms.
2. `submit_avg_ms` ≥ 2 ms on a modest mesh (≤ 100k tris) at 2K.
3. `drain_ms` growing linearly into hundreds of ms per stroke (GPU can't
   keep up with submission; dabs/sec collapses).
4. Paint mirrored/offset from the cursor with no fixable convention flip.
