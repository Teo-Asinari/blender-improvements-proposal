# PBR Layer Painting: Technical Design

Companion spec to section 2 of `../PROPOSAL.md`. Scoped to what a
Blender 4.x Python add-on can deliver; upstream C++ work is called
out explicitly.

## 1. Goals and non-goals

**v1 must do**

- Per-material layer stack with paint, fill, and group layers;
  blend modes, opacity, visibility, layer masks.
- PBR channel set from a Principled BSDF template: Base Color,
  Roughness, Metallic, Normal, Height, Emission, AO.
- One brush stroke writes multiple channel images per gesture (see
  section 5 for limits).
- Automatic shader tree feeding a Principled BSDF, preserving user
  node edits outside the managed subtree.
- Correct color spaces: sRGB for Base Color / Emission, Non-Color
  for the rest.

**Deferred**

- Smart masks driven by baked curvature / AO / thickness.
- Procedural / anchor / generator layers.
- Material IDs and imported `.sbsar` support.
- GPU re-write of the paint pipeline (upstream, see section 7).
- UDIMs on layer masks.

## 2. Prior art

Four add-ons bracket the design space. One limitation each.

- **Ucupaint** -- <https://github.com/ucupumar/ucupaint>. Most
  complete free option; channel-based stack with masks and baking.
  Limitation: synthesised shader trees get slow to evaluate and
  edit with many channels and layers.
- **Layer Painter** -- <https://blendermarket.com/products/layerpainter>.
  Clean Substance-like UI and smart masks. Limitation: multi-channel
  paint is slot-switching, not one stroke writing N textures.
- **PBR Painter 3** -- <https://blendermarket.com/products/pbr-painter>.
  Strong material-library and fill workflow. Limitation: fill /
  material stamping rather than a non-destructive stack with
  per-layer masks.
- **HAS Paint Layers** -- <https://blendermarket.com/products/has-paint-layers>.
  Photoshop-style layers over a single image. Limitation: per-image,
  not per-material; multi-channel consistency is manual.

All four are bounded by Blender's single-target paint mode and
shader-graph compile cost -- the constraints shaping this design.

## 3. Architecture

Three layers, mirroring `voxel_sculpt`:

1. **UI panel** (`panel.py`, `ui_list.py`) -- Image Paint and Shading
   workspace panel. UIList for the stack, per-layer sub-panel for
   blend / opacity / mask, channel toggles, "Rebuild Shader". No
   paint logic here.
2. **Data model** (`props.py`) -- PropertyGroup tree on `Material`
   (section 4). Single source of truth.
3. **Shader-node synthesis** (`shader_build.py`) -- deterministic
   `build(material)` regenerates a managed subtree from the data
   model and wires it into the user's graph.

**Key decision:** the layer stack is a custom PropertyGroup, not
user-visible shader nodes. Users edit layers in the panel; the
shader tree is derived. Rationale:

- UI is not hostage to node-tree layout.
- Rebuilds are idempotent and diffable, so regeneration is safe.
- File size and eval cost track layer count, not GUI state.
- User shader work outside our labelled subtree is preserved.

## 4. Data model

Attached to `Material.pbr_layers`:

```
PBRChannel:
    name: str           # "base_color", "roughness", ...
    enabled: bool
    default_value: float | vec3
    colorspace: "sRGB" | "Non-Color"
    image: Image | None # lazily allocated on first paint / bake

Layer:
    name: str
    type: "paint" | "fill" | "group" | "adjustment"
    blend_mode: enum    # mix, multiply, overlay, screen, add, ...
    opacity: float
    visible: bool
    clipped_to_below: bool
    mask: LayerMask | None
    per_channel: dict[channel -> ChannelOutput]
    children: list[Layer]            # groups only

ChannelOutput:
    enabled: bool       # does this layer write this channel?
    image: Image | None # paint layers: per-channel texture
    value: float | vec3 # fill layers: constant

LayerMask:
    type: "paint" | "fill" | "smart"   # smart = phase 2
    image: Image | None
    invert: bool

PBRLayerStack (on Material):
    template: "principled_full" | "principled_lite" | "custom"
    channels: list[PBRChannel]
    layers: list[Layer]          # stack order; [0] = bottom
    active_layer_index: int
    active_channel: str
```

Per-channel images live in `bpy.data.images`, tagged with
`image["pbr_layers.role"] = (material_name, layer_uid, channel)` so
the rebuilder can reattach them after a file reload.

## 5. Multi-channel painting pipeline

Expectation: one stroke, N channel textures updated atomically
(base color *and* roughness *and* height).

**Open question, flagged:** Blender's Image Paint is built around a
single active image slot. Texture-paint slots let the user pick one
slot at a time; there is no public API for a stroke to splat into
multiple images per brush evaluation. We assume the add-on must
splat N times per stroke.

v1 approach:

1. A "multi-channel brush" PropertyGroup holds per-channel deltas
   (BaseColor=#8a6a4a, Roughness=+0.3, Height=+0.02, Normal=off).
2. On stroke start, capture the active layer; resolve the target
   image for each enabled channel with a non-zero delta.
3. Per mouse event, either (a) drive `bpy.ops.paint.image_paint`
   once per target with the slot swapped, accepting N-x CPU cost,
   or (b) in a modal handler, rasterise dabs ourselves via the
   `gpu` module offscreen. (b) is a mini paint engine; defer to
   v1.1.
4. Undo: one `ed.undo_push` per stroke, wrapping all N image edits
   so Ctrl-Z reverts as a unit.

Splat-N is effectively what Layer Painter does. Until upstream
exposes multi-target paint (section 8), this is the ceiling.

## 6. Shader-node synthesis

`shader_build.build(material)` is pure and idempotent.

1. Locate or create a node group `PBR_LAYERS__<material_uid>`
   feeding a Principled BSDF. Nodes outside the group are
   user-owned and untouched.
2. Clear internal nodes; regenerate bottom-up, one row per layer
   per enabled channel.
3. Per layer: `paint` -> Image Texture (Non-Color where required);
   `fill` -> RGB / Value; `group` -> recurse into a sub-group.
4. Blend each layer's channel output with the accumulator via a
   `Mix` node (Color or Float) parameterised by `blend_mode` and
   `opacity`. Masks multiply into the Mix factor.
5. Clipping: a clipped layer's mask factor is multiplied by the
   mask of the nearest non-clipped layer below.
6. Final channels drive Principled inputs. Normal via a Normal Map
   node; Height via a Bump node unless a Displacement link already
   exists (defer to the user).
7. Fixed-name group outputs. Outer wiring (group to Principled) is
   only recreated if missing, preserving user intermediaries like a
   ColorRamp on Base Color.

Non-Color correctness is enforced at image creation *and* every
rebuild, so a user flipping color space manually gets corrected
next build rather than silently producing wrong lighting.

## 7. Performance

The add-on cannot fix the documented brush-lag bugs (T58465, T74753,
T60965, #93796) or the viewport refresh issue (#86787, T101032).
Those live in `source/blender/editors/sculpt_paint/` and the
image-update / GPU-texture path -- an add-on sits strictly above.

What the add-on *can* do:

- Keep synthesised trees small by collapsing disabled channels.
- Debounce shader rebuilds via depsgraph tag plus idle timer.
- Allocate channel images lazily: an unpainted Metallic stays a
  constant node -- no 4K image, no GPU upload.
- Cap undo memory with per-stroke tile diffs of the painted region,
  sidestepping the runaway RAM of whole-image snapshots on 4K-16K
  textures.

Expect v1 latency to track native single-slot paint roughly linearly
in channel count because of splat-N.

## 8. Add-on vs C++ scope

Pure Python:

- Data model, UI, shader synthesis, template system.
- Multi-channel strokes via N single-slot paint ops.
- Per-stroke undo bundling.
- Mask painting, layer reorder, blend modes, clipping.
- Baking (curvature, AO) via existing `bpy.ops.object.bake`.

Needs upstream C++:

- True multi-target paint: one dab, N image writes, one GPU
  dispatch. Most impactful change; collapses splat-N into splat-once.
- GPU paint pipeline fix for the documented brush lag.
- Real-time viewport updates in Cycles / Material Preview (#86787).
- Memory-bounded tile-based image undo for large textures.
- First-class "Layer Stack" datablock so stacks survive library
  overrides and linking (the 2022 Layered Textures design proposal
  covered this; not shipped as of 2026).

## 9. Open questions

- Any Python-level path for one `paint.image_paint` to target
  multiple slots atomically, or is splat-N truly the only option?
- Can an add-on hook the existing stroke's dab stream, or must we
  re-implement stroke spacing ourselves?
- Is the `gpu` module fast enough for a modal offscreen paint pass
  to beat N-op splatting?
- How does the managed node-group survive `Make Local`, library
  overrides, and append/link without losing image links?
- What memory budget triggers paging channel images off GPU? Any
  API to query VRAM pressure?
- 8-bit vs 16-bit default for layer masks? 8-bit banding shows in
  height / roughness masks.

## 10. Recommended next steps

1. Prototype the PropertyGroup model and a read-only UIList so a
   hand-authored stack renders in the panel. No paint yet.
2. Write `shader_build.build()` and verify idempotent rebuilds on a
   3-layer, 4-channel test material.
3. Benchmark splat-N: latency for 1, 2, 4 channel strokes at 2K,
   4K, 8K to confirm linear-in-N and find where it breaks.
4. Implement template-driven channel setup tied to Principled BSDF,
   with correct color-space assignment at image creation.
5. File an upstream design note proposing a multi-target paint API,
   citing the four add-ons in section 2 as evidence of demand.
