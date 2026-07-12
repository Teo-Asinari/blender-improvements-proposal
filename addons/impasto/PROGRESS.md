# Impasto — multi-channel painting milestone progress

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

## Next session (if any)

- GUI acceptance pass (real strokes; checklist in README).
- GPU stroke undo (region snapshot/restore) — phase 6 productization.
- Seam padding/dilation post-pass; UBO for dab matrices (>128 B push
  constants warn on some backends).
