# Impasto roadmap

## UV seam-safe paint padding

Development branch: `feature/impasto-uv-gutter-padding`. The feature remains
production-disconnected and makes no changes to painting yet.

- The pure ownership foundation lives in `gpu/uv_gutters.py`: triangles are
  grouped by mesh-edge and UV continuity, never by paint alpha.
- A production-disconnected compact GPU prototype now propagates deterministic
  local source offsets through an `RG16F` map using only padding-bounded jump
  steps. Its Blender/OpenGL tests pass; propagation is approximate rather than
  globally nearest for adversarial seed layouts.
- Next, rasterize UV interiors into the immutable seed map once per UV map and
  resolution. The retained map costs 16 MiB at 2K, 64 MiB at 4K, or 256 MiB
  at 8K; construction temporarily doubles that GPU allocation and uses a
  float32 CPU seed allocation twice the retained-map size.
- Partial, unvalidated groundwork exists for UV/resolution cache keys, direct
  GPU seed-raster shaders, and warnings for out-of-range, very small, and
  exactly duplicated UV triangles. These diagnostics do not yet detect every
  partial overlap, and the small-triangle warning is heuristic.
- At pen-up, process only the stroke dirty rectangle expanded by the padding
  radius, ping-pong-copying the complete resident texel (premultiplied RGBA
  for MIX channels; unchanged raw values for ADD channels).
- Expand tile-undo capture and session readback bounds by the same radius.
  Never overwrite occupied UV texels. Overlapping UV faces remain ambiguous
  and must be diagnosed rather than silently padded.

Remaining sequence:

1. Complete and foreground-test seed rasterization, session lifecycle,
   invalidation, cleanup, logging, and an experimental default-off toggle.
2. Integrate targeted-channel pen-up copying without overwriting UV interiors.
3. Capture expanded gutter regions before the first dab for Undo/Redo and
   include them in flush/save dirty bounds.
4. Stress-test Smart UV atlases at 2K/4K; keep 8K experimental because the
   retained compact map alone costs 256 MiB.

Estimate: two to four focused engineering passes for an experimental working
version, and four to seven for a robust release-quality implementation.

This is the authoritative list of open work for Impasto 0.15.11. Shipped work
belongs in [CHANGELOG.md](CHANGELOG.md), not here.

## Near-term

- Fix orphaned GPU-paint sessions when the owning Paint layer or stack is
  deleted. Confirmed failure: the modal timer calls
  `_refresh_stroke_settings()`, raises `PaintTargetError("The active paint
  layer disappeared")`, and exits without `_finish()`, leaving resident GPU
  resources/status overlays alive while pointer events return to Blender.
  Until fixed, recover from Blender's Python Console with
  `from impasto import gpu_engine; gpu_engine.stop_session()`. The final fix
  must catch missing-target failures on every modal refresh path, always
  remove timers/draw handlers/resources, and either prevent deletion during a
  resident session or stop/flush it explicitly before deletion. Add regression
  coverage for layer deletion, whole-stack removal, and left-handed input.
- Improve roughness readability beyond the current supplemental studio light.
  Add an optional, clearly identified diagnostic view or stronger
  preview-only contrast control while keeping the neutral preview unchanged
  and never modifying painted roughness data.
- Interactively benchmark Paint, Erase, Soften, and Smear at 4K with 1, 4, and
  8 channels. Treat 8K as experimental until latency, synchronization, undo,
  and memory behavior have been measured. See
  [high-resolution estimates](docs/HIGH_RESOLUTION_PERFORMANCE.md).

## Workflow and UX

- Add a pinned SSS Caliper mode that remains available outside an active GPU
  painting session.
- Improve Smear across rotated UV islands and seams.

## Architecture and compatibility

- Investigate format-optimized resident paint targets: keep color and normal
  channels in `RGBA16F`, but store scalar channels such as Metallic,
  Roughness, Subsurface Weight, and Emission Strength in `R16F`. For seven
  representative channels this would reduce one resident copy from about
  896 MiB to 512 MiB at 4K, or 224 MiB to 128 MiB at 2K. Because the current
  OpenGL probe supports both formats separately but not mixed-format MRT,
  implementation requires separate RGB/scalar draw batches plus readback,
  preview, compositing, undo, and backend qualification. Treat this as later
  performance work, after UV seam-safe padding correctness.
- Expand live upper-layer post-composition beyond arbitrary ordered affine
  non-normal layers, one same-UV mask per upper layer, and named-UV
  reprojection for unmasked upper Paint layers. Remaining boundaries are
  multiple or independently mapped masks, nonlinear blends, exact dynamic
  upper RNM normals, and mixed-UV lower/static-only channels.
- Continue decomposing `gpu_engine.py` and `ops.py` compatibility facades into
  focused, regression-guarded modules.
- Continue qualification across supported GPU backends and drivers.

## Explicitly not open

- Brush mode, channel targets, brush parameters, and stencil settings now
  refresh live between strokes in a resident GPU painting session.
- Embedding Eevee inside the resident GPU painting overlay is not planned.
  Instead, improve Lit PBR parity with Blender and provide diagnostic channel
  views; Eevee remains the authoritative post-flush material preview.
- Flattening the stack to combined per-channel images is implemented.
- Paint, Soften, Smear, and Erase already have independent per-channel target
  toggles with All/None shortcuts.
- The preview-only Base Normal Map picker is implemented and has been
  user-validated as a useful, reliable manual fallback. Automatic Kiln
  discovery and true layered-normal composition remain open.
- Stencil Paint Coverage and Normal Relief can be enabled together.
- Kiln and Impasto normal layers use bottom-up RNM composition in the
  generated material, resident preview, and flattened Normal export.
- Rebuild automatically imports or refreshes a loose material-level
  `Kiln Bake Target` as the bottom normal layer.
- Paintable layer masks and persistent brush-material presets are implemented.
- The stencil browser has a persistent add-on-level default directory and
  opens in thumbnail view.
