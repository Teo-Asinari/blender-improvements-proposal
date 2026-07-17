# Impasto — multi-channel painting milestone progress

Current consolidated handoff: [`SESSION_2026-07-13.md`](SESSION_2026-07-13.md).
It records the shipped GPU-resident architecture, validation, known limits,
new brush/channel/stencil roadmap, and add-on-versus-core conclusions.

## 0.9.2 — self-occluding resident preview

- The resident preview now uses Impasto's front-surface depth policy, preventing
  biased rear triangles from showing through self-occluding geometry.

## 0.9.1 — legible Lit PBR and tablet continuity

- Added restrained GGX studio keys to Lit PBR so roughness, metallic, and
  tangent-normal strokes remain readable on ordinary dielectric materials.
- Lit PBR now interpolates Blender's corner normals, respecting smooth and flat
  mesh shading instead of reconstructing a faceted normal per triangle.
- Interpolated tablet pressure across generated dabs, ignored transient invalid
  pressure samples, and made spacing follow the pressure-adjusted brush size.

## 0.9.0 — normal-profile painting and focused UI

- Added adjustable, invertible alpha-profile tangent-normal painting while
  preserving registered multi-channel stencil coverage.
- Migrated dab parameters to a structured GPU uniform buffer for improved
  backend portability and room for future brush settings.
- Reorganized the sidebar around one selected paint engine and one primary
  action; channel details and advanced stencil/preview/sync controls now use
  progressive disclosure.

## 0.7.0 — image-based PBR and common-stack resident preview

- Replaced fixed preview lights with image-based studio lighting, roughness-
  filtered reflections, metallic/Fresnel energy response, and tone mapping.
- Same-UV stacks with the active Paint layer topmost now include lower Fill,
  Paint, and Kiln-normal layers in the resident preview without readback.
- Mixed UVs, image masks, and participating upper layers explicitly fall back
  to active-layer preview. Full arbitrary-stack GPU composition remains later
  work.
- Idle synchronization defaults off; Ctrl-S defers save until resident paint
  reaches Blender Images, while explicit inspection/save/export flushing and
  normal session-exit flushing remain available.

## 0.6.0 — stroke opacity and authoritative in-session inspection

- Added an explicit GPU Stroke Opacity multiplier; layer opacity remains the
  separate non-destructive contribution of the complete layer across channels.
- `V` now flushes the dirty resident region, hides the approximate overlay, and
  shows Blender's authoritative material without ending the session. `V` again
  resumes resident painting with GPU textures and undo history preserved.
- Optional Idle Material Synchronization can perform that handoff after an
  adjustable delay, but now defaults off so routine painting stays resident and
  zero-readback. `V` / Inspect Blender Material remains the explicit path.
- Ctrl-S/Ctrl-Shift-S defer saving until a draw-context GPU flush and Blender
  Image update complete; RMB/Esc still flushes before normal session exit.

## Newly requested roadmap TODOs

- Validate Emission and Subsurface resident preview against production renders
  across a wider range of HDRIs and object scales (painting is implemented).
- Extend the implemented image-stencil system from synchronized coverage masks
  to deliberate color/value texture application. Additional projection,
  transform, masking, and per-channel semantics await the user's detailed
  workflow description.
- Implement resident-GPU equivalents of Blender's useful brush families plus
  adjustable alpha/brush textures, staged from Draw-style stamps through
  destination-sampling and source/state-heavy tools.

## 0.5.2 — explicit resident settings mode

- `P` toggles a guaranteed input-pause state: no pointer event can create a
  dab, while sidebar controls remain usable and GPU textures/history stay
  resident. This replaces reliance on layout-dependent overlapping-region
  dispatch for editing brush values.

## 0.5.1 — overlapping sidebar input routing

- Explicitly gives Blender's visible UI, header, and toolbar regions priority
  over paint hit-testing. This fixes N-panel slider/color clicks registering as
  dabs when the sidebar overlaps the viewport WINDOW region.

## 0.5.0 — resident diagnostic previews and live brush editing

- Added Lit PBR, Raw Tangent Normal, Neutral Normal Lighting, and Height
  Grayscale display modes with live switching and staged 5/1/2/1-channel
  sampling respectively.
- Sidebar brush values now remain editable between GPU strokes without a
  flush or readback; resident textures and atomic undo history are preserved.
- Corrected transparent scalar defaults, partial normal strength, mirrored and
  degenerate UV tangent frames, and Height preview derivatives.

## 0.4.1 — projected-stroke continuity and live PBR preview

- Fixed staggered pinholes on steep and finely triangulated surfaces by making
  the front-surface test honor its intended bounded sub-pixel depth footprint.
  Discontinuity gates still prevent projection onto rear and occluded surfaces.
- Replaced the flat fixed-light resident overlay with a compact environment-style
  GGX preview, so metallic, roughness, tangent normal, and height remain legible
  during a GPU-resident stroke. Blender's material remains authoritative after
  flush/session exit; the live overlay is intentionally an approximation.

Inter-session handoff file. Starting point: checkpoint commit f38372f
(everything committed, full suite green).

## Already done at checkpoint (do not redo)

- `model.py`: per-binding `image_name` on `BindingModel`, `binding_image()`
  legacy fallback, per-binding image/uv/scalar node names
  (`n_binding_src/uv/scalar`), per-channel mask gate sockets
  (`mask:<key>`), compile routes each channel chain through its own
  image node. Golden + `test_multichannel_paint_images` green.
- `props.py`: `ImpastoBinding.image_name`; per-layer GPU brush props
  (`paint_color`, `paint_roughness`, `paint_metallic`, `paint_normal`,
  `paint_height_strength/_direction`, `brush_radius`, `brush_hardness`,
  `preview_channel`).
- `gpu_engine.py`: full MRT engine promoted from the spike — MRT dab
  shader generator (`dab_frag_src(channels, additive=)`), UV-space
  rasterization, R32F depth prepass occlusion, dirty-rect math,
  zero-copy readback ladder, `plan_target_batches` (MIX pass + ADD
  height pass over the same dab queue), per-image texture seeding,
  full-size CPU mirrors, `take_pending_pixels`/`record_sync_stats`.
- `snapshot.py` plumbs binding image; tests green.

## This session's plan (status updated as work lands)

1. [done] `PROGRESS.md` created.
2. [done] Pure payload helpers in `gpu_engine.py`:
   `GPU_PAINT_CHANNEL_KEYS`, `linear_to_srgb`, `stroke_payloads()`
   (per-channel MRT payload list; sRGB-encode COLOR channels; height =
   signed ADD payload via RAISE/LOWER).
3. [done] `model.SCHEMA_VERSION` -> 2; `engine.MIGRATIONS` gains
   1->2 migration copying legacy `layer.image_name` into SHARED
   bindings that lack their own canvas (per-binding form).
4. [done] `ops.py`: `layer_add` writes `binding.image_name` (explicit
   per-binding form) + `canvas_size` choice (1K/2K/4K, default 2K);
   `binding_add` on PAINT layers creates a dedicated per-channel image
   (size inherited from the layer's existing canvases) instead of
   rejecting; `paint_activate`/`detail_paint` gain `channel_key`.
5. [done] `paint.py`: channel-aware `activate_paint_target(context,
   layer, channel_key="")` — binding-resolved image, colorspace,
   source-node selection.
6. [done] `ops.py`: `IMPASTO_OT_gpu_paint` modal operator (spike
   pattern: enqueue dabs, draw callback flushes, Image writes in the
   modal op on pen-lift, RMB/Esc stops).
7. [done] `ui.py`: multi-channel brush box (per-bound-channel values,
   radius/hardness, preview channel, GPU paint button), per-binding
   native paint buttons, menu entries.
8. [done] Tests: `test_multichannel_paint.py`
   (IMPASTO_MULTICHANNEL_PASSED — operator-level per-binding images,
   colorspaces, graph wiring, native per-channel activation,
   migration), `test_gpu_paint.py` (IMPASTO_GPU_PAINT_PASSED — shader
   source per channel count, payload building, batch planning,
   dirty-rect math, headless session no-op, operator
   registration/poll/invoke). Updated `test_native_paint.py`
   (binding-aware source node; binding_add now creates a dedicated
   image) and `test_rendered_semantics.py` (canvas swap through the
   binding). `run_tests.sh` now also runs `test_model.py` and the two
   new files.
9. [done] README updated for the multi-channel workflow + resolution
   tradeoff (2K default; 4K N=4 pen-lift ~417 ms, Image.pixels-bound).
10. [done] Full suite run -> ALL_TESTS_PASSED (2026-07-12, all 11
    files: model, integration, native_paint, multichannel_paint,
    gpu_paint, scalar_channels, rendered_semantics, normal_paint,
    persistence, restore, undo).

## Key constraints (verified previously — trust these)

- Blender exits 0 after tracebacks: grep sentinels.
- gpu object creation raises SystemError headless: lazy + latched;
  headless session start must stay a harmless no-op.
- Modal op never touches gpu; draw callback never writes IDs.
- `Image.pixels` has no partial write; `foreach_set` full-array only.
- blend state global across MRT attachments -> height ADD pass is a
  separate framebuffer batch (already implemented in
  `plan_target_batches`).
- 2K x 4ch fits the ~200 ms pen-lift budget; 4K x 4ch does not
  (~417 ms) — default 2048, expose per-layer choice.

## Session 2026-07-12 (later): Material Preview PBR regression fixed

User report from GUI testing: "Impasto metallic and roughness channels,
as well as tangent normal, all seem broken in the material preview
pane" (base color behaves).

**Root cause** (one cause, all three channels): the MIX dab
framebuffer composites with `gpu.state.blend_set('ALPHA')` — i.e.
source-over with the destination in PREMULTIPLIED alpha (rgb:
SRC_ALPHA/ONE_MINUS_SRC_ALPHA, a: ONE/ONE_MINUS_SRC_ALPHA). The
pen-lift sync wrote that framebuffer content RAW into the canvases,
but canvases are STRAIGHT alpha and the compiled chains mix the RGB
*value* by the canvas *alpha*. So every texel the GPU brush deposited
with coverage a < 1 (the soft rim — half the footprint at default
hardness 0.5 — and any tablet pressure < 1) stored `value*a` where
`value` was painted:

- Metallic/Roughness: `mix(prev, v*a, a)` instead of `mix(prev, v, a)`
  — levels collapse toward 0 wherever coverage < 1.
- Tangent Normal: the premultiplied encode `(0.5,0.5,1)*a` decodes
  through the Normal Map node into garbage directions (tilts toward
  (-1,-1,-1)) around every stroke.
- Base Color has the *same* defect but it only reads as soft-rim
  darkening — hence "base color behaves".

Why the green suite missed it: `test_rendered_semantics.py` renders
EEVEE but only VALUE-mode bindings plus one alpha=1 height canvas;
nothing rendered a per-binding paint canvas containing coverage < 1
GPU-stroke content. (EEVEE itself is fine: headless EXR probes show
unpainted transparent canvases and straight alpha=1 content render
exactly right — verified before fixing.)

**Fix** (`gpu_engine.py`, write-path boundaries only — no node-graph
changes needed, the compiled graph was correct):

- `premultiply_canvas(arr)` / `unpremultiply_readback(arr)` — pure
  numpy helpers (headless-testable).
- `_ensure_gpu`: canvas -> GPU seed premultiplies MIX targets.
- `_finalize_stroke_gpu`: CPU mirrors (framebuffer space) premultiply
  their canvas seed; `pending_pixels` un-premultiplies MIX mirrors on
  a copy before `Image.pixels.foreach_set`. ADD (Height) targets
  round-trip raw, byte-identical to before (opaque a=1 canvas).
- `PREVIEW_FRAG_SRC`: un-premultiplies for display (the preview was
  double-darkening rims by drawing premult content with ALPHA blend).

**Tests**:

- `test_gpu_paint.py`: pure conversion checks (round-trip, a=0 rgb
  zeroing, mirror-copy semantics, soft-dab sync = value at coverage).
- NEW `test_pbr_canvas_semantics.py` (IMPASTO_PBR_CANVAS_PASSED, wired
  into run_tests.sh; 12 files now): EEVEE-rendered EXR probes of
  per-binding Metallic/Roughness/Normal canvases — unpainted band =
  channel default; straight alpha=1 band = painted value; a
  GPU-stroke band driven through the exact fixed pipeline math
  (seed premultiply -> source-over composite -> unpremultiply
  readback) = value mixed at coverage. Fails with the old raw sync
  (measured pre-fix: metallic 0.25 vs 0.5, roughness 0.5 vs 0.75,
  normals tilted garbage).

**Headless verification limits**: GPU objects can't exist in
`--background`, so the actual `blend_set('ALPHA')` equation and the
preview shader change are asserted against Blender's documented GPU
blend definitions + CPU simulation, not a live framebuffer. Material
Preview shading = EEVEE, and EEVEE renders were verified headless.
GUI pass should confirm: soft-rim strokes on metallic/roughness/normal
now shade correctly after pen-lift, and the in-stroke preview overlay
matches the synced result.

## Next session (if any)

- GUI acceptance pass (real strokes; checklist in README) — now also
  covers the premultiply fix above.
- GPU stroke undo (region snapshot/restore) — phase 6 productization.
- Seam padding/dilation post-pass. Dab matrices, stencil state, and MRT payloads
  now use a vec4-aligned UBO instead of oversized push constants.
