# UV Island Overlay

Per-island **UV coloring in the 3D viewport** for Blender. Every
UV island of the active mesh is tinted with its own distinct color, drawn
translucently over the surface, so island boundaries are instantly visible
— no round-trip to the UV editor needed. Since v1.1.0 the overlay updates
**live while you mark seams**: islands are predicted
from seam topology, no Unwrap required.

*(No screenshots included — see the GUI checklist below to see it live in
under a minute.)*

## Install

Legacy add-on packaging (`bl_info`), works on Blender 4.2+ / 5.x:

1. Zip the `uv_island_overlay` folder (the folder itself, so the zip
   contains `uv_island_overlay/__init__.py`).
2. Blender: `Edit > Preferences > Add-ons > Install from Disk…`, pick the
   zip, enable **UV Island Overlay**.

For development, symlink/copy the folder into your Blender
`scripts/addons/` directory instead.

## Usage

1. Select a mesh object (Object or Edit Mode).
2. Open the 3D viewport **sidebar** (press `N`) and pick the
   **UV Islands** tab — it has the **UV Island Colors** checkbox, a
   refresh button, and a live **island count**.
3. The same controls also live at the bottom of the viewport header
   **Overlays popover** (the two-overlapping-circles icon), where
   overlay toggles conventionally go.
4. The toggle is also a menu entry — **View > Toggle UV Island Overlay**
   in any mode, and **UV > Toggle UV Island Overlay** in Edit Mode
   (right next to Unwrap) — so F3 menu search finds it; or bind
   `uv.island_overlay_toggle` to a key.

Colors are assigned by golden-ratio hue stepping with stable ordering:
recomputing the same mesh keeps the same colors, and adding islands never
reshuffles existing ones.

## Island sources: Seams (predicted) vs UVs (actual)

Both panels have a **Source** dropdown choosing how islands are defined:

- **Seams (predicted)** — the default. Islands are connected face regions
  bounded by seam edges (boundary edges bound; non-manifold edges connect
  all their faces unless seamed). No UV data is read at all, so this
  works on meshes that were never unwrapped and **updates live as you
  mark/clear seams** with any tool. Because Blender's Unwrap splits
  charts exactly at seams, this mode *predicts the post-unwrap islands*:
  it shows the same partition the next Unwrap will produce (the test
  suite asserts SEAM-partition == UV-partition-after-unwrap on several
  meshes). Paint seams, watch
  the islands split, unwrap once you like the layout.
- **UVs (actual)** — true UV-space connectivity of the *current* unwrap:
  two faces are in the same island iff their shared edge's loop UVs
  coincide on both sides. Follows the actual UV data — Smart UV Project
  charts, lightmap packs and hand-split UVs are detected correctly even
  with zero seam flags — but only changes when the UVs change (i.e.
  after unwrapping). This was the only mode before v1.1.0.

The island-count label shows which source produced it: *"6 islands
(predicted)"* vs *"(actual)"*. Switching the source invalidates and
rebuilds immediately.

Default rationale: the primary workflow this overlay serves is
interactive seam marking, where SEAM is the mode that does something
UV mode cannot (live feedback without paying for an unwrap); it is also
~5x cheaper to compute on large meshes. If you unwrap without seams
(Smart UV Project etc.), switch to **UVs (actual)**.

### Refreshing

- Islands are computed when you toggle the overlay on.
- **SEAM source — live updates:** marking or clearing seams (or any other
  geometry edit) recomputes the overlay automatically, ~0.3 s after the
  edit burst goes quiet. The pipeline is deliberately cheap:
  1. the `depsgraph_update_post` hook does O(1) work per tick (records a
     timestamp — safe even during a transform drag);
  2. a debounced `bpy.app.timers` callback waits for 0.3 s of quiet, then
     computes a **checksum** of seam flags + vertex positions + element
     counts (~0.1 s on a 300k-vert mesh; edit-mode state is snapshotted
     with `bm.to_mesh()` into a private datablock because
     `update_from_editmode()` would tag the depsgraph and re-trigger the
     hook — probed on 5.1.2);
  3. only if the checksum actually changed does the rebuild run, on a
     numpy-vectorized path (~0.65 s at 300k verts vs ~3.1 s for the old
     per-face Python loop). No-op events — mode switches, re-marking an
     existing seam, selection changes — never trigger a rebuild.

  Measured on a 302,500-face / 303,601-vert grid (Blender 5.1.2,
  headless): seam+position checksum 0.10 s; vectorized island computation
  0.06 s (pure-Python union-find: 0.57 s); full geometry rebuild 0.64 s
  (bmesh path: 3.08 s). Rebuild cost scales linearly with face count, so
  a burst of seam edits on a 300k-vert mesh costs about one second after
  you pause — no per-edit cost, hence no face-count cutoff or "Live"
  sub-toggle was needed.
- **UV source:** mesh edits and mode switches on the overlaid object are
  picked up automatically (the depsgraph hook marks the overlay dirty;
  the recompute happens once at the next viewport draw — never per
  frame). UV edits made purely in the UV editor may not flag a geometry
  update — hit **Refresh** after re-unwrapping if in doubt.
- The **refresh button** (or `uv.island_overlay_refresh`) is always
  available as the escape hatch in either mode.

### Troubleshooting

Draw-time failures are **never silent**: if anything goes wrong inside
the viewport draw callback, the overlay suspends itself, prints the full
traceback **once** to the system console (`Window > Toggle System
Console` on Windows), and both panels show a *"Draw failed — see system
console"* error row. Hitting **Refresh** clears the error and retries.

## How islands are detected

`islands.py` implements both sources, pure and bpy-free:

- `compute_islands` — **true UV-space connectivity**: two faces sharing a
  mesh edge belong to the same island iff that edge's loop UVs coincide
  on both sides (within epsilon). Follows the *actual unwrap result*
  even when **no edge is flagged as a seam** (the test suite includes a
  smart-projected cube: 6 islands, zero seam flags).
- `compute_islands_by_seam` — **seam-flag connected components** (the
  SEAM source; also the fallback for UV mode on meshes with no UV
  layer). `compute_islands_by_seam_arrays` is its numpy-vectorized twin
  (min-label propagation with pointer jumping over flat `foreach_get`
  arrays) — identical partition and island ordering, ~10x faster at
  300k faces; the test suite asserts equivalence on several meshes
  including non-manifold ones.

## Relationship to the texel-density checker overlay

This add-on's island computation (`islands.py`: `compute_islands`,
`face_index_to_island`, stable color assignment) is the **shared
foundation** for the planned texel-density checker overlay: that tool
needs the same face→island partition to compute per-island UV-area /
mesh-area ratios and tint islands by density instead of by id. Keep
`islands.py` pure (no `bpy`/`gpu` imports) so both overlays can reuse it.

## Limitations

- Overlays **one object at a time** (the active mesh when toggled on).
- The tint is pushed slightly along each face normal to avoid z-fighting;
  from the *back side* of single-sided geometry the overlay is therefore
  hidden by the surface (by design — it never bleeds through walls).
- UV-editor-only edits may not trigger the auto-refresh hook; use the
  manual **Refresh** button after re-unwrapping if in doubt.
- SEAM source can only see seams: charts split by Smart UV Project or
  manual UV edits (no seam flags) show as one predicted island — switch
  the source to **UVs (actual)** for those.
- While enabled in SEAM mode on an edit-mode mesh, a private snapshot
  mesh datablock (`.uv_island_overlay.snapshot`) holds a copy of the
  mesh for cheap array reads; it is removed when the overlay is
  disabled.
- The overlay draws the mesh *without* modifier results in Object Mode
  (it reads the base mesh, matching what unwrapping operates on).
- Colors are distinct but unlabeled; with hundreds of islands, adjacent
  hues can get close (value is cycled to compensate).

## Tests

Headless suite against the real binary (WSL → Windows Blender):

```bash
addons/uv_island_overlay/tests/run_tests.sh
# optionally: run_tests.sh "/path/to/blender.exe"
```

Blender exits 0 even when a `--python` script raises, so each test prints
a sentinel (`ISLANDS_TESTS_PASSED`, `REGISTER_TESTS_PASSED`) and the
wrapper greps for them, printing `ALL_TESTS_PASSED` / `TESTS_FAILED` and
setting the exit code. GPU shader/batch creation is impossible in
`--background` (raises `SystemError` on 5.1.2), so all gpu work is
deferred to draw time and exception-guarded; the register test verifies
the draw callback no-ops gracefully headlessly.

## Credits

Inspired by island-visualization workflows found in specialized 3D tools
such as 3DCoat. This project is independent and is not affiliated with or
endorsed by Pilgway.
