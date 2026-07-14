# Complete-Stack GPU-Resident Preview Architecture

Status: design and pure reference implementation for making the live preview
see the Impasto material state beneath and above the active resident Paint
layer. The companion implementation is `preview_stack.py`; focused coverage is
in `tests/test_preview_stack.py`.

## What “accurate” means

There are three distinct targets:

1. **Impasto channel-composition parity is achievable.** Fill constants, Paint
   images, Kiln's baked-normal baseline, layer/binding/group opacity, image
   masks, ordering, and Impasto blend modes can be evaluated from the stored
   stack model entirely on the GPU.
2. **Perceptually close PBR/IBL is achievable.** A custom shader can use the
   resulting Base Color, Metallic, Roughness, Tangent Normal, and Height with a
   defensible microfacet BRDF and either deterministic lights or the source
   studio-light HDRI.
3. **Pixel-identical Blender Material Preview is not exposed.** Blender does
   not publicly expose Eevee's compiled material shader, its prefiltered
   reflection/irradiance resources and BRDF LUTs, the evaluated scene World as
   a bindable add-on texture, or its complete OCIO display shader for reuse in
   a Python draw handler. “Accurate stack” must not be described as “exact
   Blender HDRI/material preview.”

The parity boundary is also the Impasto stack itself. On stack creation,
Impasto records displaced Principled links only so Remove Stack can restore
them; its generated channel chains start from registry defaults. Therefore the
resident compositor should match `compile_stack`, not attempt to evaluate
those displaced pre-Impasto links.

## Current compiler semantics

- Stored `layers[0]` is topmost; composition runs `reversed(layers)`.
- A bare Fill has no layer node group. Its VALUE/COLOR constants feed root Mix
  nodes directly.
- A Paint binding samples its own `binding.image_name`, with legacy fallback to
  `layer.image_name`. Scalar channels use red; Base Color is color-managed;
  tangent normal is Non-Color encoded RGB.
- Paint image alpha and visible image masks multiply the layer factor when
  `binding.use_masks` is enabled.
- Layer, binding, and ancestor-group opacity/visibility fold into that factor.
- Normal layers blend encoded tangent RGB and decode once at chain end. Height
  then enters one Bump node, optionally using that decoded normal.
- Kiln interoperability is not a special shader path: **Kiln Baked Normal** is
  an ordinary bottom Paint-normal layer. Preserving the lower stack therefore
  preserves the bake.

## Recommended resident dataflow

At GPU-session start, snapshot the stack and call:

```python
plan = preview_stack.plan_resident_preview(stack_model, active_layer_uid,
                                           channel_keys)
```

The plan partitions bottom-to-top participants into:

- static layers below active;
- the active resident Paint layer;
- static layers above active;
- nonlinear upper operations requiring live passes;
- non-active image/mask dependencies and UV maps.

### Common affine fast path

MIX, ADD, SUBTRACT, MULTIPLY, and SCREEN are affine in the incoming channel
value. Above-active layers using these modes collapse per texel to:

```text
final = C * active_result + D
```

where `active_result` is the active layer blended over the static lower
baseline. `preview_stack.affine_coefficients` and
`compose_affine_coefficients` are the shader-independent reference.

The GPU implementation should:

1. Build compact static lower-baseline channel textures once.
2. Build only the upper coefficient textures actually required; constant Fill
   coefficients remain uniforms.
3. Build an active mask texture once from non-paint masks, then multiply it by
   resident paint alpha and constant opacity in the resolve shader.
4. On each dirty tile, blend baseline + resident active and apply `C*x+D`.
5. Bind resolved channels to the existing PBR/diagnostic preview shader.

This is one small dirty-tile resolve pass in the common case and has no
GPU-to-CPU transfer.

### OVERLAY and other nonlinear operations

OVERLAY branches on the incoming value, so no single `C,D` pair can represent
it. Do not silently reorder it or treat the active layer as topmost. Apply each
reported `plan.nonlinear_upper` operation in its real bottom-to-top order using
a GPU ping-pong pass over the dirty tile. These passes remain GPU-resident and
are expected to be much cheaper than image readback/full `Image.pixels`
writes. If an implementation initially lacks these passes, label that stack
configuration approximate and offer explicit flush/material inspection.

### Different UV maps

The single-UV fast path is valid when `plan.single_uv_fast_path` is true. For
different layer/mask UV maps, raster the evaluated mesh into the active
layer's UV atlas while interpolating the source layer's named UV attribute.
This evaluates the source image at the same surface point and produces a
static baseline/coefficient atlas. Do not assume equal UV coordinates merely
because image sizes match. Cache one mesh batch per required UV mapping.

## Color and alpha domain

Blender's public `gpu.texture.from_image(image)` returns a GPU texture shared
with the Image datablock; its samples are already in **scene-linear color
space** and use premultiplied or straight alpha according to the Image alpha
mode. This is the preferred no-readback upload for static Paint, Kiln, and mask
dependencies.

Resolved Base Color atlases should use scene-linear values. Scalar and tangent
normal atlases remain Non-Color. Normalize alpha once at the input boundary;
do not blindly unpremultiply textures whose Image alpha mode is straight, and
do not apply `srgb_to_linear` to `gpu.texture.from_image` samples a second time.

The active private texture has a different, verified boundary. A Blender 5.1.2
probe loading an 8-bit sRGB `#808080` PNG returned approximately `0.50196` from
`Image.pixels`, not scene-linear `~0.216`. Impasto uploads that raw buffer,
sRGB-encodes new stroke payloads, and decodes active Base Color in the preview;
those three steps are internally consistent. A resolved compositor must decode
the active resident sample once, but must **not** decode a static baseline
sample obtained through `gpu.texture.from_image` a second time. Compose and
store the resolved Base Color atlas in scene-linear space.

## HDRI and color-management API assessment (Blender 5.1.2)

Foreground/background RNA probing confirms these public inputs:

- `View3DShading`: `selected_studio_light`, `studio_light`,
  `studiolight_intensity`, `studiolight_rotate_z`, background/blur controls,
  and scene-world/light toggles.
- `Preferences.studio_lights`: `StudioLight` entries exposing name, type,
  path, index, ambient data, and studio solid lights. Bundled WORLD entries
  expose files such as `.../studiolights/world/city.exr`.
- `ColorManagedViewSettings`: view transform, look, exposure, gamma, white
  balance, and related settings.
- `gpu.texture.from_image`: the only public image-to-GPU texture utility.

These make a closer custom IBL possible: resolve the selected WORLD light,
load/reuse its source image, bind the shared texture, apply viewport rotation
and intensity, and implement equirectangular diffuse/specular sampling.
However, the public surface does **not** expose Blender's internal convolved
environment maps, split-sum/BRDF lookup resources, Eevee material shader, or a
general Material Preview render result usable with Impasto's private channel
textures. Reading color-management property names is not equivalent to
reusing Blender's OCIO GPU transform. Exact parity would require tracking and
reimplementing those internals and would remain backend/version fragile.

Relevant public documentation:

- https://docs.blender.org/api/current/gpu.texture.html
- https://docs.blender.org/api/current/bpy.types.View3DShading.html
- https://docs.blender.org/api/current/bpy.types.StudioLight.html
- https://docs.blender.org/api/current/bpy.types.ColorManagedViewSettings.html

## VRAM and performance constraints

Do not allocate three unconditional RGBA16F copies of every 4K channel.
One 4K RGBA16F texture is 128 MiB; five such textures are 640 MiB.

- Use RGBA16F for Base Color and Normal; R16F for Metallic, Roughness, Height,
  masks, and scalar coefficients.
- Omit baseline textures for channels whose lower state is a constant default.
- Keep constant Fill values/factors as uniforms.
- Allocate upper C/D textures only for spatially varying Paint/mask layers.
- Reuse one scratch target for nonlinear passes and update dirty tiles only.
- Count baseline, resolved, coefficient, scratch, and resident-paint bytes in
  the same session VRAM budget; fail or degrade explicitly before allocation.
- Rebuild static caches only when stack structure, Fill values, masks,
  non-active images, UV assignment, or resolution changes—not per dab.

## Invalidation and lifecycle contract

- Resident dab/undo/redo: resolve affected active dirty tiles only.
- Orbit/zoom: no atlas rebuild; only redraw the mesh preview.
- Brush/PBR preview-mode change: uniforms only.
- Fill/mask/layer ordering or visibility change: rebuild affected static
  baseline/coefficients, or block stack editing while the modal session runs.
- External non-active Image edits have no reliable push callback; expose
  **Rebuild Resident Preview** or restart the session.
- Explicit flush/save/exit: synchronize the active resident canvases as
  already designed. Static baseline dependencies remain authoritative Blender
  images and require no writeback.
- Add-on disable/error: release every baseline/coefficient/resolved texture and
  image reference with the rest of the GPU session.

## Integration acceptance

1. Fill below active affects live Base Color/Metallic/Roughness immediately.
2. Kiln bottom normal remains visible while painting a higher normal layer.
3. Fill/Paint above active retains ordering; active paint never jumps to top.
4. Paint alpha, layer/binding/group opacity, masks, inversion, and visibility
   match the compiled material after flush within numerical/color tolerances.
5. All six blend modes match; OVERLAY either uses a nonlinear pass or is
   explicitly marked approximate.
6. Different named UV maps map the same surface points correctly.
7. Empty channels use registry defaults; displaced original Principled links
   are not incorrectly introduced.
8. Ordinary strokes, preview mode changes, orbit, undo, and redo cause zero
   readback and zero `Image.pixels` writes.
9. Static-cache build time and VRAM are logged separately from dab latency.
10. Lit preview is described as perceptual custom IBL, while explicit
    flush/material inspection remains the Blender-authoritative comparison.
