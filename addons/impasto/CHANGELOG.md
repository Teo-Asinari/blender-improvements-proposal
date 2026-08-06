# Impasto changelog

This file records shipped user-visible changes. Detailed historical engineering
notes remain available in
[docs/archive/PROGRESS_LEGACY.md](docs/archive/PROGRESS_LEGACY.md).

## 0.15.16

- Remove unused per-dab triangle/UV work from ordinary GPU Paint and Erase.
  Soften and Smear retain the detailed rectangles required for neighborhood
  sampling.
- Add bounded stroke profiling for input duration, flush time, UV bounds,
  seam selection, and GPU undo capture/commit. These measurements identify
  the next bottleneck without changing painting output.

## 0.15.15

- Add a separate default-off Conservative UV Seam Paint experiment for Paint
  and Erase. It extends only touched seam boundaries by less than one texel,
  evaluates brush coverage at the corresponding mesh edge, includes endpoint
  caps, and protects rasterized UV-island interiors.
- Limit conservative seam batches, undo capture, and dirty synchronization to
  seam faces intersecting the current stroke. Hard-disable the ineffective
  0.15.14 texel-center transport path.
- Known limitation: exterior gutter strips still lack ownership where islands
  are packed within roughly one texel. Tangent Normal, Soften, and Smear are
  not included in this experiment.
- User validation on a complex 4K production mesh confirmed that conservative
  boundary painting removes the persistent white, staircase-like gaps along UV
  island seams that the earlier padding and cross-island transport attempts did
  not resolve.

## 0.15.12

- Add default-off Experimental Seam Padding for resident GPU painting. After
  each stroke, exact UV-edge ownership extends complete channel texels eight
  pixels into island gutters, reducing white/filtering seams without changing
  UV-interior pixels.
- Apply seam padding only to targeted channel dirty regions, include expanded
  pixels in Undo/Redo and flush/save synchronization, and reuse the existing
  scratch texture rather than retaining another per-channel 4K canvas.

## 0.15.11

- Refresh brush mode, channel targets, brush parameters, pressure/stamp state,
  and stencil state between strokes without restarting GPU painting.
- Make stencil visibility and placement overlays follow live stencil changes
  during a resident painting session.

## 0.15.10

- Add a neutral-by-default, preview-only Roughness Readability light control
  for distinguishing low and medium roughness without changing painted data.

## 0.15.9

- Simplified canvas sizing to one persistent stack-wide selector so every
  channel created within a Paint layer remains GPU-session compatible.
- Removed per-channel resolution overrides.

## 0.15.8

- Added persistent stack-level canvas resolution selection for newly created
  Paint images at 1K, 2K, 4K, or experimental 8K.
- Added optional per-channel resolution overrides and made their
  non-destructive, new-images-only behavior explicit in the main panel.

## 0.15.7

- Added exact upper-layer mask composition for one visible same-UV image mask
  per affine non-normal layer, including opacity, inversion, and per-channel
  participation.
- Added exact named-UV reprojection for arbitrary ordered unmasked affine
  upper Paint layers while keeping active-layer-only writes.
- Kept nonlinear, independently mapped/multiple-mask, lower mixed-UV, and
  exact dynamic upper-RNM cases on authoritative Material Preview fallback.

## 0.15.6

- Replaced the two-upper-Base special case with arbitrary-depth ordered upper
  composition for compatible non-normal layers.
- Precompose every supported upper sequence into one GPU affine-transform
  texture per channel, keeping sampler use and live-preview draw cost fixed
  as layer count grows.

## 0.15.5

- Kept Lit PBR resident for an active material layer beneath ordered
  `Base + Emission` and `Base + Metallic + Roughness` Paint layers.
- Added a second ordered upper Base Color image path while preserving
  active-layer-only brush writes and the other sparse upper channels.

## 0.15.4

- Kept every visible active-layer channel in Lit PBR even when its brush
  target is disabled; brush targeting continues to control writes only.
- Made the complete preview mesh write depth during its draw so front
  triangles reject nearly coincident rear geometry.

## 0.15.3

- Fixed a misplaced initialization that suspended the Lit PBR draw callback
  when starting GPU painting after the preview UBO migration.

## 0.15.2

- Migrated every Lit PBR preview parameter from oversized push constants to
  one std140 uniform buffer for portable GPU-backend behavior.
- Kept live preview updates allocation-free and added layout, lifecycle, and
  real-GPU regression coverage.

## 0.15.1

- Added a persistent add-on preference for the default stencil directory and
  remember the folder of each successfully loaded stencil.
- Replaced the generic image opener with an image-filtered stencil browser that
  opens in thumbnail view and assigns the selected image directly.

## 0.15.0

- Added production layer-mask controls: add/remove/select, native grayscale
  painting, visibility, inversion, opacity, and per-channel participation.
- Mask canvases match their layer resolution, feed generated materials and
  flattened exports, and remain available as Blender Images after removal.
- Added persistent brush-material presets with capture/apply/remove controls,
  spherical color swatches, and full channel-value tooltips.
- Applying a preset changes material values without changing brush channel
  targets or active-layer ownership.

## 0.14.6

- Added live same-UV post-composition for affine upper Fill layers and one
  upper Paint image per active non-normal channel.
- Intermediate GPU painting now stays in Lit PBR and displays resident strokes
  immediately while preserving active-layer-only writes.
- Kept complex upper sequences, masks, nonlinear blends, and upper normals on
  the explicit authoritative-inspection fallback.

## 0.14.5

- Fixed intermediate sparse layers—such as emission-only Paint layers—so
  unrelated visible channels above and below remain composed in Lit PBR.
- Kept brush ownership isolated to channels on the active layer.
- Unsupported mixed-UV, masked, or same-channel upper compositions now retain
  authoritative Blender material inspection instead of showing active-only
  preview data as though it were the complete stack.

## 0.14.4

- Soften and Smear now copy only conservative, padded per-dab UV regions and
  render them back through a GPU scissor instead of copying whole textures.
- Reused one persistent scratch framebuffer and removed per-dab texture swaps
  and framebuffer reconstruction.
- Added exact 4K memory/copy estimates, a stable 1/4/8-channel benchmark
  matrix, and brush mode/target count in stroke telemetry.

## 0.14.3

- Removed the redundant Lit PBR depth-texture comparison that could reject thin
  surface strips and expose Blender's underlying material.
- Lit PBR now relies on Blender's framebuffer depth, a small clip-depth bias,
  smooth corner normals, and back-face culling for continuous, occluded preview.
- Verified the revised Lit PBR depth handling on a production mesh.

## 0.14.2

- Removed the obsolete `active_normal_blend` preview uniform after RNM made
  normal-layer color blend modes irrelevant, restoring GPU-paint startup.

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
