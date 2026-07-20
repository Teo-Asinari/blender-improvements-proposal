# GPU-resident Lit PBR preview: image-based lighting

## Blender 5.1 API finding

Blender 5.1.2 exposes `Preferences.studio_lights` entries with metadata such as
name, type, path, solid lights, and ambient color. It does not expose the
Material Preview engine's active prefiltered cubemap, mip chain, BRDF lookup,
or a reusable GPU texture handle. The foreground/background RNA probe found
these public `StudioLight` properties:

`has_specular_highlight_pass`, `index`, `is_user_defined`, `light_ambient`,
`name`, `path`, `solid_lights`, and `type`.

Some entries have a source path, but loading that source would still require
Impasto to prefilter it, would not reliably match the active viewport studio
light, and could introduce file availability/licensing dependencies. Therefore
the default preview uses an Impasto-owned environment with no external asset.

## Runtime design

`ibl.py` creates a cached linear-HDR equirectangular atlas:

- 128 x 64 diffuse irradiance strip;
- five 128 x 64 specular strips spanning roughness 0 through 1;
- spherical-Gaussian studio panels over a cool environment gradient;
- roughness broadens highlights while approximately retaining lobe energy.

The GPU stores the 128 x 384 atlas as RGBA16F. The Lit shader uses:

- Fresnel-Schlick with a roughness-dependent grazing term;
- metallic energy partition, `kD = (1-F) * (1-metallic)`;
- diffuse irradiance sampled by the final normal;
- reflection-vector sampling interpolated between prefiltered roughness strips;
- the split-sum integrated GGX environment-BRDF approximation;
- an ACES-fitted display shoulder for linear HDR values;
- an energy-conserving hemispheric fallback if atlas upload fails.

Base Color is decoded from stored sRGB into linear light. A Blender 5.1.2
probe loading a known `#808080` sRGB PNG found `Image.pixels == 0.5019608`;
Impasto uploads that array through a raw `GPUTexture` buffer, so its explicit
decode is required (unlike `gpu.texture.from_image`, which performs managed
sampling). Metallic and
Roughness retain their neutral alpha behavior. Tangent Normal and Height alter
the normal before both diffuse and specular environment lookup. Raw Normal,
Neutral Normal Lighting, and Height Grayscale return before IBL sampling.

## Measured cost

Blender 5.1.2, OpenGL, NVIDIA Quadro RTX 5000 Max-Q:

- cached-atlas construction test: 17.4 ms;
- foreground GPU atlas creation/upload: 6.8-7.4 ms;
- CPU atlas: 786,432 bytes (float32);
- GPU atlas: approximately 393,216 bytes (RGBA16F);
- Lit fragments add one diffuse lookup and two interpolated specular lookups;
- diagnostic modes perform no environment lookup.
- foreground preview draw CPU submission: 0.078 ms average over 20 draws
  (0.158 ms final sample).

The existing foreground five-channel stroke smoke test still completed with
no latched GPU error. Its three-dab CPU submission average was 0.102 ms per
attachment pass. This is not a GPU fragment-time benchmark: Blender's Python
GPU API exposes no timestamp-query path here, and pen-up intentionally does not
force a synchronization merely to measure it.

## Limits

The owned atlas is a deterministic studio approximation, not the active
Material Preview HDRI, and its roughness strips use an analytic
spherical-Gaussian approximation rather than offline importance-sampled GGX
convolution. The split-sum BRDF and energy partition are physically motivated,
but the viewport overlay remains a responsive paint preview, not a render
reference. Blender's actual material remains authoritative after flush.
