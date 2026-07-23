# Impasto

Impasto 0.13.1 is a Blender 5.1 add-on for non-destructive, multi-channel PBR
painting. It stores material work as ordered Paint and Fill layers, compiles
the stack into a Principled BSDF material, and provides a GPU-resident painting
session with immediate material feedback.

Impasto is under active development. The GPU workflow is the primary painting
path. **Blender Brush Replay is an embryonic, fundamentally non-performant
prototype and is not intended for serious work.**

## Current feature set

- Ordered Paint and Fill layers with visibility, opacity, blend mode, and
  per-channel influence.
- One image canvas per painted channel, with 1K, 2K, or 4K layer resolution.
- The expanded Layer Channels view reports each bound image's actual pixel
  dimensions and warns when channel sizes differ, including imported or
  migrated images.
- Channels for Base Color, Metallic, Roughness, Tangent Normal, Height, Alpha,
  Emission Color/Strength, and Subsurface Weight/Radius/Scale.
- Post-creation channel expansion without replacing existing canvases.
- GPU multi-channel strokes with tablet-pressure control for size and opacity.
- Emission and Subsurface brush-value sections are collapsed by default and
  retain their disclosure state per Paint layer.
- A collapsed **Recent Colors** menu remembers up to eight colors actually
  used in each material stack. Base and Emission histories are separate,
  near-identical colors are consolidated, and the history persists in the
  `.blend`. Expand the menu and click the arrow beside a swatch to reuse it.
- A GPU-resident **Soften** brush that blurs all enabled active-layer channel
  canvases together; brush strength, falloff, and optional pressure control the
  effect without synchronizing images back to the CPU.
- A GPU-resident **Smear** brush that transports enabled active-layer channel
  pixels along the stroke. This first version maps screen direction onto
  texture axes; rotated UV islands and seams remain a refinement target.
- Layer-aware GPU erasing that removes active-layer coverage to reveal the
  layers below instead of painting black or neutral channel values.
- GPU-resident per-stroke undo and deferred synchronization to Blender Images.
- Lit PBR and diagnostic live previews.
- Image stencils as a viewport projection or brush-following alpha.
- Grayscale-stencil normal relief.
- Configurable preview lighting and a preview-only Base Normal Map fallback.
- Kiln baked-normal import/repair.
- A literal-scale SSS Caliper during GPU painting.

Subsurface IOR and Anisotropy can be registered for material control, but are
not GPU paint-canvas channels.

## Install

Impasto currently targets Blender 5.1.

1. Copy `addons/impasto/` into Blender's `scripts/addons/` directory, or zip
   the `impasto` folder so `impasto/__init__.py` is at the archive root.
2. Enable **Impasto** in Blender's Add-ons preferences.
3. Select a UV-unwrapped mesh with a node-based material.
4. Open `3D Viewport > N sidebar > Impasto`.

## Recommended workflow

1. Create an Impasto layer stack.
2. Add or select a Paint layer.
3. Expand **Layer Channels** and add the channels that layer should own.
4. Under **Brush Controls**, select **GPU Multi-Channel**.
5. Choose Paint, Soften, Smear, or Erase, then set Brush Radius, Brush Hardness, Brush
   Opacity, pressure behavior, and any channel values used by Paint mode.
   Erase exposes a compact **Erase Channels** grid for targeting any subset
   of the enabled layer channels. All are selected by default, and the
   selection is saved in the `.blend`.
6. Start GPU Painting.
7. Use LMB to paint. RMB or Esc flushes the resident canvases and exits.

During a session:

- `P` pauses/resumes dab capture so sidebar controls can be edited safely.
- `V` flushes current changes and temporarily shows Blender's authoritative
  material; use it again to resume the Impasto preview.
- Ctrl-Z / Ctrl-Shift-Z operate Impasto's atomic multi-channel stroke history.
- **Flush for Save / Export** synchronizes resident canvases to Blender Images.

Ordinary GPU strokes remain resident at pen-up. Synchronization is explicit or
performed when the session exits; 4K is viable, but uses substantially more
VRAM and makes synchronization slower.

## Flatten to channel images

The **Flatten / Export** box creates one new Blender Image for every enabled
stack channel without changing or deleting the source layers. Choose 1K, 2K,
or 4K; generated datablocks are named `Impasto Export <material> <channel>`
and can be packed safely into the `.blend`. Repeating the operation updates
images with matching names and dimensions.

Color channels are tagged sRGB; scalar, normal, height, and vector channels
are Non-Color. Tangent normals remain encoded RGB, height remains in its
stored data representation, and flattened outputs are opaque because the
channel's material default supplies a complete surface below all layers.
Source images are bilinearly resampled to the chosen size. Flush a resident
GPU session first. UDIM images and stacks using multiple UV maps are rejected
rather than producing a misleading result; file-path export is left to
Blender's Image editor.

## Painting engines

### GPU Multi-Channel

This is the intended engine. One stroke writes simultaneously to every enabled
paint binding on the selected layer. Base Color, Emission Color, and encoded
tangent normals use RGB values; scalar channels use grayscale values; Height
uses signed additive deposition.

The active Blender Draw brush contributes the basic stamp size, spacing,
strength, and supported pressure behavior. Impasto does not yet reproduce the
full behavior of every Blender brush asset. Clone, Smear, Soften, Fill,
Gradient, Mask, arbitrary brush textures, and custom falloff parity remain
incomplete.

### Blender Brush Replay (Prototype)

This path records a stroke and replays Blender image painting separately into
each target canvas after pen-up. It is slow, visually delayed, and cannot
provide the resident multi-channel behavior of the GPU engine. It remains only
as a compatibility experiment and should not be treated as a production
painting workflow.

Single-channel native image painting is still available from individual
channel rows when direct editing of one canvas is useful.

## Live preview

The resident preview offers:

- **Lit PBR** for approximate material evaluation.
- **Raw Tangent Normal** for encoded normal-map inspection.
- **Neutral Normal Lighting** for isolating Normal and Height response.
- **Height Grayscale** for inspecting the height canvas.

Lit PBR uses Impasto's own configurable studio lighting. It is not Blender
Material Preview. For common same-UV stacks with the active layer on top, it
can include eligible lower Paint/Fill layers. Upper participating layers,
mixed UV layouts, image masks, and other unsupported stack structures may fall
back to an active-layer-only preview. Use **Inspect Material** when Blender's
actual shader evaluation is required.

The preview-only **Base Normal Map** picker can display an existing tangent
normal image while painting. It does not alter the node graph, stack, render,
export, or source image. Kiln normal data can also be imported or repaired as a
baseline layer. Multiple opaque normal layers still use encoded-RGB mixing;
true RNM/UDN layered-normal composition remains unimplemented.

## Image stencils

The stencil image selector includes a cached thumbnail of the selected image.
The Preview Lighting popover includes Blender's spherical preview for the
active material; because resident strokes deliberately avoid routine
readback, that sphere represents the last synchronized material. Use Inspect
Material or finish the painting session to synchronize it.

An Image Stencil has three independent choices:

- **Placement:** fixed Viewport Stencil or brush-following footprint.
- **Image interpretation:** Alpha Channel or Grayscale.
- **Stencil Effects:** Paint Coverage and Normal Relief are independent toggles
  and can be enabled together. Coverage masks every enabled painted channel;
  relief derives tangent-normal detail from the same image.

Normal Relief derives tangent-space normal direction from grayscale gradients;
it does not interpret grayscale directly as normal-map RGB. See
[STENCIL_WORKFLOW.md](docs/STENCIL_WORKFLOW.md) for the detailed transform and
sampling contract. **Alpha Channel** only produces relief when the image has
varying transparency. For an opaque grayscale height image, select
**Grayscale** so relief is derived from its visible brightness. Normal Relief
can be used in the same stroke as Base Color, Metallic, Roughness, and other
enabled material channels: the derived gradient supplies Normal while the
stencil intensity remains the shared paint-coverage mask for those channels.

## Emission and subsurface painting

Emission Color and Emission Strength are independent channels. Strength is an
unclipped HDR scalar.

Principled subsurface color comes from Base Color. The paintable SSS controls
are:

- **Weight:** how much subsurface scattering contributes.
- **Scale:** the overall scene-space travel distance.
- **Radius RGB:** relative red, green, and blue travel distances.

The optional **Show SSS Caliper** overlay is currently visible only during GPU
painting. Its colored rings show the literal projected distances
`Scale × Radius R/G/B`; the white circle is the screen-sized brush radius.
There is no visual magnification. Extremely small distances produce a warning
relative to the mesh bounding-box diagonal.

## Storage and material ownership

Each Paint binding owns a dedicated Blender Image at the layer's resolution.
Display-color channels use sRGB storage; scalar, Height, and normal data use
Non-Color storage. Older single-canvas layer data migrates to the current
per-binding schema without replacing its images.

Impasto owns its generated root and per-layer node groups. Treat those node
graphs as build artifacts and edit the material stack through Impasto.
Removing a stack restores displaced pre-existing Principled links where they
were recorded.

## Important limitations

- The live preview is an Impasto approximation, not Blender Material Preview.
- GPU painting currently requires UV-mapped image canvases.
- Full arbitrary layered-normal composition is not implemented.
- Image masks are represented in the stack model but are not a complete
  production mask workflow.
- A synchronized material sphere and stencil thumbnail are available. A real
  recent-material preset palette with parameter tooltips remains roadmap work.
- The SSS Caliper is tied to an active GPU paint session; a persistent pinned
  inspection mode remains future work.
- GPU canvases consume real VRAM. One 4K RGBA16F channel is approximately
  128 MB before preview, depth, and undo resources.
- Resident painting also keeps one full-size RGBA16F scratch texture regardless
  of channel count. The minimum active-canvas allocation is therefore roughly
  `(channels + 1) × 128 MiB` at 4K and `(channels + 1) × 512 MiB` at 8K,
  before baseline textures, viewport depth, Blender's own image textures, and
  up to 256 MiB of tile undo snapshots.
- 8K creation is not currently exposed in the UI. Soften and Smear perform a
  full texture copy per enabled channel per dab, making them especially poor
  8K candidates until dirty-region copies are implemented.

See [High-resolution painting estimates](docs/HIGH_RESOLUTION_PERFORMANCE.md)
for formulas, VRAM/RAM tables, expected responsiveness, and qualification
requirements.

## Tests and development notes

Runtime code is separated by responsibility: focused GPU helpers live under
`gpu/`, paint-panel rendering lives in `ui_paint.py`, channel menus in
`ui_channels.py`, and reusable operator mechanics in `operator_support.py`.
Historical design notes live under `docs/`. The original `gpu_engine`, `ops`,
and `ui` module paths remain compatibility facades while further safe
decomposition continues.

Run the Blender regression suite with:

```bash
addons/impasto/tests/run_tests.sh
```

The runner checks explicit success sentinels because Blender may exit with a
zero status after a Python exception. Current release history, validation, and
open work are tracked in [PROGRESS.md](PROGRESS.md). Architectural background
is in [`../../research/layer-stack-design.md`](../../research/layer-stack-design.md).

## License

GPL-2.0-or-later.
