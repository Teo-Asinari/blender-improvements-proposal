# Impasto GPU-Resident Painting Redesign

Status: implementation work commissioned after interactive testing of Impasto
0.3.3 on Blender 5.1.2, OpenGL, NVIDIA Quadro RTX 5000 Max-Q.

## User-visible failures in the current prototype

1. `Blender Brush -> N Channels` records mouse samples and runs one complete
   `bpy.ops.paint.image_paint` replay per channel at pen-up. The stroke is not
   visible during the drag, pen-up is latent, and Blender creates separate
   native paint-undo entries for the channel operations.
2. The experimental GPU brush paints quickly into private GPU textures but the
   composed Blender material reads CPU-backed `Image` datablocks. Consequently
   the painted result remains hidden until pen-up readback and full-image
   `Image.pixels.foreach_set` synchronization completes.
3. Removing the former raw Base Color overlay fixed misleading single-channel
   shading but exposed the absence of any live composed PBR preview.
4. GPU channel values were originally frozen at session start. Impasto 0.3.2
   refreshes color, roughness, metallic, normal, height, radius, and hardness
   at each pen-down.
5. Native replay originally changed `Brush.color`, while Blender 5.1 defaulted
   to `ImagePaint.unified_paint_settings.color`. Impasto 0.3.2 now sets and
   restores both.
6. Projection painted through the mesh. Impasto 0.3.3 forces native Occlude
   and Backface Culling during replay and changed the GPU prepass from a broad,
   nonlinear NDC-depth epsilon to linear view-space depth.
7. The strict linear-depth comparison prevents rear-surface painting but
   produces grainy lines/holes on visible surfaces. The UV-rasterized fragment
   and the screen-prepass sample represent slightly different points on sloped
   geometry; a fixed near-zero tolerance is therefore insufficient.

## Measured latency

Recent three-channel, 2048-square interactive logs show:

- GPU dab submission averages approximately 0.017-0.10 ms per dab.
- Depth prepass costs approximately 13-18 ms when refreshed.
- Pen-up synchronization costs approximately 50-113 ms per stroke.
- Full Blender image writes alone cost approximately 35-38 ms for three 2K
  channels, regardless of the dirty readback area.
- GPU readback varies with dirty area, approximately 9-60 ms in recent tests.

The rasterizer is not the primary bottleneck. Per-stroke GPU-to-CPU readback
and complete Blender `Image` rewrites dominate. Blender's Python API exposes no
partial `Image.pixels` write, so smaller GPU readback rectangles cannot remove
the full-image CPU write cost.

At four channels and 4K, earlier measurements placed pen-up synchronization at
roughly 212-298 ms for typical strokes. Native replay is additionally bounded
by N sequential full Blender paint operations.

## Required architecture for approximately 100x perceived improvement

The interactive path must stop synchronizing Blender images at every pen-up.

1. Keep every active channel texture GPU-resident for the complete paint
   session.
2. Render live composed multi-channel surface feedback directly from those GPU
   textures. Do not substitute a raw Base Color or selected-channel overlay.
3. Treat pen-up as a GPU-only stroke boundary. It should finalize GPU undo
   metadata but perform no blocking readback or `Image.pixels` rewrite.
4. Synchronize Blender images at explicit flush, idle budget, paint-mode exit,
   save/export boundary, add-on unload, or controlled memory pressure.
5. Track dirty tiles and retain GPU tile snapshots/deltas per stroke so one
   Impasto undo record restores every affected channel atomically.
6. Keep the native replay path as a compatibility fallback. It cannot reach a
   two-order-of-magnitude improvement while invoking Blender's painter once per
   channel.
7. For production performance with Blender brush assets, translate the exposed
   brush alpha/texture, falloff, spacing, size, strength, and pressure behavior
   into GPU stamps. Specialized tools such as Clone, Smear, Soften, Fill, and
   Gradient require separate implementations or explicit unsupported status.

The realistic live-feedback floor is one viewport frame, normally 8-16 ms,
not literal sub-millisecond visual latency. Removing the current hidden stroke
and 50-113 ms blocking pen-up synchronization can nevertheless exceed a 100x
improvement in perceived response for longer strokes.

## Visibility correction direction

The old GPU test compared nonlinear NDC depth with a constant epsilon of
`0.002`. With a small near clip plane, front and rear surfaces of a meter-scale
object can differ by less than that value, admitting the rear surface.

Impasto 0.3.3 stores positive linear view-space depth. Its current tolerance is
an absolute `1e-4` plus `front_depth * 1e-5`; interactive testing shows this is
too strict on sloped/curved visible surfaces.

The replacement should use a screen-footprint-aware policy, for example:

- exact texel fetches from the linear-depth prepass rather than accidentally
  filtered samples;
- a local depth-gradient estimate from valid neighboring prepass texels;
- tolerance derived from that gradient plus a small absolute/relative floor;
- explicit handling of silhouette neighbors and clear-depth sentinels so the
  tolerance does not bridge unrelated front/rear surfaces;
- tests for flat, steep, curved, thin, self-occluding, and silhouette cases.

A triangle/visibility identifier buffer is a stronger alternative if gradient
tolerance cannot robustly distinguish same-surface mismatch from a genuinely
hidden surface.

## Undo constraints

Blender 5.1 exposes no `WindowManager.undo_group_begin/end` Python API.
`UNDO_GROUPED` on the Impasto modal operator does not combine the nested image
paint operations across different canvases. Therefore native replay currently
requires multiple Ctrl-Z operations and cannot provide atomic multichannel
undo without maintaining an Impasto-owned snapshot/delta history.

GPU tile snapshots are the preferred solution because the channel textures
already live on the GPU and only touched tiles need preservation. The history
must have explicit byte accounting, a memory budget, deterministic eviction,
and a flush boundary before Blender saves or exports the images.

## Parallel implementation partition

- GPU-resident session and composed preview: owns integration changes to
  `gpu_engine.py` and `ops.py`, deferred synchronization, and lifecycle safety.
- Visibility: builds a separate reusable policy/shader module and tests for
  crack-free front-surface selection; integration follows after validation.
- Brush adapter and undo: builds separate brush-conversion and GPU tile-history
  modules with tests and clean APIs, avoiding concurrent edits to the main GPU
  engine.

All interactive claims still require a foreground Blender viewport smoke test;
background Blender cannot create or validate the real painting framebuffers.
