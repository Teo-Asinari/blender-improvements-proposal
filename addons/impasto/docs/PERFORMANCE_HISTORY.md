# GPU painting performance history

This log records measured Impasto performance changes separately from the
feature changelog. Numbers below come from the same production Blender file on
a Quadro RTX 5000 Max-Q, painting four targets in a five-channel 4096² session.
Stroke length and cursor travel differ between samples, so per-flush component
costs are the most useful comparison. `dabs_per_s` is dabs divided by physical
pen-down time; it is not an isolated engine benchmark.

## Measurement vocabulary

- **Dab:** one overlapping brush stamp along the sampled pen path.
- **Flush:** one live batch of queued dabs, normally drained near viewport
  redraw cadence. It is not an Image save or GPU-to-CPU synchronization.
- **Deferred stroke line:** resident pen-up statistics; no Blender Image
  synchronization has occurred.
- **Synchronized stroke line:** the later explicit/session-exit transfer into
  Blender Images.
- Component timers overlap with `flush_wall_ms`; they must not be added to
  `stroke_s`, which includes the user's pen-down time.

## 0.15.15 baseline

A representative 4K Paint stroke produced 913 dabs over 13.62 seconds:

| Measurement | Value |
|---|---:|
| Dab rate | 78.9 dabs/s |
| Dirty-region CPU work | 6,431.9 ms |
| GPU command submission | 26.5 ms |

Profiling showed that ordinary Paint and Erase computed detailed per-dab UV
work rectangles intended only for neighborhood-sampling Soften and Smear.
For the production mesh this was roughly 913 dabs multiplied by about 175,000
triangle bounds, or approximately 160 million unnecessary tests.

## 0.15.16: remove unused Paint/Erase work

Paint and Erase now retain one conservative union bound but skip the detailed
per-dab rectangle calculation. Soften and Smear are unchanged. New telemetry
also split flushing, UV bounds, seam selection, undo, input time, and pen-up
finalization.

A long 4K validation stroke produced 5,605 dabs and 1,009 flushes:

| Component | Total | Approx. per flush |
|---|---:|---:|
| Full flush work | 28,312.5 ms | 28.06 ms |
| Conservative seam selection | 20,710.2 ms | 20.53 ms |
| Undo tile capture | 5,553.5 ms | 5.50 ms |
| UV union bound | 952.5 ms | 0.94 ms |
| Detailed work rectangles | 2.3 ms | 0.002 ms |
| GPU command submission | 94.0 ms | — |

The removed calculation collapsed to effectively zero. This exposed repeated
Python scanning of roughly 19,000 conservative seam records as the dominant
live-stroke bottleneck.

## 0.15.17: vectorized seams and undo preflight

Conservative seam records now cache their owner-triangle indices. Each flush
uses one NumPy gather and intersection mask, preserving inclusive selection and
record order. Multichannel undo rectangles are preflighted atomically; work
known to exceed the 256 MiB history budget is not copied merely to be discarded.

The first production validation after this change used 1,875 dabs and 885
flushes over 19.64 seconds:

| Component | Before per flush | 0.15.17 per flush | Change |
|---|---:|---:|---:|
| Conservative seam selection | 20.53 ms | 0.71 ms | **29× faster** |
| Full flush work | 28.06 ms | 7.69 ms | **3.65× faster** |
| Undo tile capture | 5.50 ms | 4.18 ms | 1.3× faster |
| UV union bound | 0.94 ms | 1.02 ms | approximately unchanged |

Pen-up finalization fell from 1,780.6 ms in the earlier profiled stroke to
6.9 ms in the resident validation line. Explicit synchronization remained a
separate cost: 929.8 ms for the near-full 4041×4081 dirty rectangle across the
target images, comparable to earlier measurements.

The user reported that painting felt noticeably smoother, consistent with the
3.65× reduction in live per-flush work. This is not a controlled end-to-end
speedup claim because the strokes had different lengths and paths.

## Remaining measured bottlenecks

Undo capture was the largest named live cost in the 0.15.17 trace. Version
0.15.18 derives Paint/Erase Undo tiles from individually screen-hit triangle UV
regions rather than the broad rectangle spanning distant islands. Overlapping
requests are deduplicated before atomic budget accounting, and gutter work
retains separate destination regions. The gain still requires measurement on
the production 4K scene; dense strokes naturally benefit less than fragmented
atlases with large empty gaps.

### 0.15.18 regression on a highly fragmented Smart UV atlas

The first post-release comparison established that UV topology matters more
than raw texture resolution. The earlier production object was hand-unwrapped
into relatively few, large islands. A second 4K object used Blender Smart UV
Project and contained very many tiny, nearly adjacent islands. On that object,
a 19.34-second Paint stroke produced 1,194 dabs and only 197 live flushes:

| Component | Hand-unwrapped object | Smart-UV object |
|---|---:|---:|
| Full flush work | 6,808.3 ms | 17,921.6 ms |
| Approx. flush cost | 7.69 ms | 90.97 ms |
| Undo tile work | 3,699.3 ms | 15,093.8 ms |
| UV bounds/sparse-rect work | 906.1 ms | 1,485.0 ms |
| GPU command submission | 57.5 ms | 40.5 ms |

The roughly 91 ms flush cost reduced live feedback to about 10 Hz and was
visibly choppy. GPU drawing was not the bottleneck. The fragmented atlas made
each screen hit produce many small UV rectangle requests. After their atomic
Undo estimate exceeded 256 MiB, the transaction correctly became non-undoable,
but every later flush still rebuilt, tiled, and deduplicated those requests.
That work could no longer produce an Undo record and was therefore wasted.

Required correction: once a stroke transaction is abandoned, bypass sparse
rectangle generation and tile enumeration for the rest of that stroke. After
that early exit is validated, consider an adaptive selector that uses sparse
capture only while its request count or measured cost is lower than the broad
alternative. Do not infer that Smart UV Project is universally slower: the
observed driver is this result's many tiny islands, not the operator's name.

## 0.15.19: abandoned-Undo exit and camera-plane clipping

Version 0.15.19 implements the required early exit: later flushes do not build
sparse UV rectangles or enumerate tiles after an atomic record is abandoned.
Painting, seams, gutters, and final synchronization remain active. It also
clips camera-crossing triangles before perspective division. Fully hidden and
offscreen triangles receive empty screen bounds rather than entering every
zoomed-in dab; only non-finite projection data keeps the always-dirty fallback.

Passive viewport work is now measured separately. On session stop, one
`GPU_PAINT_SPIKE_HOVER` line reports average/maximum composed-preview,
depth-prepass, stencil, reticle, caliper, text-overlay, total view, and total
pixel-callback times, alongside triangle and unprojectable counts. New
production measurements are required before stating a speedup for either fix.

## 0.15.20: non-blocking navigation depth prepass

The first 0.15.19 hover trace reported 1,151 passive view callbacks on a
173,063-triangle mesh. Lit PBR preview submission averaged 0.2204 ms and pixel
overlays averaged 0.1621 ms, but 118 camera-change prepasses averaged 126.7249
ms. Inspection found an intentional one-pixel framebuffer read after every
depth draw. That read forced Blender's CPU to wait for the complete GPU raster,
reducing interactive navigation toward 8 frames per second.

Version 0.15.20 removes the read. Commands submitted afterward remain ordered
behind the depth pass on the GPU, preserving the dependency without blocking
the CPU. Consequently, `prepass_avg_ms` now measures CPU projection work and
GPU command submission, not true completed GPU duration. Interactive orbit and
zoom measurements are required to quantify the user-visible gain.

## 0.15.21: defer CPU triangle bounds during navigation

A post-0.15.20 production trace remained visibly choppy: 53 camera-change
prepasses on a 151,112-triangle mesh averaged 111.6518 ms even without the
blocking framebuffer read. Lit PBR submission remained only 0.2507 ms. The
remaining time was CPU projection and homogeneous clipping of every triangle,
performed on every orbit/zoom frame solely to prepare future dab dirty bounds.

Version 0.15.21 invalidates those bounds when the camera moves but rebuilds
them only on the first actual paint flush after navigation. The GPU depth pass
still follows the live view. Stroke telemetry exposes the deferred one-time
cost as `projection_bounds_ms`; passive navigation should no longer pay it.

## 0.15.22: lifecycle profiling and probe reuse

The remaining perceived delays occur at session entry and exit rather than
ordinary navigation. Version 0.15.22 caches capability probes for a versioned
backend/vendor/renderer identity within the Blender process. A cached session
restores the selected readback strategy without rerunning framebuffer tests.

`GPU_PAINT_SPIKE_START_PHASES` reports CPU mesh soup, base UV, seam mapping,
and UV-bbox preparation. `GPU_PAINT_STARTUP` reports first-draw capability,
shader/UBO, IBL, gutter, paint-texture, batch, stack-baseline, and remaining
GPU setup. `GPU_PAINT_SPIKE_STOP` reports handler removal, hover logging,
history disposal, GPU reference release, modal timer removal, redraw, and total
operator teardown. Required readback/Image writes remain in the existing
`syncback_total_ms` measurement and are not presented as teardown overhead.

Object-, image-, UV-, and stack-dependent GPU resources are deliberately not
cached across sessions until production timings justify a more complex and
invalidation-safe resident resource pool.

Other remaining costs are conservative UV-union calculation, unclassified
flush overhead, and roughly one second for explicit near-full 4K Image
synchronization. GPU command submission itself is small in these traces.

Do not reduce the current two-pixel minimum dab spacing merely to improve a
counter: overlapping dabs determine opacity and pressure response, and the
approximately 30 Hz flush cadence supplies live feedback. GPU instancing may
eventually reduce draw-call volume without changing appearance, but current
CPU submission time does not make it the first priority.

See [High-resolution performance](HIGH_RESOLUTION_PERFORMANCE.md) for memory
estimates and qualification policy.
