# Blender Improvements Proposal

A project proposing enhancements to Blender's sculpting, texture painting, and UX — inspired by workflows in 3DCoat, Substance Painter, and other specialized 3D tools.

## What This Is

This repository contains a proposal and working prototypes for bringing key missing features to Blender, either as add-ons or as upstream contributions. The focus areas are:

- **Voxel-Based Sculpting** — topology-free sculpting with real-time booleans, built on OpenVDB
- **PBR Texture Painting with Layers** — a proper layer stack, simultaneous multi-channel painting, GPU-accelerated performance
- **Interactive Brush Controls** — 3DCoat-style hold-key + right-drag to adjust brush size and intensity
- **UX & Navigation Improvements** — viewport navigation and workflow enhancements drawn from 3DCoat

## Repository Layout

```
PROPOSAL.md                 Full proposal with feature descriptions and references
addons/
  brush_gesture/            Working v1 add-on (feature #3). Needs Blender test.
  voxel_sculpt/             Skeleton add-on for feature #1. Stubs only.
docs/
  VOXEL_SCULPT_RESEARCH.md  Feasibility findings for voxel sculpting
  VOXEL_SCULPT_DESIGN.md    Technical design for the voxel sculpt add-on
  PBR_LAYER_PAINTING_DESIGN.md  Technical design for the PBR painting add-on
  UX_NAVIGATION_PAIN_POINTS.md  Catalog of sculpt/paint UX pain points
  DEVTALK_VOLUME_API_POST.md    Draft devtalk post requesting a Volume API
tools/
  voxel_sculpt_demo.py      Standalone numpy + skimage voxel sculpt reference
  volume_roundtrip_benchmark.py  Measures .vdb round-trip latency in Blender
```

## Status per Feature

| Feature | Status | Next step |
|---|---|---|
| 1. Voxel sculpting | Skeleton + research + design. Blocked on Blender Volume API. | Post devtalk thread requesting in-memory `FloatGrid -> Volume` construction. |
| 2. PBR layer painting | Design doc complete. No prototype. | Validate multi-channel paint assumption against existing add-ons. |
| 3. Brush gesture | v1 add-on written (871 lines). Not yet verified in Blender. | Install in Blender 4.x, resolve `# VERIFY:` comments, submit to Extensions. |
| 4. UX pain points | Catalog of 7 documented pain points. | Pick quick wins and build small add-ons. |

## Running the Demos

The `tools/` scripts are standalone and do not require Blender to be running (except the benchmark).

```
pip install numpy scikit-image
python tools/voxel_sculpt_demo.py --scene swiss --resolution 128 --output out.obj
python tools/voxel_sculpt_demo.py --benchmark
```

For the Volume round-trip benchmark, paste `tools/volume_roundtrip_benchmark.py` into Blender's Scripting workspace or run:

```
blender --background --python tools/volume_roundtrip_benchmark.py
```

## Installing the Add-ons

Both add-ons are in `addons/`. In Blender: `Edit -> Preferences -> Add-ons -> Install from disk`, then point at the zipped directory. The brush gesture add-on is v1 and functionally complete pending real-Blender verification. The voxel sculpt add-on is a skeleton and does not yet perform real sculpting.

## Contributing

Open an issue or PR. The largest unblockers right now are: (a) verifying `brush_gesture` in a real Blender install, and (b) pushing the Volume API request on devtalk.blender.org with measurement data from the benchmark script.

## License

TBD
