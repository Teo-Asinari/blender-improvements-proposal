# Impasto

Impasto is an in-development, non-destructive PBR layer stack for Blender
materials. Phase 1 establishes the stack data model and compiler: fill layers,
pass-through groups, channel bindings, paint-mask graph structure, minimal node
reconciliation, and a small 3D Viewport sidebar for creating and arranging the
stack without working directly in the Shader Editor.

> **Development status:** Phase 1 prototype, not a finished painting add-on.
> Paint layers, native-brush canvas switching, the full channel registry,
> templates, channel isolation, smart masks, bake-down/export, and GPU strokes
> belong to later phases.

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

## Basic Phase 1 usage

1. Select a mesh with a material that uses nodes and contains a Principled
   BSDF.
2. Open the 3D Viewport sidebar with `N`, then choose the **Impasto** tab.
3. Create a new layer stack.
4. Add Fill or Group layers, bind available channels, and adjust layer order,
   opacity, blend mode, and visibility.
5. Use **Rebuild Stack** only to repair or explicitly regenerate the compiled
   graph; ordinary uniform edits should not require it.

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
