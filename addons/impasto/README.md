# Impasto

Impasto is an in-development, non-destructive PBR layer stack for Blender
materials. Phase 1 establishes the stack data model and compiler: fill layers,
pass-through groups, channel bindings, paint-mask graph structure, minimal node
reconciliation, and a small 3D Viewport sidebar for creating and arranging the
stack without working directly in the Shader Editor.

> **Development status:** Phase 1 stack foundation plus the first Phase 3
> native-paint workflow. Paint layers work with Blender's native brush; masks,
> channel isolation, bake-down/export, and GPU strokes remain later work.

## Phase 1 scope

The current milestone includes:

- stack, layer, binding, mask, and material state stored in the generated root
  shader node group;
- fill layers and pass-through groups;
- Base Color and Roughness bindings;
- pure model-to-graph compilation with committed golden specifications;
- minimal reconciliation that repairs drift and produces zero mutations when
  the graph already matches;
- two-tier debounce so uniform edits do not structurally rebuild the graph;
- a minimal **Impasto** N-panel with stack creation, layer add/remove/reorder,
  visibility, opacity, blend controls, and manual rebuild.

The binding design and complete roadmap live in
[`../../research/layer-stack-design.md`](../../research/layer-stack-design.md).

## Install for development

Impasto uses Blender's legacy add-on packaging (`bl_info`) and targets Blender
5.1.2 during Phase 1.

1. Zip the `impasto` folder itself, so the archive contains
   `impasto/__init__.py` at its root.
2. In Blender, choose `Edit > Preferences > Add-ons > Install from Disk...`,
   select the zip, and enable **Impasto**.

For local development, copy or symlink `addons/impasto/` into Blender's
`scripts/addons/` directory. Do not include `tests/` or `__pycache__/` in a
release archive.

## Paint with Blender's native brush

1. Select a mesh with a material that uses nodes and contains a Principled
   BSDF.
2. Open the 3D Viewport sidebar with `N`, then choose the **Impasto** tab.
3. Create a new layer stack.
4. Add a Paint layer. It creates a transparent 2048 x 2048 image using the
   mesh's currently active UV map.
5. Select that layer and click **Paint Active Layer**. Impasto makes its image
   Blender's explicit image-paint canvas and enters Texture Paint mode.
6. Use Blender's normal Texture Paint brushes. Strokes update the image sampled
   by the generated Impasto layer graph, so they appear through the material.
7. Add Fill or Group layers, bind available channels, and adjust layer order,
   opacity, blend mode, and visibility.
8. Use **Rebuild Stack** only to repair or explicitly regenerate the compiled
   graph; ordinary uniform edits should not require it.

Selecting another Paint layer switches the canvas but does not force a mode
change. The explicit button is the safe way to enter Texture Paint. If a layer's
stored UV map or image was deleted, activation stops and reports what is missing
instead of allowing Blender to paint into a different target.

Native paint is currently one image and one PBR channel per Paint layer. Use
**Add Channel Paint Layer** to create a dedicated Base Color, Roughness,
Metallic, Height, or Tangent Normal image with the correct colorspace. Impasto
rejects a second shared channel on the same native Paint layer: Blender's native
brush cannot deposit independent values into multiple PBR channels in one
stroke. GPU multi-channel painting is still a separate experiment.

### Normal and height painting

**Tangent Normal (RGB)** bindings treat the paint image as an absolute
tangent-space normal map. Images
are stored as **Non-Color**, conventional encoded RGB `(0.5, 0.5, 1.0)` is a
flat normal, and the compiled shader decodes the blended image through Blender's
Normal Map node. Create a dedicated Tangent Normal channel Paint layer, activate
it, and paint/import encoded tangent-normal colors.
Blender's ordinary color brush does not generate sculpt-like normals from brush
pressure; it deposits the encoded RGB direction you choose. Repeating the same
stroke therefore does not accumulate additional relief. Use a Height Detail
layer for brush-built relief, and reserve Tangent Normal for painting/importing
encoded normal directions.

**Height Detail** is a grayscale derivative field centered on neutral mid-gray.
The **Raise** and **Lower** buttons configure Blender's native brush to ADD or
SUBTRACT white, so repeated strokes accumulate above or below 0.5. Constant
black, gray, or white regions are all geometrically flat; visible bump comes
from spatial gradients and stroke falloff, not the absolute shade. The result
feeds Blender's Bump node. When Normal and Height are both present, the decoded
tangent normal feeds the Bump node's Normal input, and the combined result drives
Principled. Multiple Normal layers currently use an approximate MIX of encoded
normal colors before decoding. This is useful for masks and simple overlays but
is not mathematically exact RNM normal blending; keep full-strength detail maps
on separate layers conservative until RNM/UDN blending is implemented.
Native brush undo is Blender's normal paint undo and stack operators use normal
operator undo.

### GUI acceptance checklist

Headless tests verify target setup and graph wiring, but cannot synthesize a
real viewport brush stroke. Before packaging a release, verify interactively:

- create a stack on a UV-unwrapped mesh and add two Paint layers;
- click **Paint Active Layer**, paint a visible stroke in Material Preview,
  and confirm it appears in the Impasto material;
- select the other layer, confirm its image becomes the canvas, and paint a
  visually distinct stroke without changing the first image;
- undo and redo each native stroke, then undo a stack operation, confirming the
  two Blender undo paths interleave normally;
- save, reopen, select the paint layer, and confirm activation restores its
  saved image and UV target;
- delete or rename the stored UV map and confirm activation reports the missing
  UV rather than painting elsewhere.

Impasto owns its generated root and per-layer node groups. Treat those graphs
as build artifacts: edit the stack through Impasto rather than manually
rewiring generated nodes.

## Phase 1 acceptance gates

Phase 1 is complete only when all of these pass:

- pure golden and invariant tests;
- real-Blender zero-delta second reconciliation and tamper repair;
- save/reload and append persistence with stable UIDs, ordering, and bindings;
- undo across stack operators and cache rebuild;
- register, unregister, and re-register lifecycle;
- every operator exposed in the sidebar, a menu, and F3 search with an
  `Impasto:` label prefix;
- slider drags produce no node-tree mutations, verified by the delta log;
- the manual GUI responsiveness and undo-interleaving checklist in the design
  document.

## Tests

Pure compiler tests live in `tests/test_model.py`. A complete Phase 1 package
must also provide a `tests/run_tests.sh` Blender wrapper and headless lifecycle,
reconciliation, persistence, undo, and registration tests. The wrapper must
check explicit success sentinels because Blender can exit with status 0 after a
Python exception.

## Packaging checklist

Before distributing a Phase 1 archive:

- ensure `addons/impasto/__init__.py` exists and contains `bl_info`, module
  registration, and clean unregister logic;
- ensure the zip root is `impasto/`, not the repository root or the contents of
  `impasto/` without their parent folder;
- include runtime Python modules and this README;
- exclude `tests/`, golden fixtures, `__pycache__/`, `.pyc` files, and local
  logs;
- confirm no `Flapjack`, `flapjack`, `PBRStack`, or `pbrstack` identifier
  remains in runtime code, fixtures, docs, archive paths, or generated names;
- install the built archive into a clean Blender profile and run the
  register/re-register and smoke checks.

## License

GPL-2.0-or-later, consistent with Blender add-on requirements and the SPDX
headers in the source files.
