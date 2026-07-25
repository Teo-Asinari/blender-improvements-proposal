# Impasto roadmap

This is the authoritative list of open work for Impasto 0.15.4. Shipped work
belongs in [CHANGELOG.md](CHANGELOG.md), not here.

## Near-term

- Interactively benchmark Paint, Erase, Soften, and Smear at 4K with 1, 4, and
  8 channels. Treat 8K as experimental until latency, synchronization, undo,
  and memory behavior have been measured. See
  [high-resolution estimates](docs/HIGH_RESOLUTION_PERFORMANCE.md).

## Workflow and UX

- Add a pinned SSS Caliper mode that remains available outside an active GPU
  painting session.
- Improve Smear across rotated UV islands and seams.

## Architecture and compatibility

- Expand live upper-layer post-composition beyond one affine Paint image per
  non-normal channel: multiple images, masks, nonlinear blends, RNM normals,
  and mixed UV layouts.
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
- Paintable layer masks and persistent brush-material presets are implemented.
- The stencil browser has a persistent add-on-level default directory and
  opens in thumbnail view.
