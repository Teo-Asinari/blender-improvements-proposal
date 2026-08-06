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

Soften and Smear remain more expensive than Paint and Erase, but now copy only
the conservative padded UV region affected by each dab. Large brushes, dense
or widely scattered UV layouts, and unprojectable geometry can still expand
that region substantially. The implementation falls back to a full texture
when projection bounds are unavailable.

Explicit synchronization has measured roughly 0.85–0.93 seconds for near-full
4K dirty regions in recent four-target production traces. A simple pixel-count
projection puts the equivalent four 8K transfer around 3.4–3.7 seconds under
comparable conditions. This estimates readback/Image synchronization latency,
not ordinary resident pen-up latency. See the
[performance history](PERFORMANCE_HISTORY.md) for the source measurements.

## Current policy

- A persistent stack-wide selector exposes 1K, 2K, 4K, and experimental 8K
  for newly created Paint layers. Channels added later inherit their layer's
  resolution, preserving uniform GPU-session dimensions.
- 8K remains unqualified as an interactive target despite being selectable.
- Large full-surface strokes can exceed the 256 MiB atomic undo budget. Such a
  record is rejected rather than partially retained.
- Before treating 8K as supported, benchmark Paint, Erase, Soften, Smear,
  preview orbiting, explicit flush, save, undo, and session teardown across
  1/4/8 channels.

The headless analytic benchmark matrix covers Paint, Erase, Soften, and Smear
at 1, 4, and 8 channels. Real timing still requires an interactive GPU context:
use a fixed mesh, viewport and brush, warm the session, record three identical
20-dab strokes, and compare median stroke telemetry.

All figures are architectural estimates unless explicitly described as a
measurement. Actual performance depends strongly on GPU bandwidth, driver,
mesh density, UV layout, brush size, and enabled channels.
