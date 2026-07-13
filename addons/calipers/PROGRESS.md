# Calipers — scale-aware voxel-remesh preview and safety (Proposal §5 prototype)

Inter-session handoff file. Blueprint: `research/scale-aware-remesh-safety.md`
(implementation sequence §1–4 in scope; §5–6 out of scope).

## Probe results (Blender 5.1.2, ec6e62d40fa9 — all verified live)

Scripts in `probes/` (not part of the test suite). Findings:

### Research-doc claims CONFIRMED
- `Mesh.remesh_voxel_size` default 0.1, `remesh_voxel_adaptivity` 0.0.
- `RemeshModifier.voxel_size` default 0.1, `adaptivity` 0.0, mode enum
  `['BLOCKS','SMOOTH','SHARP','VOXEL']`, default mode `'VOXEL'`.
- Voxel size IS object-space: cube with unapplied 2x scale remeshes to the
  identical vert count (2648) as the unscaled cube; scale untouched by the op.
- Destructive op uses ORIGINAL mesh data: cube+subsurf(2) remeshed to the
  same 2648 verts; the modifier survives the op.
- Adding a Remesh modifier evaluates immediately (warning fired on a
  parameter change; enabled trailing modifier changes evaluated verts).
- Safe-add timing works: `modifiers.new()` + `show_viewport=False` in the
  same operator execution => the remesh never evaluates (evaluated verts
  stayed at the subsurf output count 98).
- Disabled TRAILING Remesh modifier => `evaluated_get()` yields exactly the
  geometry entering the modifier (98 subsurf verts). This is the exact-
  confidence path for the modifier context.

### Research-doc claims CONTRADICTED / REFINED (doc was written blind)
- `hasattr(bpy.types, "OBJECT_OT_voxel_remesh")` is **False** on 5.1.2 even
  though the operator exists and runs. Native (C) operators are not exposed
  as bpy.types attributes. Reliable existence probe:
  `bpy.ops.object.voxel_remesh.get_rna_type()` (raises KeyError for fakes).
- `Modifier.show_viewport` **RNA default is False**, but `modifiers.new()`
  and `object.modifier_add` both create it True. RNA default != DNA/factory
  behavior for this prop; do not trust RNA default here.
- `Mesh.remesh_voxel_size` hard_min is **0.0** (soft_min 1e-4): 0.0 is
  assignable. The native op then raises
  `RuntimeError: ... cannot run with a voxel size of 0.0` ({'CANCELLED'} is
  not returned — it raises). Modifier with voxel_size 0.0 logs
  "Zero voxel size cannot be solved" and outputs nothing (no crash).
- `RemeshModifier.adaptivity` hard range is **unbounded** (±3.4e38), unlike
  the mesh prop (0..1). Both adaptivity props report unit 'LENGTH'.
- Empty mesh: op returns {'CANCELLED'} gracefully. Flat plane (zero Z
  extent): op **succeeds** (968 verts — inflates a thin slab), so zero
  extents are valid input, not an error.
- Shape keys: op uses the BASIS coordinates (2648 verts despite a 3x shape
  key at value 1.0) and DESTROYS shape keys ("Shape key data lost" warning).
  So `mesh.vertices[].co` is the exact geometry source even with keys.
- Op poll: True for active mesh in Object Mode; False in Edit Mode; False
  with no active object. Runs headless fine.
- `evaluated_get(depsgraph).bound_box` is **NOT tight to the evaluated
  geometry** (a subsurf-shrunk cube still reports the base ±1 box in the
  headless flow). Tight bounds require scanning the evaluated
  `to_mesh()` vertices — which is what core.evaluated_stats does.
- Test-harness notes: `bpy.ops` RAISES RuntimeError in background when an
  operator reports {'ERROR'} (tests must accept either shape); modifier
  float properties are float32 in DNA (no exact float64 comparisons);
  PyRNA wrappers are fresh objects (compare `==`, never `is` — the
  sibling `nodes.active` trap applies to modifiers too).

## Status

- [done] Probes 01/02 (`probes/`) — findings above.
- [done] PROGRESS.md created.
- [done] `estimate.py` — pure estimator (no bpy; runs under bare python3),
  frozen Estimate result, saturating arithmetic, singular-value scale
  analysis (pure-python Jacobi — exact golden-ratio check in tests),
  world-target conversion, log-band risk.
- [done] `tests/test_estimate.py` green (CALIPERS_ESTIMATE_TESTS_PASSED).
- [done] `live.py` — Debounce (sibling pattern).
- [done] `core.py` — geometry-stats cache, source resolution
  (mesh/modifier contexts, exact-vs-approximate), RNA default reads,
  debounce plumbing. Tests green (CALIPERS_CORE_TESTS_PASSED).
- [done] `__init__.py` — settings (Scene.calipers), operators (safe-add,
  enable-confirm, remesh-confirm, set-from-world-target, refresh, overlay
  toggle), N-panel, depsgraph handler + debounce timer, menu entries,
  importlib-reload block. Tests green (CALIPERS_REGISTER_TESTS_PASSED).
- [done] `overlay.py` — GPU guide (sample cell + capped mid-slices +
  bbox + blf annotation), lazy+latch, create_from_info only, state
  restore + AST audit. Tests green (CALIPERS_OVERLAY_TESTS_PASSED).
- [done] `tests/run_tests.sh` — ALL_TESTS_PASSED (full suite verbatim run).
- [done] README.md.

## Remaining / out of scope

- Research-doc Implementation sequence §5 (empirical band calibration
  across builds/meshes/hardware) and §6 (Blender-core work) — out of
  scope per the task.
- GUI-interactive verification (dialogs, overlay visuals) needs a live
  viewport; everything testable headless is tested headless.

## Suite

`tests/run_tests.sh` — sentinels: CALIPERS_ESTIMATE_TESTS_PASSED,
CALIPERS_CORE_TESTS_PASSED, CALIPERS_REGISTER_TESTS_PASSED,
CALIPERS_OVERLAY_TESTS_PASSED; runner prints ALL_TESTS_PASSED.
Full suite green as of last edit (see README for the verbatim tail).

## Design decisions

- Estimator risk bands key on log10(bounding_cells) with configurable
  yellow/red exponents (defaults 7.0 / 9.0 ~= 215^3 / 1000^3 cells).
- Padding: +1 cell per axis on the bounding score (documented, avoids
  implying false precision); longest_axis_cells is the raw unpadded ceil.
- Modifier-context confidence: EXACT when the modifier is first in the
  stack (input == base mesh) or disabled-and-last (input == evaluated_get
  output, probed); APPROXIMATE otherwise (base-mesh stats stand in).
- Panel draw reads a stats cache only; stats extraction (vertex/area scans,
  depsgraph reads) happens in operators and a debounced timer, never draw.
- Safe-add disables BOTH show_viewport and show_render (a render with a
  never-confirmed modifier must not stall either); confirm re-enables both.
- Overlay anchors the sample cell at the bounds min corner; three mid-axis
  grid slices capped at SLICE_MAX_LINES per direction; above the cap the
  slice is dropped (partial grids would lie about density); extreme density
  => bbox + sample cell + annotation only.
