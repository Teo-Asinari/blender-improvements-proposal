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

Undo capture is now the largest named live cost. A stroke can gradually spread
across a fragmented UV atlas: early rectangular tile captures occur before the
eventual atomic record is known to exceed its budget. The next exact approach
is sparse touched-tile capture derived from individually hit UV regions rather
than the broad rectangle spanning distant islands.

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
