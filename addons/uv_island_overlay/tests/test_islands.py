# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for islands.py (run inside `blender --background --python`).

Builds meshes procedurally, unwraps with real operators, and checks island
computation, color assignment and lookup tables.

Prints ISLANDS_TESTS_PASSED on success. Blender exits 0 even on unhandled
exceptions in --python scripts, so the wrapper greps for the sentinel.
"""

import os
import sys
import traceback

import bpy
import bmesh

# Make the add-on package importable from its source location.
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

from uv_island_overlay import islands  # noqa: E402
from uv_island_overlay import live  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def edit_bmesh_of_active():
    obj = bpy.context.active_object
    if obj.mode != 'EDIT':
        bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    return obj, bm


def assert_partition(name, isl, n_faces):
    """Every face in exactly one island."""
    seen = []
    for s in isl:
        seen.extend(s)
    check(name + ": partition covers every face exactly once",
          sorted(seen) == list(range(n_faces)),
          "faces seen: %r of %d" % (sorted(seen), n_faces))


# ---------------------------------------------------------------------------

def test_cube_all_seams_unwrap():
    print("test_cube_all_seams_unwrap")
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj, bm = edit_bmesh_of_active()

    for e in bm.edges:
        e.seam = True
    for f in bm.faces:
        f.select = True
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.02)

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.active
    check("cube has a UV layer after unwrap", uv_layer is not None)

    isl = islands.compute_islands(bm, uv_layer)
    check("cube with all seams -> 6 islands", len(isl) == 6,
          "got %d" % len(isl))
    assert_partition("cube", isl, len(bm.faces))
    check("cube islands are singletons", all(len(s) == 1 for s in isl))

    # Seam-fallback agrees on this mesh (all edges are seams).
    isl_seam = islands.compute_islands_by_seam(bm)
    check("cube seam-fallback also -> 6 islands", len(isl_seam) == 6,
          "got %d" % len(isl_seam))

    bpy.ops.object.mode_set(mode='OBJECT')


def test_grid_middle_seam():
    print("test_grid_middle_seam")
    reset_scene()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4,
                                    size=2.0)
    obj, bm = edit_bmesh_of_active()

    # Seam line down the middle (edges whose both verts sit at x == 0).
    n_seam = 0
    for e in bm.edges:
        if all(abs(v.co.x) < 1e-6 for v in e.verts):
            e.seam = True
            n_seam += 1
    check("grid: found a middle seam line", n_seam > 0, "0 seam edges")

    for f in bm.faces:
        f.select = True
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.02)

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.active
    isl = islands.compute_islands(bm, uv_layer)
    check("grid with middle seam -> 2 islands", len(isl) == 2,
          "got %d" % len(isl))
    assert_partition("grid", isl, len(bm.faces))

    # Expected membership: left half (face center x < 0) vs right half.
    left = {f.index for f in bm.faces if f.calc_center_median().x < 0.0}
    right = {f.index for f in bm.faces if f.calc_center_median().x > 0.0}
    got = {frozenset(s) for s in isl}
    check("grid island membership is left/right halves",
          got == {frozenset(left), frozenset(right)},
          "got %r" % got)

    # Seam-fallback path must agree with UV connectivity here.
    isl_seam = islands.compute_islands_by_seam(bm)
    check("grid seam-fallback matches UV islands",
          {frozenset(s) for s in isl_seam} == got)

    bpy.ops.object.mode_set(mode='OBJECT')


def test_no_uv_layer_fallback():
    print("test_no_uv_layer_fallback")
    # Pure bmesh, never touched by an unwrap: no UV layer at all.
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=4, y_segments=4, size=1.0)
    check("procedural grid has no UV layer",
          bm.loops.layers.uv.active is None)

    # No seams: one island.
    isl = islands.compute_islands_by_seam(bm)
    check("no seams -> 1 island", len(isl) == 1, "got %d" % len(isl))

    # Seam the middle line, expect 2.
    for e in bm.edges:
        if all(abs(v.co.x) < 1e-6 for v in e.verts):
            e.seam = True
    isl = islands.compute_islands_by_seam(bm)
    check("middle seam -> 2 islands (fallback)", len(isl) == 2,
          "got %d" % len(isl))
    assert_partition("fallback grid", isl, len(bm.faces))
    bm.free()


def test_disjoint_pieces():
    print("test_disjoint_pieces")
    reset_scene()
    # Two separate cubes inside ONE mesh object, no seams anywhere.
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    obj = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.duplicate_move(
        TRANSFORM_OT_translate={"value": (3.0, 0.0, 0.0)})
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.02)

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    check("joined mesh has 12 faces", len(bm.faces) == 12,
          "got %d" % len(bm.faces))

    uv_layer = bm.loops.layers.uv.active
    isl = islands.compute_islands(bm, uv_layer)
    check("two disjoint pieces -> 2 UV islands (no seams)",
          len(isl) == 2, "got %d" % len(isl))
    assert_partition("disjoint", isl, len(bm.faces))
    check("disjoint islands have 6 faces each",
          sorted(len(s) for s in isl) == [6, 6],
          "got %r" % sorted(len(s) for s in isl))

    # Fallback also separates disconnected components without any seams.
    isl_seam = islands.compute_islands_by_seam(bm)
    check("disjoint pieces -> 2 islands via seam-fallback too",
          len(isl_seam) == 2, "got %d" % len(isl_seam))

    bpy.ops.object.mode_set(mode='OBJECT')


def test_smart_uv_project_cube():
    print("test_smart_uv_project_cube")
    reset_scene()
    # No seam flags at all: island detection must follow the actual
    # unwrap (true UV-space connectivity), which Smart UV Project splits
    # into 6 charts on a cube (90-degree angles > 66-degree limit).
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.02)

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    check("cube has no seam flags", not any(e.seam for e in bm.edges))
    uv_layer = bm.loops.layers.uv.active
    isl = islands.compute_islands(bm, uv_layer)
    check("smart-projected cube -> 6 islands despite zero seams",
          len(isl) == 6, "got %d" % len(isl))
    assert_partition("smart cube", isl, len(bm.faces))

    # The seam-flag heuristic gets this WRONG (1 island) — which is
    # exactly why compute_islands uses UV-space connectivity.
    isl_seam = islands.compute_islands_by_seam(bm)
    check("seam-fallback sees 1 island here (documents its limitation)",
          len(isl_seam) == 1, "got %d" % len(isl_seam))

    bpy.ops.object.mode_set(mode='OBJECT')


def test_colors():
    print("test_colors")
    for n in (0, 1, 2, 6, 64):
        cols = islands.island_colors(n)
        check("island_colors(%d) returns %d colors" % (n, n),
              len(cols) == n)
        check("island_colors(%d) all distinct" % n,
              len(set(cols)) == n)
        check("island_colors(%d) all valid RGBA" % n,
              all(len(c) == 4 and all(0.0 <= x <= 1.0 for x in c)
                  for c in cols))
    a = islands.island_colors(16, seed=0)
    b = islands.island_colors(16, seed=0)
    check("island_colors deterministic across calls", a == b)
    c = islands.island_colors(16, seed=7)
    check("different seed -> different palette", a != c)
    # Stability: color i must not depend on n (growing island count keeps
    # existing colors).
    check("palette is prefix-stable",
          islands.island_colors(4) == islands.island_colors(8)[:4])
    # Alpha plumbed through.
    check("alpha parameter respected",
          all(col[3] == 0.4 for col in islands.island_colors(3, alpha=0.4)))


def _bm_to_seam_arrays(bm):
    """Flat arrays for compute_islands_by_seam_arrays, derived from a
    bmesh exactly the way overlay.py derives them from Mesh.foreach_get."""
    bm.faces.index_update()
    bm.edges.index_update()
    loop_edge = []
    loop_face = []
    for f in bm.faces:
        for l in f.loops:
            loop_edge.append(l.edge.index)
            loop_face.append(f.index)
    seam = [e.seam for e in bm.edges]
    return loop_edge, loop_face, seam


def _check_arrays_match_bmesh(name, bm):
    """The vectorized implementation must produce the SAME partition in
    the SAME order (colors depend on the order) as the bmesh reference."""
    ref = islands.compute_islands_by_seam(bm)
    loop_edge, loop_face, seam = _bm_to_seam_arrays(bm)
    mapping, n = islands.compute_islands_by_seam_arrays(
        len(bm.faces), loop_edge, loop_face, seam)
    check(name + ": arrays island count matches bmesh reference",
          n == len(ref), "arrays %d vs bmesh %d" % (n, len(ref)))
    check(name + ": arrays partition + ordering match bmesh reference",
          islands.islands_from_face_mapping(mapping) == ref)


def test_seam_arrays_equivalence():
    print("test_seam_arrays_equivalence")

    # Grid with a middle seam (has boundary edges).
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=6, y_segments=6, size=1.0)
    for e in bm.edges:
        if all(abs(v.co.x) < 1e-6 for v in e.verts):
            e.seam = True
    _check_arrays_match_bmesh("grid+seam", bm)
    bm.free()

    # Cube with every edge seamed (all-singleton islands).
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    for e in bm.edges:
        e.seam = True
    _check_arrays_match_bmesh("all-seam cube", bm)
    bm.free()

    # Two topologically disjoint cubes, no seams.
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.create_cube(bm, size=1.0)
    _check_arrays_match_bmesh("disjoint cubes", bm)
    bm.free()

    # Non-manifold fan: three faces sharing one edge.
    bm = bmesh.new()
    v0 = bm.verts.new((0, 0, 0))
    v1 = bm.verts.new((0, 0, 1))
    va = bm.verts.new((1, 0, 0))
    vb = bm.verts.new((-1, 0, 0))
    vc = bm.verts.new((0, 1, 0))
    bm.faces.new((v0, v1, va))
    bm.faces.new((v0, v1, vb))
    bm.faces.new((v0, v1, vc))
    _check_arrays_match_bmesh("non-manifold fan (no seam)", bm)
    isl = islands.compute_islands_by_seam(bm)
    check("non-manifold fan without seam is one island", len(isl) == 1)
    for e in bm.edges:
        if len(e.link_faces) == 3:
            e.seam = True
    _check_arrays_match_bmesh("non-manifold fan (seamed)", bm)
    isl = islands.compute_islands_by_seam(bm)
    check("seaming the shared edge splits the fan into 3", len(isl) == 3,
          "got %d" % len(isl))
    bm.free()

    # Empty input.
    mapping, n = islands.compute_islands_by_seam_arrays(0, [], [], [])
    check("empty mesh -> 0 islands, empty mapping",
          n == 0 and len(mapping) == 0)


def test_debounce():
    print("test_debounce (pure, fake clock)")
    d = live.Debounce(0.3)
    check("fresh debounce is idle", not d.pending)
    check("idle debounce never fires", not d.try_fire(100.0))

    # A burst of changes -> exactly ONE fire, after quiet from the LAST.
    d.note_change(1.00)
    d.note_change(1.10)
    d.note_change(1.25)
    check("pending during burst", d.pending)
    check("no fire 0.20s after last change", not d.try_fire(1.45))
    check("still pending", d.pending)
    check("fires 0.30s after last change", d.try_fire(1.56))
    check("burst produced exactly one fire", not d.try_fire(1.60))
    check("idle after firing", not d.pending)

    # Re-arms for the next burst.
    d.note_change(2.0)
    check("re-armed debounce fires again", d.try_fire(2.31))

    # reset() cancels a pending burst.
    d.note_change(3.0)
    d.reset()
    check("reset cancels pending fire", not d.try_fire(10.0))


def _partition(islist):
    return {frozenset(s) for s in islist}


def test_seam_partition_predicts_unwrap():
    """A seam-marked but never (re)unwrapped mesh: the SEAM partition
    computed NOW must equal the UV partition a subsequent Unwrap
    produces — that is the whole premise of the predicted mode."""
    print("test_seam_partition_predicts_unwrap")

    # Mesh 1: grid with a middle seam. The primitive auto-generates UVs
    # (one chart), so before unwrapping the UV partition is stale.
    reset_scene()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4,
                                    size=2.0)
    obj, bm = edit_bmesh_of_active()
    for e in bm.edges:
        if all(abs(v.co.x) < 1e-6 for v in e.verts):
            e.seam = True
    bmesh.update_edit_mesh(obj.data)

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.active
    check("grid primitive has auto-generated UVs", uv_layer is not None)
    seam_isl = islands.compute_islands_by_seam(bm)
    uv_stale = islands.compute_islands(bm, uv_layer)
    check("grid: stale UVs say 1 island, seams predict 2",
          len(uv_stale) == 1 and len(seam_isl) == 2,
          "uv %d seam %d" % (len(uv_stale), len(seam_isl)))

    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.02)
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_isl = islands.compute_islands(bm, bm.loops.layers.uv.active)
    seam_after = islands.compute_islands_by_seam(bm)
    check("grid: seam partition unchanged by unwrapping",
          _partition(seam_after) == _partition(seam_isl))
    check("grid: SEAM prediction == UV partition after unwrap",
          _partition(uv_isl) == _partition(seam_isl),
          "uv %r seam %r" % (_partition(uv_isl), _partition(seam_isl)))
    bpy.ops.object.mode_set(mode='OBJECT')

    # Mesh 2: cylinder — cap-ring seams + one vertical seam (3 islands).
    reset_scene()
    bpy.ops.mesh.primitive_cylinder_add(vertices=16)
    obj, bm = edit_bmesh_of_active()
    vertical_done = False
    for e in bm.edges:
        if any(len(f.verts) > 4 for f in e.link_faces):
            e.seam = True     # cap rings (edges touching an ngon cap)
        elif not vertical_done and len(e.link_faces) == 2 and \
                abs(e.verts[0].co.x - e.verts[1].co.x) < 1e-6 and \
                abs(e.verts[0].co.y - e.verts[1].co.y) < 1e-6:
            e.seam = True     # one vertical side edge
            vertical_done = True
    bmesh.update_edit_mesh(obj.data)

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    seam_isl = islands.compute_islands_by_seam(bm)
    check("cylinder: seams predict 3 islands (2 caps + body)",
          len(seam_isl) == 3, "got %d" % len(seam_isl))

    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.02)
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_isl = islands.compute_islands(bm, bm.loops.layers.uv.active)
    check("cylinder: SEAM prediction == UV partition after unwrap",
          _partition(uv_isl) == _partition(seam_isl),
          "uv %d islands, seam %d islands"
          % (len(uv_isl), len(seam_isl)))
    bpy.ops.object.mode_set(mode='OBJECT')


def test_face_index_to_island():
    print("test_face_index_to_island")
    isl = [{0, 2}, {1, 3, 4}, {5}]
    mapping = islands.face_index_to_island(isl)
    check("mapping length == total faces", len(mapping) == 6)
    check("mapping values correct",
          mapping == [0, 1, 0, 1, 1, 2], "got %r" % mapping)
    check("every face mapped (no -1 left)", -1 not in mapping)
    check("empty islands -> empty mapping",
          islands.face_index_to_island([]) == [])


# ---------------------------------------------------------------------------

try:
    test_cube_all_seams_unwrap()
    test_grid_middle_seam()
    test_no_uv_layer_fallback()
    test_disjoint_pieces()
    test_smart_uv_project_cube()
    test_seam_arrays_equivalence()
    test_debounce()
    test_seam_partition_predicts_unwrap()
    test_colors()
    test_face_index_to_island()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("ISLANDS_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("ISLANDS_TESTS_PASSED")
sys.stdout.flush()
