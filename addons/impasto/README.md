# Impasto

Impasto is an in-development, non-destructive PBR layer stack for Blender
materials. Phase 1 establishes the stack data model and compiler: fill layers,
pass-through groups, channel bindings, paint-mask graph structure, minimal node
reconciliation, and a small 3D Viewport sidebar for creating and arranging the
stack without working directly in the Shader Editor.

> **Development status:** Phase 1 stack foundation, the Phase 3
> native-paint workflow, and the first multi-channel painting milestone:
> one logical Paint layer with a separate canvas per channel, painted
> either natively (one channel at a time), by replaying one active Blender
> brush stroke across every channel, or with the experimental GPU brush.
> Masks, channel isolation, and
> bake-down/export remain later work.

## Phase 1 scope

The current milestone includes:

- stack, layer, binding, mask, and material state stored in the generated root
  shader node group;
- fill layers and pass-through groups;
- Base Color and Roughness bindings;
- pure model-to-graph compilation with committed golden specifications;
- minimal reconciliation that repairs drift and produces zero mutations when
  the graph already matches;
- two-tier debounce so uniform edits do not structurally rebuild the graph;
- a minimal **Impasto** N-panel with stack creation, layer add/remove/reorder,
  visibility, opacity, blend controls, and manual rebuild.

The binding design and complete roadmap live in
[`../../research/layer-stack-design.md`](../../research/layer-stack-design.md).

## Install for development

Impasto uses Blender's legacy add-on packaging (`bl_info`) and targets Blender
5.1.2 during Phase 1.

1. Zip the `impasto` folder itself, so the archive contains
   `impasto/__init__.py` at its root.
2. In Blender, choose `Edit > Preferences > Add-ons > Install from Disk...`,
   select the zip, and enable **Impasto**.

For local development, copy or symlink `addons/impasto/` into Blender's
`scripts/addons/` directory. Do not include `tests/` or `__pycache__/` in a
release archive.

## Paint with Blender's native brush

1. Select a mesh with a material that uses nodes and contains a Principled
   BSDF.
2. Open the 3D Viewport sidebar with `N`, then choose the **Impasto** tab.
3. Create a new layer stack.
4. Add a Paint layer. It creates a transparent 2048 x 2048 image using the
   mesh's currently active UV map.
5. Select that layer and click **Start Painting**. Impasto makes its image
   Blender's explicit image-paint canvas, enters Texture Paint mode, selects
   Blender 5.1's main Paint brush tool, and switches the invoking Solid
   viewport to Material Preview.
6. Use Blender's normal Texture Paint brushes. Strokes update the image sampled
   by the generated Impasto layer graph, so they appear through the material.
7. Add Fill or Group layers, bind available channels, and adjust layer order,
   opacity, blend mode, and visibility.
8. Use **Rebuild Stack** only to repair or explicitly regenerate the compiled
   graph; ordinary uniform edits should not require it.

Selecting another Paint layer switches the canvas but does not force a mode
change. The explicit button is the safe way to enter Texture Paint. If a layer's
stored UV map or image was deleted, activation stops and reports what is missing
instead of allowing Blender to paint into a different target.

### Kiln normal-bake interoperability

When Kiln bakes onto a material that already has an Impasto stack, the baked
image is inserted as a **Kiln Baked Normal** Paint layer at the bottom of the
stack. Existing layers and the active paint layer are preserved, and Impasto
remains the sole owner of the Principled BSDF Normal input.

For a file made with an older version, select the object and click **Import /
Repair Kiln Normal** in the Impasto panel. The button reuses the image in the
material's **Kiln Bake Target** node, so no rebake is required. Repeating the
repair updates the same baseline layer rather than creating duplicates.

### One layer, one canvas per channel

A Paint layer is one logical layer whose bindings each own a dedicated
image (`binding.image_name`), created with the correct colorspace: color
channels sRGB, scalars and tangent normals Non-Color, Height seeded at
opaque neutral mid-gray. Adding a channel to a Paint layer (the `+`
rows in the Channels list, or **Add Channel Paint Layer** for a fresh
single-channel layer) creates that channel's canvas at the layer's
existing resolution, so all canvases of one layer stay equal-sized.

Blender's native brush still edits exactly one image at a time: each
painted channel row shows a brush button that makes that channel's
canvas the native paint target, and **Start Painting** picks the
layer's first painted channel. One native stroke lands in one channel —
that is the deliberate single-channel editing path.

**Blender Brush → N Channels** is the native multi-channel path. Impasto
records region position, pressure, size, tilt, and timing, then invokes
Blender's own `paint.image_paint` stroke once for each enabled channel at
pen-up. The active Brush asset, its texture/falloff/spacing behavior, and the
workspace tool are not replaced. Impasto temporarily substitutes Base Color,
Metallic, Roughness, Normal, or signed Height values, then restores the user's
canvas, brush colors, blend mode, and image-paint mode even on failure.

Draw-style Blender brushes are the supported baseline. Stroke-driven
Soften/Smear brushes use the same replay mechanism, but Clone, Fill, gradients,
and other tools with extra source/state requirements still need interactive
qualification. A replay appears after pen-up rather than while the pointer is
moving, because Blender's Python paint API accepts a completed stroke and one
canvas per invocation.

Files saved by earlier Impasto versions stored a single canvas on the
layer; they keep working unchanged, and opening them migrates the
stored state to per-channel form automatically (schema 1 to 2, no
images are created or altered).

### GPU multi-channel painting (experimental)

**GPU Paint All Channels** (layer panel, Object menu, or F3) rasterizes
each brush dab once into every bound channel simultaneously — Base
Color, Metallic, Roughness, Tangent Normal, and Height — using the
layer's Multi-Channel Brush values. Material channels alpha-blend;
Height is a separate additive pass driven by the same stroke, so
**Raise/Lower** accumulate relief around the neutral mid-gray canvas
exactly like the native Height brush. Left mouse paints, right mouse or
Esc stops, and the channel canvases sync back on every pen lift.
The viewport keeps the composed PBR material visible throughout the session;
the former raw single-channel overlay has been removed. A screen-space reticle
uses the same Radius value as the GPU dabs, and completed image writes are
tagged for material redraw immediately at pen-up.
GPU projection rejects hidden fragments using a front-surface prepass stored
as linear view-space depth. Native multi-channel replay temporarily enables
Blender's Occlude and Backface Culling options, then restores their prior
values.
The panel lists the exact target channels and includes their count in the
button label. Multi-channel painting operates on bindings of the **selected
Paint layer**; use the `+` channel rows on that same layer to add simultaneous
targets. Separate Paint layers are intentionally separate strokes.

The Metallic and Roughness controls in **Multi-Channel Brush** are stroke
values: they are written as grayscale into those channel images. The
**Influence** control beside each channel image is separate; it controls how
strongly that image layer is composited and therefore appears as a Mix factor
inside Impasto's generated node group. New images start transparent (visually
blank) so an untouched Paint layer has no material effect.

Notes and current limits:

- Base Color brush values are sRGB-encoded on deposit so the painted
  swatch renders as picked; blending still happens in stored space, not
  scene-linear composited space.
- GPU strokes are not undoable yet (native painting keeps Blender's
  normal paint undo). Stop the session before undoing stack operations.
- One native multi-channel replay invokes Blender image paint once per canvas.
  Blender 5.1 exposes no Python undo-group API, so those channel operations
  currently occupy separate native paint-undo entries rather than one Ctrl-Z.
- Occlusion is depth-prepass based; a stroke uses the view it was
  painted in. Orbiting between strokes is fine.
- If the GPU session fails on a backend/driver, Impasto reports once
  and native painting is unaffected.

**Resolution tradeoff:** new canvases default to 2048 x 2048, chosen so
a four-channel GPU pen-lift sync (GPU readback + `Image.pixels` write
per channel) stays within an interactive ~200 ms budget on measured
hardware. The layer-creation operator offers 1K/2K/4K per layer; at 4K
a four-channel pen lift measured ~417 ms, dominated by Blender's
full-image `Image.pixels` write, which no add-on can shrink. Pick 4K
only for hero assets where pen-lift latency is acceptable.

### Normal and height painting

**Tangent Normal (RGB)** bindings treat the paint image as an absolute
tangent-space normal map. Images
are stored as **Non-Color**, conventional encoded RGB `(0.5, 0.5, 1.0)` is a
flat normal, and the compiled shader decodes the blended image through Blender's
Normal Map node. Create a dedicated Tangent Normal channel Paint layer, activate
it, and paint/import encoded tangent-normal colors.
Blender's ordinary color brush does not generate sculpt-like normals from brush
pressure; it deposits the encoded RGB direction you choose. Repeating the same
stroke therefore does not accumulate additional relief. Use a Height Detail
layer for brush-built relief, and reserve Tangent Normal for painting/importing
encoded normal directions.

**Height Detail** is a grayscale derivative field centered on neutral mid-gray.
The **Raise** and **Lower** buttons configure Blender's native brush to ADD or
SUBTRACT white, so repeated strokes accumulate above or below 0.5. Constant
black, gray, or white regions are all geometrically flat; visible bump comes
from spatial gradients and stroke falloff, not the absolute shade. The result
feeds Blender's Bump node. When Normal and Height are both present, the decoded
tangent normal feeds the Bump node's Normal input, and the combined result drives
Principled. Multiple Normal layers currently use an approximate MIX of encoded
normal colors before decoding. This is useful for masks and simple overlays but
is not mathematically exact RNM normal blending; keep full-strength detail maps
on separate layers conservative until RNM/UDN blending is implemented.
Native brush undo is Blender's normal paint undo and stack operators use normal
operator undo.

### GUI acceptance checklist

Headless tests verify target setup and graph wiring, but cannot synthesize a
real viewport brush stroke. Before packaging a release, verify interactively:

- create a stack on a UV-unwrapped mesh and add two Paint layers;
- click **Start Painting**, paint a visible stroke in Material Preview,
  and confirm it appears in the Impasto material;
- select the other layer, confirm its image becomes the canvas, and paint a
  visually distinct stroke without changing the first image;
- undo and redo each native stroke, then undo a stack operation, confirming the
  two Blender undo paths interleave normally;
- save, reopen, select the paint layer, and confirm activation restores its
  saved image and UV target;
- delete or rename the stored UV map and confirm activation reports the missing
  UV rather than painting elsewhere;
- add Roughness and Height to a Base Color Paint layer, start **GPU Paint
  All Channels**, and confirm one stroke changes color, roughness, and
  relief together in Material Preview after pen lift;
- use **Blender Brush → 3 Channels** with a textured/falloff Brush asset,
  confirm all three images receive the same footprint with their respective
  PBR values, and confirm the original canvas, brush color, and blend return;
- confirm Raise strokes accumulate upward relief and Lower strokes recess
  it, and that repeated strokes deepen the effect;
- paint the front of a sphere with the GPU brush and confirm the back
  stays clean (occlusion), confirm the radius reticle follows the pointer and
  the composed material remains visible, then stop with RMB/Esc and confirm the
  Image editor shows each channel's synced canvas;
- confirm native per-channel brush buttons still edit exactly one
  canvas each after a GPU session ends.

Impasto owns its generated root and per-layer node groups. Treat those graphs
as build artifacts: edit the stack through Impasto rather than manually
rewiring generated nodes.

## Phase 1 acceptance gates

Phase 1 is complete only when all of these pass:

- pure golden and invariant tests;
- real-Blender zero-delta second reconciliation and tamper repair;
- save/reload and append persistence with stable UIDs, ordering, and bindings;
- undo across stack operators and cache rebuild;
- register, unregister, and re-register lifecycle;
- every operator exposed in the sidebar, a menu, and F3 search with an
  `Impasto:` label prefix;
- slider drags produce no node-tree mutations, verified by the delta log;
- the manual GUI responsiveness and undo-interleaving checklist in the design
  document.

## Tests

Pure compiler tests live in `tests/test_model.py`. A complete Phase 1 package
must also provide a `tests/run_tests.sh` Blender wrapper and headless lifecycle,
reconciliation, persistence, undo, and registration tests. The wrapper must
check explicit success sentinels because Blender can exit with status 0 after a
Python exception.

## Packaging checklist

Before distributing a Phase 1 archive:

- ensure `addons/impasto/__init__.py` exists and contains `bl_info`, module
  registration, and clean unregister logic;
- ensure the zip root is `impasto/`, not the repository root or the contents of
  `impasto/` without their parent folder;
- include runtime Python modules and this README;
- exclude `tests/`, golden fixtures, `__pycache__/`, `.pyc` files, and local
  logs;
- confirm no `Flapjack`, `flapjack`, `PBRStack`, or `pbrstack` identifier
  remains in runtime code, fixtures, docs, archive paths, or generated names;
- install the built archive into a clean Blender profile and run the
  register/re-register and smoke checks.

## License

GPL-2.0-or-later, consistent with Blender add-on requirements and the SPDX
headers in the source files.
