# High-resolution painting: performance and memory estimates

Impasto's active GPU canvases use `RGBA16F`, or 8 bytes per texel. A resident
session owns one texture per active channel plus one full-size scratch texture
used by Soften and Smear.

The minimum active-canvas allocation is therefore:

`width × height × 8 × (active channels + 1)`

| Resolution | Per texture | 1 channel + scratch | 4 channels + scratch | 8 channels + scratch |
|---|---:|---:|---:|---:|
| 4096² | 128 MiB | 256 MiB | 640 MiB | 1.125 GiB |
| 8192² | 512 MiB | 1 GiB | 2.5 GiB | 4.5 GiB |

These are lower bounds, not whole-application figures. Additional VRAM is
used by:

- GPU tile undo, capped at 256 MiB;
- lower-stack/baseline and preview textures;
- viewport color and depth attachments;
- stencil, base-normal, and environment textures;
- Blender's own image and material texture allocations.

Practical planning estimates are roughly 1–2 GiB of Impasto-related VRAM for
four active 4K channels and 3–5 GiB for four active 8K channels. The rest of
Blender and the operating system still require headroom. An 8 GB GPU is a
reasonable floor for serious 4K multi-channel work; 8K multi-channel work
should be treated as experimental even with 16 GB or more.

Blender Images also occupy system memory in float RGBA form: approximately
256 MiB per 4K channel and 1 GiB per 8K channel, before temporary upload or
readback arrays.

## Expected responsiveness

Ordinary Paint and Erase use one MRT raster pass and are the best high-
resolution paths. Four-channel 4K painting is expected to be viable on a
modern discrete GPU, though mesh density, brush footprint, stencil sampling,
and dab spacing remain important. Eight active 4K channels need measurement.

Soften and Smear are substantially more expensive. Their correctness-first
implementation copies a complete texture for every enabled channel and every
dab before applying the effect. At 4K this already creates heavy memory-
bandwidth traffic; at 8K it is not expected to be interactive. Dirty-region
copying is required before either tool should be advertised for 8K work.

Explicit synchronization previously measured about 417 ms for four 4K
channels. A simple pixel-count projection puts four 8K channels near 1.7
seconds under comparable conditions. This estimates flush/readback latency,
not ordinary resident pen-up latency.

## Current policy

- 1K, 2K, and 4K creation are exposed in the UI.
- 8K is not exposed and is unsupported as an interactive target.
- Large full-surface strokes can exceed the 256 MiB atomic undo budget. Such a
  record is rejected rather than partially retained.
- Before exposing 8K, benchmark Paint, Erase, Soften, Smear, preview orbiting,
  explicit flush, save, undo, and session teardown across 1/4/8 channels.

All figures are architectural estimates unless explicitly described as a
measurement. Actual performance depends strongly on GPU bandwidth, driver,
mesh density, UV layout, brush size, and enabled channels.
