# Impasto roadmap

This is the authoritative list of open work for Impasto 0.14.3. Shipped work
belongs in [CHANGELOG.md](CHANGELOG.md), not here.

## Near-term

- Implement production layer masks: paintable mask canvases, visibility and
  invert controls, predictable layer/channel scope, and mask-aware export.
- Optimize Soften and Smear with dirty-region copies. Their current full
  texture copy per selected channel per dab is unsuitable for 8K and can be
  expensive at 4K.
- Interactively benchmark Paint, Erase, Soften, and Smear at 4K with 1, 4, and
  8 channels. Treat 8K as experimental until latency, synchronization, undo,
  and memory behavior have been measured. See
  [high-resolution estimates](docs/HIGH_RESOLUTION_PERFORMANCE.md).
- Interactively qualify the revised Lit PBR depth handling on production meshes,
  especially close or intersecting geometry.

## Workflow and UX

- Add a material preset palette with spherical thumbnails and channel-value
  tooltips. Recent color swatches, a synchronized material sphere, and stencil
  thumbnails already exist.
- Add a pinned SSS Caliper mode that remains available outside an active GPU
  painting session.
- Improve Smear across rotated UV islands and seams.

## Architecture and compatibility

- Expand resident full-stack preview beyond the common same-UV arrangement,
  including participating upper layers and mixed UV layouts.
- Continue decomposing `gpu_engine.py` and `ops.py` compatibility facades into
  focused, regression-guarded modules.
- Continue qualification across supported GPU backends and drivers.

## Explicitly not open

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
