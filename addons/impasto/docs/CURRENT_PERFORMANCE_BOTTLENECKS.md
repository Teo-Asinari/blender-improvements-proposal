# Current GPU-paint performance bottlenecks

Production snapshot for Impasto 0.15.24, measured in Blender 5.1 on a
173,063-triangle mesh with five resident 4K `RGBA16F` channels. This note
records current evidence and priorities; the roadmap remains authoritative.

## Executive summary

Live painting and navigation have improved substantially. Sparse Undo
bookkeeping is no longer the dominant cost. Session entry is now the clearest
problem: the measured cold start took about 7.4 seconds, led by topology-to-UV
seam correspondence and GPU texture/batch initialization.

## Session startup

Measured CPU preparation:

- Total: 4,393 ms.
- Mesh soup: 93 ms.
- Seam correspondence: 4,252 ms.
- UV bounds: 46 ms.

Measured first-draw GPU initialization:

- Total: 3,049 ms.
- Paint texture creation/seeding: 2,028 ms.
- GPU batches and seam records: 895 ms.
- Stack baselines: 78 ms.

### What the seam correspondence is

Painting occurs in 2D UV space, while continuity is defined by the 3D mesh.
The seam correspondence pairs the two UV representations of a manifold mesh
edge when those representations are separated in the texture atlas. Impasto
uses these pairs to transport coverage across distant UV islands and prevent
white cracks at painted seams.

The current pure-Python builder scans every triangle edge and creates:

- a dictionary from canonical mesh edge `(min_vertex, max_vertex)` to its UV
  half-edges;
- a quantized-UV-segment dictionary used for overlap diagnostics;
- immutable `UVHalfEdge` and `UVSeamPair` records;
- diagnostic tuples for boundary, non-manifold, degenerate, continuous, and
  overlapping edges.

This is linear in triangle-edge count in broad complexity terms, but Python
object construction, hashing, sorting, and tuple conversion are expensive at
this mesh size. The 4.25-second measurement covers this correspondence stage;
the later 895 ms batch phase also includes construction of conservative seam
transport records.

Next investigation: cache correspondence by mesh topology and UV-layout
identity, invalidating it only when either changes. A safe cache needs a cheap,
reliable revision key and must not reuse stale triangle indices after edits.

## Live painting

Recent production strokes reached roughly 200–550 dabs/s in favorable cases.
GPU submission remained small. Representative longer strokes:

- 3,064 dabs, 579 flushes: 2,030 ms dirty tracking, 123 ms Undo touching,
  51 ms seam selection, and 42 ms GPU submission.
- 1,815 dabs, 198 flushes: 722 ms dirty tracking, 94 ms Undo touching,
  18 ms seam selection, and 19 ms GPU submission.

The next recurring paint-path target is therefore dirty-region calculation,
not Undo tile enumeration. `dirty_ms` includes conservative screen/UV coverage
work and should be split further before another invasive optimization.

Impasto 0.15.24 unions sparse tile geometry once and reuses it across channels.
Its synthetic five-channel fragmented-UV benchmark improved Undo bookkeeping
from 868.4 ms to 69.5 ms (12.5x). Production `undo_touch_ms` also includes GPU
snapshot capture, so its end-to-end improvement is necessarily smaller.

## Navigation

The latest session reported an average resident-view cost of 0.716 ms across
8,315 frames. Lit PBR navigation is no longer a leading bottleneck. Rare
multi-second maximum values likely include unrelated stalls or session events
and should not be interpreted as the normal frame cost.

## Synchronization and shutdown

- Explicit five-channel 4K GPU-to-Image synchronization: about 907 ms.
- Undo-history disposal at shutdown: about 185 ms.
- Total measured operator shutdown: about 196 ms, excluding the separately
  reported image synchronization.

`finalize_delay_ms=28819` in the supplied trace is elapsed idle time between
the last stroke activity and eventual finalization, not 28.8 seconds of
processing.

Dirty-region-only synchronization remains potentially valuable but requires
careful qualification of Blender Image state, saving, Undo, color management,
and partial pixel updates.

## Priority order

1. Cache or accelerate topology-to-UV seam correspondence.
2. Split and reduce recurring `dirty_ms` work.
3. Reduce cold paint-texture seeding and batch construction where lifecycle
   measurements show safe reuse.
4. Replace linear gutter/seam rectangle membership checks.
5. Investigate partial explicit GPU-to-Image synchronization.

The Blender extension-repository warning (`blender_org not found, sync
required`) is unrelated to Impasto GPU-paint performance.
