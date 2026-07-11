# Bake Flow

A **guided high-poly → low-poly normal-baking workflow** for Blender.
Setting up a high→low normal bake by hand is roughly fifteen
error-prone steps spread across three editors: switch the render
engine to Cycles, create an image datablock at the right size, set its
colorspace to Non-Color, give the low-poly a material, add an Image
Texture node, assign the image, make that node *active* (silently the
bake target), select the high-poly, ctrl-select the low-poly, enable
*Selected to Active*, guess a cage extrusion and max ray distance,
set the margin, bake, remember to save the image manually, and then
wire the result through a Normal Map node into the BSDF. Forget any
one of these and the bake is black, empty, sRGB-crushed or written
into the void.

Bake Flow collapses all of it into **one sidebar panel** that presents
the pipeline as three sequential stages with status checkmarks, and
one **Bake Normal Map** button that runs the whole gauntlet — with
every failure reported as a clear, actionable message instead of a
traceback or a silently black texture.

*(No screenshots included — see the GUI checklist at the bottom to see
it live in a minute.)*

## Install

Legacy add-on packaging (`bl_info`), works on Blender 4.2+ / 5.x
(probed throughout on 5.1.2):

1. Zip the `bake_flow` folder (the folder itself, so the zip contains
   `bake_flow/__init__.py`).
2. Blender: `Edit > Preferences > Add-ons > Install from Disk…`, pick
   the zip, enable **Bake Flow**.

For development, symlink/copy the folder into your Blender
`scripts/addons/` directory instead.

## Usage: the three stages

Open the 3D viewport **sidebar** (press `N`) and pick the **Bake
Flow** tab. The two main operators are also in the **Object menu**
(so F3 menu search finds *Create Low-Poly Candidate* and *Bake Normal
Map*).

### Stage 1 — High / Low pair

Pick your **High-Poly** (the dense sculpt, the source of detail) and
your **Low-Poly** (the retopologized bake target). Both pickers only
list mesh objects.

No low-poly yet? Set **Target Faces** and hit **Create Low-Poly
Candidate**: it duplicates the high-poly's *evaluated* surface
(modifiers applied — a Multires sculpt is captured at its visible
detail) and remeshes it with **QuadriFlow** to approximately the
target face count. If QuadriFlow fails (it can choke on non-manifold
input) or is unavailable, a **Decimate** modifier fallback runs
instead, and the report tells you which path was used.

> **The candidate is a starting point, not animation-grade topology.**
> QuadriFlow produces an even, seam-free quad grid with no regard for
> edge flow or deformation loops; Decimate produces faithful-but-ugly
> triangles. For static props it often suffices as-is; for anything
> that deforms, treat it as a base to retopologize properly. Either
> way the remesh has **no UVs** — stage 2 is next, always.

Settings live at **scene level** (`Scene.bake_flow`), so the pair and
all bake settings **survive save/load** — unlike a sibling add-on's
WindowManager properties, which are deliberately runtime-only (right
for a viewport toggle, wrong for a bake configuration that belongs to
the asset).

### Stage 2 — Seams & UVs (readiness checklist)

The panel continuously shows a checklist for the low-poly:

| Check | Severity | Meaning |
|---|---|---|
| UV layer | blocks | The low-poly must be unwrapped |
| UVs non-degenerate | blocks | An all-zero / collapsed / zero-area layout cannot receive a bake |
| Scale applied | warns | Non-unit object scale distorts the bake cage and tangent basis — an **Apply Scale** fix button appears |
| No mirrored transform | warns | Negative scale flips effective normals — **Recalculate Outside** fix button appears |

Blocking failures stop the bake operator with a message naming the
failed checks; warnings are reported but do not block. The
flipped-normals check is deliberately pragmatic: it detects the
cheap, reliable negative-scale case rather than running a full
winding-consistency analysis (the sibling overlay add-on shows
genuinely flipped faces visually — they vanish from its back-face-
culled overlay).

**Soft integration:** if the sibling add-ons are installed and
enabled, stage 2 grows convenience buttons — **Mark Seams
(interactive)** (Seam Path Tool) and **Toggle Island/Density Overlay**
(UV Island Overlay) — for marking seams and verifying islands/texel
density without leaving the panel. There is **no hard dependency and
no cross-import**: availability is probed at draw time via the
registered operator type (`bpy.types.MESH_OT_seam_path_interactive` /
`UV_OT_island_overlay_toggle` — probed on 5.1.2:
`hasattr(bpy.ops.mesh, name)` is `True` for *any* name, so the
operator *type* is the reliable signal), and absent siblings simply
show nothing.

### Stage 3 — Bake

Settings, then one button:

- **Bake Type** — `NORMAL` only for now; the machinery (image naming,
  colorspace, node wiring, operator settings) all switches on one
  table so more types slot in later (see TODOs).
- **Resolution** — 1024 / 2048 (default) / 4096, square.
- **Margin** — bake bleed in pixels (default 16).
- **Auto Distances** (default on) — see the heuristic below; disable
  to set **Extrusion** and **Max Ray Distance** manually.
- **Output Path** — empty (default) means
  `//textures/<lowpoly>_normal.png` next to the saved `.blend`
  (directories are created). Accepts absolute and `//`-relative
  paths; a trailing slash means "this directory, default file name".
  In an **unsaved** `.blend` with a relative path the operator refuses
  with a clear message instead of writing somewhere surprising
  (probed: `bpy.path.abspath("//…")` silently resolves against the
  process CWD when unsaved).
- **Wire Into Material** (default on) — after baking, connect
  `Image Texture → Normal Map (tangent) → Principled BSDF Normal` in
  the low-poly's material. Warns (doesn't fail) if the material has
  no Principled BSDF.

**Bake Normal Map** then: validates the pair and the checklist →
remembers your render engine and selection → switches to **Cycles**
(baking requires it; your configured Cycles device is left alone) →
creates or reuses the image datablock `<lowpoly>_normal` (Non-Color;
re-bakes never pile up `.001` copies) → ensures the low-poly has a
node material with a named Image Texture node targeting that image,
made the **active node** (the bake-target mechanism on 5.1.2 with the
default `IMAGE_TEXTURES` target) → selects high + low, makes low
active → runs `bpy.ops.object.bake(type='NORMAL',
use_selected_to_active=True, …)` with cage extrusion, max ray
distance and margin passed as **operator arguments** (probed on
5.1.2 — nothing in `scene.render.bake` is mutated, so nothing can be
left dirty) → saves the PNG → wires the material → **restores engine
and selection in a `finally` block**, even on failure.

## The extrusion heuristic

With **Auto Distances** on (the panel shows the live values):

```
extrusion        = 2% of the pair's combined world-space bounding-box diagonal
max ray distance = 4% of the same diagonal (= 2 x extrusion)
```

Rationale: the cage must be inflated past the largest high↔low
surface deviation, which for a sane retopo is a small fraction of the
model size — 2% comfortably covers typical QuadriFlow/manual retopo
error without ballooning the cage into self-intersections in concave
areas. Rays then travel from the inflated cage back through the low
surface to the high surface — up to roughly twice the extrusion.
Because both scale with the *pair's* diagonal, the defaults adapt to
any model size. Override cases: very thin shells or interior detail
close behind surfaces (lower both to avoid grabbing the wrong
surface), badly matched pairs (raise both — misses show as flat
`(0.5, 0.5, 1.0)` patches).

## Limitations

- **The bake blocks the UI.** `bpy.ops.object.bake` is synchronous
  when called from an operator; a 4K bake of a heavy sculpt will
  freeze Blender until it finishes. Acceptable for v1.
  *TODO: async/modal bake with progress (the `'INVOKE_DEFAULT'` bake
  is modal; wiring completion detection into the flow is the work).*
- **Normals only** (tangent space, +Y green). *TODO: AO,
  cavity/curvature, displacement — the enum, image naming,
  colorspace and node wiring already switch on one table
  (`flowcore.BAKE_TYPES`), each new type is an entry there + an enum
  item + a settings branch in `baking._bake_kwargs`.*
- No cage-object support (extrusion-based cage only). *TODO.*
- One high→low pair at a time; no multi-object / decal stacking.
- The bake targets the low-poly's **active material** (one is created
  if missing); multi-material low-polys bake into that slot's image
  only.
- The auto-generated candidate is **not animation-ready topology**
  and has **no UVs** by design (see stage 1).
- Output is always PNG; the image datablock is 8-bit non-float
  (fine for tangent-space normals; 16-bit output is a TODO).
- Baking requires Cycles; the add-on switches to it for the bake and
  restores your engine afterwards, but it cannot bake with EEVEE.

## Tests

Headless suite against the real binary (WSL → Windows Blender):

```bash
addons/bake_flow/tests/run_tests.sh
# optionally: run_tests.sh "/path/to/blender.exe"
```

Blender exits 0 even when a `--python` script raises, so each test
prints a sentinel (`CORE_TESTS_PASSED`, `READINESS_TESTS_PASSED`,
`RETOPO_TESTS_PASSED`, `BAKE_TESTS_PASSED`, `REGISTER_TESTS_PASSED`)
and the wrapper greps for them, printing `ALL_TESTS_PASSED` /
`TESTS_FAILED` and setting the exit code. Suite runtime: **~12 s**
total on 5.1.2 (five Blender launches; the bake itself is fast).

Headless Cycles **CPU baking works in `--background`** (probed on
5.1.2), so `test_bake.py` is a genuine end-to-end bake, not a mock: a
displaced 1152-face sphere is baked onto a smart-UV-projected 80-face
icosphere at 128×128 through the real operator, asserting the PNG
lands on disk (directories created), the pixels carry real normal
detail (mean RGB ≈ (0.501, 0.498, 0.989) — the tangent-space
(0.5, 0.5, 1.0) signature — with per-channel stddev ≈ (0.075, 0.066,
0.014), far above the uniform-image floor), the material got the
Image Texture → Normal Map → Principled wiring with the texture node
left *active*, re-bakes reuse datablocks, and the render engine and
selection are restored — including across the error paths, which are
asserted to raise actionable messages (same-object pair, UV-less
low-poly, unsaved-file output path). `test_retopo.py` covers the real
QuadriFlow run (face count in the target ballpark, all-quads, UVs
dropped, evaluated-mesh duplication with a Subsurf applied) plus the
Decimate fallback under simulated QuadriFlow failure *and* absence
(monkeypatching the module's two seams). `test_readiness.py` drives
every checklist state on constructed meshes, in Object and Edit Mode
(probed on 5.1.2: the Mesh UV arrays are empty while the edit bmesh
owns the data, so Edit Mode reads go through bmesh). `test_core.py`
unit-tests the pure logic (heuristic factors and linearity, path
resolution branches, UV-degeneracy math, scale checks, decimate
ratio) and pins `flowcore.py`'s bpy/gpu-free purity.
`test_register.py` covers the register/unregister/re-register
lifecycle, panel/menu discoverability, the soft-integration probe
(False without the siblings, flips True when a stand-in operator with
the sibling's exact idname registers, never raises), and proves the
scene-level settings **survive save/reopen** — the reason they are
Scene properties, not WindowManager ones.

### GUI checklist (not coverable headless)

1. Sidebar → **Bake Flow** tab: three numbered stage boxes with
   status icons; stage 2 and 3 show hints until both objects are
   picked.
2. Pick a high-poly sculpt, hit **Create Low-Poly Candidate**: a
   `<name>_low` object appears, selected and active; the info bar
   names the path used (QuadriFlow or Decimate fallback).
3. The checklist shows red ✗ rows (no UVs) — with the sibling
   add-ons enabled, the **Mark Seams (interactive)** and **Toggle
   Island/Density Overlay** buttons appear; with them disabled, no
   buttons and no errors.
4. Mark seams, unwrap, verify islands with the overlay; the
   checklist rows flip to checkmarks (within ~1 s — panel evaluation
   is TTL-cached).
5. Scale the low-poly: the *Scale applied* row warns and **Apply
   Scale** appears; clicking it fixes the row immediately.
6. **Bake Normal Map** with defaults on a saved file: UI blocks for
   the bake duration, then the info bar reports the resolution, the
   output path and the distances used; the PNG is in `//textures/`,
   the low-poly's material shows the wired normal map in Material
   Preview, and your render engine and selection are exactly as
   before.
7. Set Output Path to a read-only location: the bake reports a clear
   error and the engine/selection are still restored.

## Credits

Inspired by the one-button baker workflows of specialized baking and
texturing tools. This project is independent and is not affiliated
with or endorsed by the vendors of any such tools.
