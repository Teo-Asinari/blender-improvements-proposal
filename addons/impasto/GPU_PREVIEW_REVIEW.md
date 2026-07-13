# Impasto GPU Preview: Rendering Review and Acceptance Contract

Status: independent review of the temporary GPU-resident preview introduced
for low-latency multi-channel painting. This preview is interactive feedback;
the compiled Blender material remains authoritative after a flush.

## Why this cannot exactly reproduce Blender's Material Preview HDRI

Impasto's in-progress paint data lives in private `GPUTexture` objects so a
stroke can remain visible without GPU-to-CPU readback or a full
`Image.pixels` rewrite. Blender's Eevee/Material Preview pipeline, however,
reads material node graphs backed by Blender image datablocks. The Python GPU
API does not expose a supported way to bind an add-on's private textures as
temporary image-node inputs, invoke the viewport's material shader on them, or
sample the active studio-light/HDRI resources with Blender's complete color
management and reflection prefiltering.

Copying private textures into Blender images every stroke would recover the
real material preview, but it is precisely the 50-113 ms blocking path the
GPU-resident redesign removes. Capturing the viewport color buffer would only
capture the old material result; it would not shade the new private channel
values. Therefore `LIT_PBR` deliberately uses deterministic fixed lights and
a compact microfacet approximation. It must be described as an approximation,
not as the active Blender HDRI.

## Required modes

- `LIT_PBR`: composed Base Color, Metallic, Roughness, Tangent Normal, and
  Height under deterministic approximation lighting.
- `RAW_TANGENT_NORMAL`: the encoded tangent-normal RGB, with transparent
  pixels resolved toward neutral `(0.5, 0.5, 1.0)`.
- `NEUTRAL_NORMAL_LIGHTING`: neutral material and grazing diffuse lights to
  make normal and height direction legible without Base Color/specular noise.
- `HEIGHT_GRAYSCALE`: signed scalar inspection around neutral mid-gray;
  raising trends white and lowering trends black.

All modes must sample resident textures directly. Switching modes is a shader
uniform/state update only: no `GPUTexture.read`, framebuffer `read_color`,
`Image.pixels.foreach_set`, image `update`, flush, or undo boundary.

## Numerical findings

### Per-channel alpha

`straight_sample` recovers unpremultiplied RGB, but that does not make the
sample locally present. Each channel must resolve its own transparent pixels
toward a neutral fallback before the values are shaded:

```glsl
albedo   = mix(vec3(0.5), srgb_to_linear(base.rgb), base.a);
metallic = mix(0.0, metal_sample.r, metal_sample.a);
roughness = mix(0.5, rough_sample.r, rough_sample.a);
encoded_normal = mix(vec3(0.5, 0.5, 1.0), normal_sample.rgb,
                     normal_sample.a);
```

Without this, an allocated but untouched Roughness canvas resolves to zero
and appears nearly mirror-like wherever Base Color makes the overlay opaque.
A low-alpha normal stroke can likewise become a full-strength perturbation
when another channel supplies the overlay's maximum coverage.

### Tangent frame

The derivative frame must guard zero/degenerate UV derivatives before
normalization. A zero vector passed to `normalize` can produce NaNs and black
or sparkling fragments. Mirrored UVs need consistent handedness; compare the
preview on mirrored islands against Blender's Tangent Normal node after flush.
When a stable frame cannot be formed, fall back to the geometric normal.

### Height

The reviewed implementation uses four additional texture reads per fragment
for central differences and multiplies the result by a fixed `8.0`. This is
useful as a legibility approximation but is not physically equivalent to
Blender's Bump node: its apparent strength depends on UV scale, texture
resolution, filtering, and screen footprint.

For the immediate preview, mode-specific amplification is acceptable if it is
clearly diagnostic. A later optimization should use screen derivatives of the
already sampled height value and a cotangent surface-gradient formulation,
removing four texture taps and improving behavior across UV scale. This needs
foreground comparison before replacing the simpler central difference.

## Performance invariants

1. Pen-up remains GPU-only and must not synchronize Blender images.
2. Preview-mode changes do not flush, allocate canvases, or create undo steps.
3. Raw Normal and Height modes should return before GGX/Fresnel evaluation.
4. Neutral Normal Lighting should use simple diffuse grazing lights and avoid
   the three-light microfacet path.
5. Shader and batches are compiled/created once per session, not per frame or
   per mode switch.
6. The preview draw performs no CPU traversal proportional to texture pixels.
7. Missing channels use already-bound fallback textures plus `has_*` uniforms;
   mode switching must not rebuild framebuffer attachments.

## Persistent UI and resident-session behavior

The active paint layer stores `gpu_preview_mode` as Blender RNA state, so the
choice survives save/reopen and is independent for each layer. The stable enum
identifiers are `LIT_PBR`, `RAW_TANGENT_NORMAL`,
`NEUTRAL_NORMAL_LIGHTING`, and `HEIGHT_GRAYSCALE`. `ops.gpu_preview_mode()`
sanitizes legacy/unknown values to `LIT_PBR`.

The mode enters `start_session` settings and remains editable in the sidebar
while the modal GPU session is active. The modal timer detects a changed layer
value and calls `gpu_engine.set_preview_mode`; this changes only draw-time
state and tags the viewport for redraw. It does not flush, synchronize Images,
or end the session.

The same resident modal deliberately passes mouse events outside the 3D
`WINDOW` region through to Blender. Consequently Base Color, Metallic,
Roughness, Normal, Height, Radius, and Hardness can be edited in the N-panel
between strokes. Their PropertyGroup values are read immediately before the
next `begin_stroke`, preserving resident textures and undo history with no
readback. A stroke already in progress retains its pen-down settings.

Impasto's visible GPU Radius and Hardness controls are authoritative. A
supported Blender Draw asset still contributes spacing, strength, pressure,
falloff, and texture metadata, but its stored size does not silently override
the Radius displayed in Impasto.

## Foreground acceptance checklist

### Accuracy and legibility

- Paint Base Color only while Roughness/Metallic/Normal canvases exist but are
  untouched: the preview stays neutral, not mirror-black or strongly bumped.
- Paint a 25%-alpha normal over opaque Base Color: perturbation is visibly
  weaker than the same normal at 100% alpha.
- A flat normal `(0.5, 0.5, 1.0)` produces no shape change.
- Positive and negative tangent X/Y normals tilt in opposite directions under
  Neutral Normal Lighting.
- Mirrored UV islands show consistent tangent-space direction relative to the
  flushed Blender material.
- Degenerate or zero-area UV triangles show geometric shading, never NaNs,
  black sparkles, or disappearing fragments.
- Height `0.5` is neutral; equal Raise/Lower magnitudes are equally legible and
  have opposite polarity in Height Grayscale and Neutral Normal Lighting.
- Height strokes remain recognizable at 1K, 2K, and 4K and on differently
  scaled UV islands; document expected approximation differences.

### Mode behavior

- Switching among all four modes during an active session updates on the next
  viewport redraw without ending the paint session.
- Raw Tangent Normal is encoded RGB, not a world-space normal visualization.
- Height Grayscale is diagnostic scalar data, not displaced geometry.
- Lit PBR remains visually stable while Blender's studio light is changed;
  this confirms it is deterministic approximation lighting.
- Exiting/flushing restores the real Blender material/HDRI result.

### Performance and lifecycle

- Repeated mode switching produces no readback/flush log entries and no new
  undo records.
- Pen-up latency remains frame-scale and does not regress toward the former
  50-113 ms image synchronization cost.
- Orbiting, zooming, and painting remain responsive on the user's 600k-face,
  4K-texture stress case.
- Session exit, explicit flush, save boundary, and add-on disable retain all
  painted data regardless of the last preview mode.
