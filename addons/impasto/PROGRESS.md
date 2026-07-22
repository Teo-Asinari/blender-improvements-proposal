# Impasto — multi-channel painting milestone progress

## 0.13.0 — targeted erase, smear, export, and preview hardening

- Added GPU-resident Smear across every enabled active-layer channel with
  pressure-scaled strength and no routine GPU-to-CPU synchronization.
- Direction mapping is presently screen-to-texture approximate;
  projection-aware transport over rotated UV islands and seams remains.
- Erase can target a persistent subset of the active layer's enabled channels;
  all channels remain selected by default.
- Non-destructive Flatten to Channel Images creates explicit 1K/2K/4K channel
  images while preserving the source stack; UDIM and mixed-UV stacks remain
  unsupported.
- Added stencil and synchronized-material previews plus deterministic resident
  GPU resource teardown.
- Confirmed with exact payload/UBO-slot tests that bound Metallic and Roughness
  values survive combined Paint Coverage and Normal Relief strokes.

## 0.12.1 — independent stencil effects

- Paint Coverage and Normal Relief are independent toggles and can run in the
  same stencil stroke.
- Effect selection is visually separated from Stamp Opacity, Relief Strength,
  and relief inversion controls.

## 0.12.0 — persistent color swatches and custom brush icons

- Added a collapsed per-material-stack Recent Colors menu for Base and Emission paint,
  with persistent, deduplicated eight-color histories captured on real paint
  strokes rather than continuously while dragging Blender's color picker.
- Soften and Erase use project-owned icons that depict diffusion and active
  erasure instead of unrelated Blender stock glyphs.

## 0.11.3 — legible brush-mode selector

- Replaced the sparse icon-only mode controls with a compact segmented row
  labeled Paint, Soften, and Erase.
- Soften and Erase now use Blender's actual smoothing and tablet-eraser icons.

## 0.11.2 — clearer brush-mode controls

- Paint, Soften, and Erase are separate icon buttons in a boxed Brush Mode
  group, visually separated from radius, hardness, and opacity controls.
- Each mode button exposes its behavior through the enum tooltip.

## 0.11.1 — channel image dimension readout

- The expanded Layer Channels view now shows the real Blender image datablock
  dimensions for each paint channel and warns when bound channel sizes differ.
- Missing images and unavailable dimensions remain explicit without confusing
  the readout with the layer's creation-resolution setting.

## 0.11.0 — combined stencils, erase, and soften

- Normal Relief can paint alongside the other enabled material channels.
- GPU Erase removes active-layer coverage to reveal lower layers.
- GPU Soften applies a resident 3×3 blur across enabled layer channels; this
  correctness-first path still needs interactive performance qualification.

## 0.10.1 — usable stencil Normal Relief

- Normal-profile central differences are converted from per-texel change to
  normalized-image derivatives, keeping relief strength stable across source
  resolutions and non-square images.
- Alpha mode now warns that opaque grayscale images must use Grayscale to
  provide meaningful relief gradients.

## Active roadmap

- Diagnose and fix top-layer Lit PBR preview regressions: intermittent white
  surface stripes/holes and unexpected flat-looking shading. The likely seams
  are front-surface depth rejection and the corner-normal fallback, but both
  require reproduction before changing tolerances or normal policy.
- Implement real layer masks with painting, visibility/invert controls, and
  predictable per-channel/layer application.
- Interactively qualify the implemented GPU eraser, which removes resident
  stroke coverage atomically across enabled channels rather than painting
  replacement values.
- Add spherical material previews, stencil thumbnails, and a recent-material
  palette with channel-value tooltips.
- Add a pinned SSS Caliper inspection mode outside active GPU painting.
- Implement RNM/UDN composition for multiple tangent-normal layers and expand
  resident preview support beyond the common same-UV stack.
- Continue decomposing the compatibility-facade internals of `gpu_engine.py`
  and `ops.py` behind regression-guarded packages.

## 0.10.0 — package decomposition

- Extracted pure brush math, SSS caliper math, and viewport overlays into the
  `gpu/` package while preserving the `gpu_engine` API.
- Extracted reusable operator mechanics, channel menus, and paint-panel
  rendering from the former `ops.py` / `ui.py` kitchen sinks.
- Added public-import, operator-ID, saved-property, registration lifecycle,
  source hygiene, and package-layout regression guards.
- Moved historical design notes into `docs/`; README and PROGRESS remain at
  the add-on root.

## 0.9.19 — literal SSS caliper scale

- Removed all automatic visual magnification from the SSS rings.
- Rings now always use their literal projected scene-space radii; very small
  effective distances produce a mesh-relative warning instead.

## 0.9.18 — live and self-identifying SSS caliper

- The Show SSS Caliper toggle now updates during an active GPU paint session.
- Each colored circumference is labeled R, G, or B; overlay text explicitly
  distinguishes the screen-sized white brush circle from the SSS rings.
- Caliper magnification is mesh-relative, preventing zoom thresholds from
  making the colored rings jump discontinuously.
- The hover tooltip explains the colors, equation, white circle, and current
  GPU-session scope.

## 0.9.17 — subsurface caliper

- Optional cursor-centred RGB rings visualize effective Subsurface distances
  (`Scale × Radius RGB`) during GPU painting.
- Ring projection uses the actual mesh ray hit under the cursor, so scene
  distance is converted at the correct view depth rather than confused with
  brush pixels. Off-mesh cursors intentionally show no caliper.
- The readout includes compact scene-unit values, percentages of the world
  bounding-box diagonal, and a labelled power-of-ten magnifier for distances
  too small to see directly.
- Immutable ring geometry and its shader are cached for the paint session.

Current consolidated handoff: [`SESSION_2026-07-13.md`](docs/SESSION_2026-07-13.md).
It records the shipped GPU-resident architecture, validation, known limits,
new brush/channel/stencil roadmap, and add-on-versus-core conclusions.

## 0.9.16 — brush-wide controls first

- Brush Radius, Brush Hardness, Brush Opacity, and pressure controls now
  precede the material values they affect.
- A `Painted Channel Values` heading separates brush-wide behavior from Base
  Color, Roughness, Metallic, and other channel attributes.

## 0.9.15 — clearer channel and brush-control hierarchy

- Layer bindings now live in a distinct `Layer Channels` box, separate from
  the `Brush Controls` section.
- The engine selector is explicitly labeled `Painting Engine`.
- Blender Brush Replay is labeled and warned as a fundamentally
  non-performant prototype demo not intended for serious painting.

## 0.9.14 — collapsible extended channel sections

- The expanded layer Channels view keeps core channels visible while grouping
  Emission and Subsurface bindings into separate collapsed sections.

## 0.9.13 — distinct layer-creation tooltips

- Paint-layer buttons now explain that they create image canvases for brush
  strokes; Fill-layer buttons explain that they apply uniform values.

## 0.9.12 — compact grouped layer badges

- Layer rows now keep core initials compact and collapse extended channels
  into counts, for example `BMRNH E(2) SS(3)`.

## 0.9.11 — clearer paint groups and channel badges

- Emission and Subsurface values now occupy separate labeled groups in the
  Paint section.
- Layer-list channel badges use unique short codes (`EC`, `ES`, `SW`, `SR`,
  `SS`, etc.) instead of ambiguous first letters.

## 0.9.10 — clearer subsurface controls

- Added hover descriptions explaining Subsurface Weight, Radius, and Scale.
- Added concise in-panel guidance: Weight controls amount, Scale controls
  travel distance, and Radius controls relative RGB travel.

## 0.9.9 — expand existing stacks with paint channels

- Added a grouped Add Material Channel menu beside Channels. One click safely
  registers a missing channel and binds it to the selected Paint/Fill layer;
  a register-only submenu remains available for stack-level channels.
- Standard stacks can now gain Emission Color/Strength and paintable
  Subsurface Weight/Radius/Scale without recreation or existing-data loss.
- New Paint canvases inherit layer resolution and channel colorspace/domain;
  registry order, displaced Principled links, duplicate safety, undo, compiler
  wiring, and save/reopen persistence are covered by Blender regressions.
- SSS IOR and Anisotropy remain register-only for Paint layers because the GPU
  brush intentionally exposes the ten defined paintable channels.

## 0.9.8 — categorized stencil controls

- Reorganized stencil settings into explicit Placement, Read Image From, and
  Apply As groups, with mode-specific descriptions and transforms.
- Renamed ambiguous display labels: Brush Footprint describes placement,
  Alpha Channel/Grayscale describe the sampled data, and Paint Coverage/Normal
  Relief describe the effect. Stored identifiers and saved files remain
  compatible.

## 0.9.7 — preview-only Base Normal Map

- Added an explicit Base Normal Map fallback in the Preview Lighting popover
  with Blender image/file selection, independent UV map, strength, and
  green-channel inversion.
- Base and painted normals use their own derivative tangent frames and combine
  in world space for Lit, Raw, and Neutral resident previews.
- Image and UV changes rebuild only preview resources in the owning draw
  context; source images, material nodes, stack state, renders, and exports are
  untouched.
- Added real foreground GPU coverage for authoritative RGB upload, independent
  UVs, live settings, missing resources, and preview shader compilation.

## 0.9.6 — pressure-opacity calibration

- Compensate pressure-controlled per-dab alpha for predictable source-over
  overlap, so dense continuous strokes converge on the intended tablet
  opacity instead of saturating nearly opaque at light pressure.

## 0.9.5 — visible stencils, explicit pressure, and Kiln wiring

- Added GPU-resident textured stencil previews: a camera-facing planar overlay
  for Viewport Stencil and a cursor-following footprint for Brush Alpha, both
  using the exact paint transform and a visible projection boundary.
- Added persistent Pressure Opacity and Pressure Size controls which override
  hidden Blender brush flags and update live without image synchronization.
- Build the resident stack plan before the first preview allocation, restoring
  lower/Kiln baseline textures during ordinary top-active painting sessions.
- Preserve a selected Kiln normal canvas as opaque data during resident upload;
  a real foreground GPU regression now checks both top- and lower-active cases.

## 0.9.4 — preserve opaque baseline image RGB

- Opaque lower-channel images now upload from raw Blender pixels with forced
  alpha, preventing alpha-zero Kiln bakes from losing their tangent-normal RGB
  before resident baseline composition.
- Expanded resident normal-stack regression coverage and refreshed the global
  and add-on READMEs for the 0.9.x painting and preview improvements.
- Follow-up viewport testing confirmed this did not solve general lower-normal
  layering: opaque upper normal maps still replace lower maps under encoded-RGB
  MIX. RNM/UDN vector composition remains required and unimplemented.

## 0.9.3 — configurable preview lighting and Kiln normals

- Added live preview-only environment exposure/rotation, key strength/rotation,
  and fill strength in a compact lighting popover without texture readback.
- Kiln baked-normal baselines now ignore non-authoritative bake alpha, including
  legacy imported layers, so they remain visible in Lit PBR.

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

- Post-creation channel registration and optional selected-layer binding are
  implemented. Emission Color/Strength and paintable Subsurface
  Weight/Radius/Scale create correctly typed canvases without replacing
  existing layer data; IOR and Anisotropy remain register-only. Consider
  one-click bundle presets after the explicit grouped menu is user-qualified.

- Qualify the preview-only Base Normal Map fallback across production meshes
  and backend/driver combinations. It explicitly samples one chosen image/UV
  with strength and green inversion in resident previews only; it intentionally
  does not alter the material graph, layer stack, images, renders, or exports.
- Longer term, replace that display-only bridge with arbitrary-UV resident
  normal-stack composition and defined RNM/UDN semantics for opaque detail
  maps.
- Auto-populate the preview-only Base Normal Map from the current material's
  Kiln bake target and recorded UV when no manual image is selected. Keep the
  manual picker authoritative and display the resolved source explicitly.

- Validate Emission and Subsurface resident preview against production renders
  across a wider range of HDRIs and object scales (painting is implemented).
- **Paint Coverage is already implemented:** it uses stencil alpha/grayscale
  as the mask for the configured brush values. A distinct possible feature is
  sampling stencil pixels as the deposited color/scalar values themselves;
  that is not required for the existing coverage-mask workflow.
- Normal Relief now warns that Alpha Channel requires varying transparency;
  opaque grayscale height images must use Grayscale. The same distinction is
  stated in selector tooltips and the README.
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
