# Blender Improvements

Enhancements to Blender's sculpting, texture painting, and UX workflows — inspired by workflows found in specialized 3D tools such as 3DCoat and Substance Painter. Shipped as add-ons where Python can carry the feature; researched toward upstream contributions where it can't.

> This is an independent project. It is not affiliated with, endorsed by, or derived from 3DCoat (Pilgway), Substance Painter (Adobe), or any other product mentioned; product names are used only to describe comparable workflows. All code here is original, written against Blender's public APIs.

## Add-ons (working today)

All four are tested against **Blender 5.1.2**, ship with headless test suites that run against a real Blender binary, and install by copying the folder (minus `tests/`) into your `scripts/addons/` directory. See each add-on's README for details.

### [Seam Path Tool](addons/seam_path_tool/) — v1.4.0

Interactive shortest-path UV seam marking in Edit Mode: click points on the mesh, each click commits a seam along the shortest path from the last anchor, with a live preview of the candidate path under the cursor. Occlusion-aware vertex picking, erase mode, per-segment undo, on-screen help panel. Fast on large meshes: commits reuse the previewed path (no pathfinding on click), and the hover path tree solves at C speed via an optional scipy dependency (pure-Python fallback included).

### [UV Island Overlay](addons/uv_island_overlay/) — v1.4.0

Viewport overlay that colors each UV island distinctly and/or drapes a texel-density checkerboard through the actual UVs — the default combined mode shows both at once (hue = island, checker scale = density). Islands can be computed from true UV connectivity or *predicted from seams live as you mark them*, no unwrap needed. Per-island density stats, deviation tint, live opacity controls. Drawn as a GPU overlay; the mesh is never modified.

### [Kiln](addons/kiln/) — v1.0.1

A guided high-poly → low-poly normal-baking workflow in one sidebar panel: pair the sculpt with a retopo mesh (existing, or a generated QuadriFlow candidate), pass a bake-readiness checklist (UVs, scale, normals — with one-click fixes and shortcuts into the two add-ons above), then one button runs the whole bake gauntlet: Cycles switch, image/material/node targeting, selected-to-active with auto ray distances, save to `//textures/`, normal map wired into the material, everything restored afterward. Normals-only for now; other map types are structured TODOs.

### [Calipers](addons/calipers/) — v1.0.0

Scale-aware voxel-remesh preview and safety (the Proposal §5 prototype). A sidebar panel shows, for both the sculpt-mode Voxel Remesh and the Remesh modifier, what the current voxel size actually means for your mesh: cell counts along each axis, a green/yellow/red cost band, and warnings for unapplied or non-uniform scale. "Add Remesh Modifier (Safe)" adds the modifier *without* computing anything until you've seen the estimate and confirmed; a preflight dialog does the same for the destructive operation. A viewport guide draws a voxel-sized sample cell and sparse grid slices against the mesh so the size is judged visually, never by the bare number.

## Documents

- [PROPOSAL.md](PROPOSAL.md) — the full proposal: the features above, plus the two flagship efforts (layered PBR texture painting, voxel sculpting) that ultimately need work in Blender's core.
- [research/](research/) — technical research feeding the flagship designs.

## Approach

Features are piloted as Python add-ons with agent-assisted development: each add-on carries a headless test suite (`tests/run_tests.sh`) that exercises the real Blender binary in `--background`, including — where the domain allows — real end-to-end assertions (e.g. Kiln's suite performs an actual Cycles normal bake and checks the pixel statistics). API behavior is probed against the running binary rather than assumed; the traps found along the way are documented in the add-on READMEs.

## Status

Active development. The three add-ons above are usable daily; the flagship features are in research/design.

## License

The add-ons are GPL-2.0-or-later (as Blender add-ons must be; see SPDX headers). Documentation and research notes: all rights reserved for now.
