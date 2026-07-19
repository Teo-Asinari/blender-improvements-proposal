# Blender Improvements

Enhancements to Blender's sculpting, texture painting, and UX workflows — inspired by workflows found in specialized 3D tools such as 3DCoat and Substance Painter. Shipped as add-ons where Python can carry the feature; researched toward upstream contributions where it can't.

> This is an independent project. It is not affiliated with, endorsed by, or derived from 3DCoat (Pilgway), Substance Painter (Adobe), or any other product mentioned; product names are used only to describe comparable workflows. All code here is original, written against Blender's public APIs.

## Add-ons

All five are tested against **Blender 5.1.2**, ship with test suites that run
against a real Blender binary, and install by copying the folder (minus
`tests/`) into your `scripts/addons/` directory. See each add-on's README for
usage, limitations, and interactive acceptance checks.

### [Seam Path Tool](addons/seam_path_tool/) — v1.4.0

Interactive shortest-path UV seam marking in Edit Mode: click points on the mesh, each click commits a seam along the shortest path from the last anchor, with a live preview of the candidate path under the cursor. Occlusion-aware vertex picking, erase mode, per-segment undo, on-screen help panel. Fast on large meshes: commits reuse the previewed path (no pathfinding on click), and the hover path tree solves at C speed via an optional scipy dependency (pure-Python fallback included).

### [UV Island Overlay](addons/uv_island_overlay/) — v1.4.0

Viewport overlay that colors each UV island distinctly and/or drapes a texel-density checkerboard through the actual UVs — the default combined mode shows both at once (hue = island, checker scale = density). Islands can be computed from true UV connectivity or *predicted from seams live as you mark them*, no unwrap needed. Per-island density stats, deviation tint, live opacity controls. Drawn as a GPU overlay; the mesh is never modified.

### [Kiln](addons/kiln/) — v1.2.1

A guided high-poly → low-poly normal-baking workflow in one sidebar panel:
pair the sculpt with a retopo mesh (existing, or a generated QuadriFlow
candidate), pass a bake-readiness checklist, then run the complete selected-to-
active bake without manually assembling nodes and settings. Automatic ray
distance, manual inner/outer shells, and an explicit cage are presented as
distinct projection modes with viewport guides; the explicit cage is the most
predictable option for difficult meshes. Kiln configures Cycles, the target
image and nodes, saves to `//textures/`, wires the normal map, and restores the
previous Blender state. Bakes integrate with an existing Impasto stack instead
of replacing its Normal connection. Normals-only for now.

### [Calipers](addons/calipers/) — v1.2.0

Scale-aware voxel-remesh preview and safety (the Proposal §5 prototype). A
sidebar panel shows, for both Sculpt Mode Voxel Remesh and the Remesh modifier,
what the current voxel size means for the selected mesh: cell counts along each
axis, a green/yellow/red cost band, bounding-box dimensions, and scale warnings.
Safe modifier creation and destructive-remesh preflight prevent Blender's
default `0.1 m` voxel size from triggering a prohibitively expensive operation
without review. A viewport guide draws grid slices and voxel-sized samples at
all eight bounding-box corners so scale can be judged visually.

### [Impasto](addons/impasto/) — v0.9.11 (active development)

A non-destructive Principled-PBR layer stack with Fill, Paint, and pass-through
Group layers. One logical Paint layer can own separate Base Color, Metallic,
Roughness, Tangent Normal, Height, Emission, and Subsurface images, with
generated node graphs that keep those channels composited independently. Kiln
normal bakes can become the stack's baseline normal layer without damaging the
active painting setup.

A Standard stack can be expanded later from **Add Material Channel** without
recreating it. Emission Color/Strength and the paintable Subsurface
Weight/Radius/Scale channels can be registered and bound to the selected Paint
layer in place; new canvases inherit that layer's resolution and existing
bindings/images remain untouched. Subsurface IOR and Anisotropy are supported
as register-only material channels rather than paint canvases.

Impasto currently offers three painting paths:

- Blender's native Texture Paint brush for one channel at a time;
- **Blender Brush → N Channels**, which captures a native Draw stroke and
  replays its footprint into every enabled channel; and
- an experimental **GPU Paint All Channels** session that keeps channel
  textures GPU-resident, previews the composed PBR result while painting, and
  flushes them to Blender images explicitly or on session exit.

The streamlined GPU workflow includes a brush-sized reticle, front-surface
depth rejection for both painting and preview, per-stroke multi-channel GPU
undo/redo, continuous pressure-aware tablet strokes, and a Lit PBR preview with
adjustable environment, key, and fill lighting. It uses Blender corner normals
for smooth shading and makes roughness, metallic, tangent-normal, and Height
changes visible without routine synchronization. A shared image stencil can
act as a viewport stencil, per-dab alpha, or grayscale normal profile. In the
common same-UV/topmost-active-layer case, lower Fill/Paint layers—including
alpha-zero Kiln normals—are composed as a resident baseline from the first
preview draw. Opaque upper normal maps still replace lower normals because
normal layers use ordinary encoded-RGB MIX rather than RNM/UDN composition. The
preview remains a perceptual approximation rather than Blender's exact
Material Preview HDRI.

For materials whose existing normal map cannot enter Impasto's restricted
resident stack, GPU painting also offers an explicit **Base Normal Map**
fallback. It supplies that image, UV map, strength, and optional green-channel
inversion to Lit PBR and the normal diagnostic previews only. It does not edit
the material node graph, painted images, bake/export output, or Blender's
authoritative Material Preview; inspect the Blender material to verify the
final result. Planned follow-up will discover Kiln's bake target automatically
while retaining the explicit image picker as the authoritative manual override.
Mixed UVs, image masks, participating upper layers, channel isolation,
bake-down/export, arbitrary Blender brush textures, and specialized brush tools
remain future work. Ctrl-S safely flushes before saving; menu-driven
save/export should be preceded by **Flush for Save / Export**.

## Documents

- [PROPOSAL.md](PROPOSAL.md) — the full proposal: the features above, plus
  longer-term layered painting and voxel-sculpting changes that ultimately need
  work in Blender's core.
- [research/](research/) — technical research feeding the flagship designs.

## Approach

Features are piloted as Python add-ons with agent-assisted development. Each
add-on carries a test suite (`tests/run_tests.sh`) that exercises a real Blender
binary, including — where the domain allows — end-to-end assertions. Kiln's
suite performs an actual Cycles normal bake and checks its pixel statistics;
Impasto additionally has foreground GPU smoke coverage because viewport draw
handlers cannot be validated completely in background mode. API behavior is
probed against the running binary rather than assumed, and the traps found
along the way are documented in the add-on READMEs.

## Status

Active development. Seam Path Tool, UV Island Overlay, Kiln, and Calipers have
complete guided workflows. Impasto's layer stack and native painting paths are
usable, while its high-performance multi-channel GPU brush remains experimental
and is being qualified interactively on larger meshes and textures.

## License

The add-ons are GPL-2.0-or-later (as Blender add-ons must be; see SPDX headers). Documentation and research notes: all rights reserved for now.
