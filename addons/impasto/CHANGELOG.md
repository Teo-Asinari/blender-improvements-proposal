# Impasto changelog

This file records shipped user-visible changes. Detailed historical engineering
notes remain available in
[docs/archive/PROGRESS_LEGACY.md](docs/archive/PROGRESS_LEGACY.md).

## 0.14.1

- Rebuild now discovers a loose material-level `Kiln Bake Target` image and
  imports or refreshes it as the bottom RNM normal layer.

## 0.14.0

- Added bottom-up RNM composition for Kiln and Impasto tangent-normal layers.
- Kept generated Blender nodes, resident Lit PBR preview, and flattened Normal
  exports on the same alpha/mask-aware normal-composition semantics.
- Existing stacks upgrade in place through Rebuild Stack without replacing
  layers or painted images.

## 0.13.4

- Paint, Soften, Smear, and Erase independently remember their selected layer
  channels.
- Every brush mode provides All and None target shortcuts.
- Resident painting and stroke undo affect only the selected channels.

## 0.13.3

- Fixed GPU painting startup after Blender optimized the unused
  `resolved_stack` shader uniform away.

## 0.13.2

- Added All and None shortcuts to the Erase channel grid.

## 0.13.1

- Made the top-layer Lit PBR overlay continuous across the visible surface.
- Collapsed Emission and Subsurface brush-value sections by default.

## 0.13.0

- Added layer-aware targeted erasing, GPU Smear, and non-destructive
  Flatten/Export to combined per-channel Blender Images.
- Hardened preview startup, state restoration, and fallback behavior.

## 0.12

- Made stencil Paint Coverage and Normal Relief independent, allowing both in
  one stroke.
- Added persistent recent-color swatches and custom brush-mode icons.

## 0.11

- Added GPU Soften and Erase, combined stencil material/normal painting,
  per-channel image dimension readouts, and clearer brush-mode controls.

## 0.10

- Made grayscale stencil Normal Relief resolution-independent and split major
  UI, operator, and GPU responsibilities into focused modules.

## 0.9

- Added Emission and Subsurface painting, categorized stencil controls,
  configurable preview lighting, pressure opacity, Base Normal Map preview,
  Kiln-normal integration, improved occlusion, and the SSS Caliper.

## 0.7 and earlier

- Established GPU-resident multi-channel painting, atomic GPU undo, deferred
  image synchronization, diagnostic previews, PBR lighting, per-channel
  canvases, and the non-destructive Principled layer stack.
