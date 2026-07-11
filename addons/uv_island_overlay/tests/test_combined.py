# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless tests for the COMBINED display mode (v1.4.0)
(run inside `blender --background --python`).

COMBINED = the DENSITY checker soup (positions + actual UVs) drawn
through the SAME density shader, with the ISLANDS per-island identity
colors baked as the per-vertex tint instead of deviation tints. Covers:
the enum/default flip to COMBINED, shader reuse (no third GLSL
variant), color equality with ISLANDS mode for both island sources, UV
soup presence, the no-UV draw-nothing contract, the deviation-tint and
opacity property semantics, mode-switch invalidation across all three
modes, and — via a fake clock — that BOTH invalidation paths (seam
edits and UV edits) converge on exactly ONE debounced rebuild in
COMBINED+SEAM while COMBINED+UV keeps the classic dirty -> draw path.

Prints COMBINED_TESTS_PASSED on success (sentinel-grepped by
run_tests.sh because Blender exits 0 even on unhandled --python
exceptions).
"""

import inspect
import os
import sys
import traceback

import bpy
import bmesh
import numpy as np

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def corner_multiset(coords, colors):
    """Order-independent multiset of (position, color) soup corners —
    ISLANDS and COMBINED builds may triangulate in different orders
    (bmesh calc_loop_triangles vs Mesh.loop_triangles)."""
    arr = np.hstack([np.asarray(coords, dtype=np.float32).reshape(-1, 3),
                     np.asarray(colors, dtype=np.float32).reshape(-1, 4)])
    return sorted(map(tuple, arr.tolist()))


def two_quads_2x():
    """Two disjoint unit quads; quad B's UVs are quad A's scaled by
    exactly 2 (the density-test mesh: densities 0.25 / 0.5, median
    0.375). Two islands under BOTH island sources (topologically
    disjoint, so seam prediction agrees with UV connectivity)."""
    me = bpy.data.meshes.new("CombinedTwoQuads")
    me.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
         (2, 0, 0), (3, 0, 0), (3, 1, 0), (2, 1, 0)],
        [], [(0, 1, 2, 3), (4, 5, 6, 7)])
    me.update()
    layer = me.uv_layers.new(name="UVMap")
    layer.data.foreach_set(
        "uv", [c for uv in
               [(0.0, 0.0), (0.25, 0.0), (0.25, 0.25), (0.0, 0.25),
                (0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)]
               for c in uv])
    obj = bpy.data.objects.new("CombinedTwoQuads", me)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    import uv_island_overlay
    from uv_island_overlay import overlay

    uv_island_overlay.register()
    wm = bpy.context.window_manager

    # --- meta: version, enum, default -----------------------------------
    check("version bumped to 1.4.0 for the combined mode",
          uv_island_overlay.bl_info.get("version", (0,))[:3] >= (1, 4, 0))
    mode_prop = wm.bl_rna.properties.get("uv_island_overlay_mode")
    check("mode enum has the COMBINED item",
          mode_prop is not None
          and {i.identifier for i in mode_prop.enum_items}
          == {'ISLANDS', 'DENSITY', 'COMBINED'})
    check("mode enum default is COMBINED", mode_prop.default == 'COMBINED')
    check("a fresh session starts in COMBINED (WM value == default)",
          wm.uv_island_overlay_mode == 'COMBINED')

    # --- shader reuse: no third GLSL variant exists ----------------------
    check("no COMBINED-specific shader constants exist (density shader "
          "is reused as-is)",
          not any(n.startswith("COMBINED") for n in dir(overlay)))
    draw_src = inspect.getsource(overlay._draw)
    check("draw routes COMBINED through the density shader, batch and "
          "uniform paths (three shared-mode branches)",
          draw_src.count("in ('DENSITY', 'COMBINED')") >= 3
          and "density_shader" in draw_src)
    check("COMBINED opacity comes from the Checker Opacity property "
          "(Tint Opacity stays ISLANDS-only)",
          "uv_island_overlay_density_opacity" in draw_src
          and draw_src.index("in ('DENSITY', 'COMBINED')")
          < draw_src.index("uv_island_overlay_density_opacity"))
    check("density create-info untouched by the combined mode",
          "COMBINED" not in inspect.getsource(
              overlay._density_shader_create_info))
    ui_src = inspect.getsource(uv_island_overlay._draw_overlay_controls)
    check("panel: Source dropdown shown for ISLANDS and COMBINED",
          "if mode in ('ISLANDS', 'COMBINED'):" in ui_src)
    check("panel: deviation-tint row is DENSITY-only",
          "if mode == 'DENSITY':" in ui_src
          and ui_src.index("if mode == 'DENSITY':")
          < ui_src.index("uv_island_overlay_density_tint"))
    check("panel: COMBINED shares the Checker Opacity row",
          "uv_island_overlay_density_opacity" in ui_src)

    # --- UV source: colors match ISLANDS, soup matches DENSITY -----------
    obj = two_quads_2x()
    wm.uv_island_overlay_source = 'UV'
    wm.uv_island_overlay_mode = 'ISLANDS'
    wm.uv_island_overlay = True
    check("baseline ISLANDS+UV: 2 islands on the two-quads mesh",
          overlay.is_enabled() and overlay.island_count() == 2)
    m_islands = corner_multiset(overlay._state.coords,
                                overlay._state.colors)

    wm.uv_island_overlay_mode = 'DENSITY'
    coords_d = overlay._state.coords
    uvs_d = overlay._state.uvs

    wm.uv_island_overlay_mode = 'COMBINED'
    check("mode switch to COMBINED rebuilds and reports the mode",
          overlay.active_mode() == 'COMBINED'
          and overlay._state.coords is not coords_d)
    check("COMBINED sees the 2 islands, not flagged no-UVs",
          overlay.island_count() == 2 and not overlay.has_no_uvs())
    check("COMBINED soup carries one UV per vertex (checker input in "
          "the batch)",
          overlay._state.uvs is not None
          and len(overlay._state.uvs) == len(overlay._state.coords))
    check("COMBINED (UV source) position+UV soup identical to the "
          "DENSITY soup",
          np.array_equal(np.asarray(overlay._state.coords),
                         np.asarray(coords_d))
          and np.array_equal(np.asarray(overlay._state.uvs),
                             np.asarray(uvs_d)))
    check("COMBINED colors ARE the ISLANDS-mode identity colors (same "
          "mesh, same source; order-independent corner comparison)",
          corner_multiset(overlay._state.coords, overlay._state.colors)
          == m_islands)
    cols = np.asarray(overlay._state.colors)
    check("COMBINED colors are per-island hues, not deviation tints "
          "(2 distinct rows, none neutral)",
          len(np.unique(cols.round(6), axis=0)) == 2
          and not np.any(np.all(cols[:, :3] == np.float32(1.0), axis=1)))
    check("density stats still exposed for the panel (median 0.375)",
          overlay.median_density() == 0.375
          and list(overlay._state.densities) == [0.25, 0.5],
          "median %r densities %r" % (overlay.median_density(),
                                      overlay._state.densities))

    # --- property semantics in COMBINED ----------------------------------
    coords_before = overlay._state.coords
    colors_before = overlay._state.colors
    wm.uv_island_overlay_density_tint = False
    check("deviation-tint toggle is ignored in COMBINED (no rebuild, "
          "hues kept)",
          overlay._state.coords is coords_before
          and overlay._state.colors is colors_before
          and not overlay._state.dirty)
    wm.uv_island_overlay_density_tint = True
    check("re-enabling the tint is equally a no-op in COMBINED",
          overlay._state.colors is colors_before
          and not overlay._state.dirty)
    wm.uv_island_overlay_density_opacity = 0.5
    check("Checker Opacity change is uniform-only in COMBINED (no "
          "dirty flag, no rebuild)",
          overlay._state.coords is coords_before
          and not overlay._state.dirty)
    wm.uv_island_overlay_density_opacity = overlay.DEFAULT_DENSITY_OPACITY
    wm.uv_island_overlay_checker_size = 64
    check("Checker Size change is uniform-only in COMBINED",
          overlay._state.coords is coords_before
          and not overlay._state.dirty)
    wm.uv_island_overlay_checker_size = 32

    # --- mode-switch invalidation across all three modes -----------------
    for target in ('ISLANDS', 'DENSITY', 'COMBINED', 'ISLANDS',
                   'COMBINED', 'DENSITY', 'COMBINED'):
        prev = overlay._state.coords
        wm.uv_island_overlay_mode = target
        ok = (overlay.active_mode() == target
              and overlay._state.coords is not prev
              and ((overlay._state.uvs is None) == (target == 'ISLANDS')))
        check("switch to %s invalidates + rebuilds (uvs %s)"
              % (target, "absent" if target == 'ISLANDS' else "present"),
              ok)
    wm.uv_island_overlay = False

    # --- SEAM source: predicted hues over actual (stale) checkers --------
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.mark_seam()

    wm.uv_island_overlay_source = 'SEAM'
    wm.uv_island_overlay_mode = 'ISLANDS'
    wm.uv_island_overlay = True
    check("baseline ISLANDS+SEAM: 6 predicted islands on the all-seam "
          "cube", overlay.island_count() == 6)
    coords_is = overlay._state.coords
    colors_is = overlay._state.colors

    wm.uv_island_overlay_mode = 'COMBINED'
    check("COMBINED+SEAM: hues are the 6 predicted islands (no unwrap "
          "happened)", overlay.island_count() == 6,
          "got %d" % overlay.island_count())
    check("COMBINED+SEAM positions identical to the ISLANDS+SEAM soup",
          np.array_equal(np.asarray(overlay._state.coords),
                         np.asarray(coords_is)))
    check("COMBINED+SEAM colors identical to the ISLANDS+SEAM colors",
          np.array_equal(np.asarray(overlay._state.colors),
                         np.asarray(colors_is)))
    check("COMBINED+SEAM soup carries UVs for the checker",
          overlay._state.uvs is not None
          and len(overlay._state.uvs) == len(overlay._state.coords))
    check("COMBINED+SEAM stored a checksum for the live gate",
          overlay._state.seam_checksum is not None)
    check("standalone UV-inclusive checksum matches the build's (the "
          "live tick gates on this equality)",
          overlay.seam_state_checksum(cube, include_uvs=True)
          == overlay._state.seam_checksum)
    check("UV-inclusive checksum differs from the plain SEAM checksum "
          "(UV bytes really participate)",
          overlay.seam_state_checksum(cube, include_uvs=True)
          != overlay.seam_state_checksum(cube))

    # The checker samples the ACTUAL (stale, single-chart) UVs while the
    # hues predict 6 islands — DENSITY (always actual) sees 1.
    wm.uv_island_overlay_mode = 'DENSITY'
    check("DENSITY on the same cube sees the 1 stale actual island "
          "(documents the predicted-hue vs stale-checker split)",
          overlay.island_count() == 1)
    wm.uv_island_overlay_mode = 'COMBINED'
    check("back to COMBINED: 6 predicted hues again",
          overlay.island_count() == 6)

    # Source switch inside COMBINED behaves exactly like ISLANDS mode.
    wm.uv_island_overlay_source = 'UV'
    check("COMBINED source switch to UV: 1 actual island, no checksum "
          "(dirty/draw path owns rebuilds)",
          overlay.island_count() == 1
          and overlay._state.seam_checksum is None)
    wm.uv_island_overlay_source = 'SEAM'
    check("COMBINED source switch back to SEAM: 6 predicted islands, "
          "checksum restored",
          overlay.island_count() == 6
          and overlay._state.seam_checksum is not None)

    # --- invalidation convergence: exactly ONE rebuild -------------------
    # COMBINED+SEAM routes ALL geometry updates through the debounce
    # (the draw path never rebuilds this mode/source), and the checksum
    # covers seams AND UVs — so a seam edit and a UV edit each cost one
    # debounced rebuild, never a draw rebuild racing a debounced one.
    overlay.refresh(bpy.context)
    overlay._state.debounce.reset()
    check("convergence baseline: 6 islands", overlay.island_count() == 6)

    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.mark_seam(clear=True)          # real seam change
    check("seam edit routed to the debounce, NOT the dirty/draw path",
          overlay._state.debounce.pending and not overlay._state.dirty)
    coords_before = overlay._state.coords
    overlay._draw()                              # must not rebuild
    check("draw during the pending burst keeps the cached geometry "
          "(no draw-path rebuild in COMBINED+SEAM)",
          overlay._state.coords is coords_before)
    t0 = 1000.0
    overlay._state.debounce.note_change(t0)
    r = overlay._live_tick(t0 + 0.15)            # quiet not yet reached
    check("tick during the burst polls without rebuilding",
          r == overlay.LIVE_POLL_S
          and overlay._state.coords is coords_before)
    r = overlay._live_tick(t0 + 0.50)            # quiet period elapsed
    check("ONE debounced rebuild after the burst: seams cleared -> 1 "
          "island", r is None
          and overlay._state.coords is not coords_before
          and overlay.island_count() == 1,
          "got %d" % overlay.island_count())
    coords_after = overlay._state.coords
    overlay._draw()
    check("no second rebuild via the draw path after the tick",
          overlay._state.coords is coords_after
          and not overlay._state.dirty)
    r = overlay._live_tick(t0 + 1.00)
    check("idle tick after the rebuild stops the timer without work",
          r is None and overlay._state.coords is coords_after)

    # UV edit: the OTHER invalidation concern, converging on the same
    # single debounced path (the checksum's UV bytes catch it).
    bpy.ops.object.mode_set(mode='OBJECT')
    overlay.refresh(bpy.context)                 # clean baseline
    overlay._state.debounce.reset()
    median_before = overlay.median_density()
    check("baseline median defined for the UV-edit check",
          median_before is not None and median_before > 0)
    layer = cube.data.uv_layers.active
    buf = [0.0] * (len(cube.data.loops) * 2)
    layer.data.foreach_get("uv", buf)
    layer.data.foreach_set("uv", [v * 2.0 for v in buf])
    cube.data.update_tag()
    bpy.context.view_layer.update()
    check("UV edit in COMBINED+SEAM routed to the debounce, NOT dirty",
          overlay._state.debounce.pending and not overlay._state.dirty)
    coords_before = overlay._state.coords
    overlay._state.debounce.note_change(3000.0)
    r = overlay._live_tick(3000.5)
    check("UV edit converges on one debounced rebuild (2x UVs -> 2x "
          "median; hue partition unchanged)",
          r is None and overlay._state.coords is not coords_before
          and overlay.median_density() == 2.0 * median_before
          and overlay.island_count() == 1,
          "median %r -> %r" % (median_before, overlay.median_density()))

    # No-op activity is stopped by the checksum gate (nothing changed).
    coords_after = overlay._state.coords
    overlay.note_activity(now=4000.0)
    r = overlay._live_tick(4000.5)
    check("no-op activity fires the debounce but skips the rebuild "
          "(UV-inclusive checksum unchanged)",
          r is None and overlay._state.coords is coords_after)

    # COMBINED+UV: the classic dirty -> rebuild-at-next-draw path, with
    # the debounce fully out of the picture (no double rebuild).
    wm.uv_island_overlay_source = 'UV'           # refresh; latch cleared
    overlay._state.debounce.reset()
    layer.data.foreach_get("uv", buf)
    layer.data.foreach_set("uv", [v * 0.5 for v in buf])
    cube.data.update_tag()
    bpy.context.view_layer.update()
    check("geometry update in COMBINED+UV marks dirty, debounce stays "
          "idle (classic path only)",
          overlay._state.dirty and not overlay._state.debounce.pending)
    coords_before = overlay._state.coords
    overlay._draw()
    check("draw-time rebuild consumed the dirty flag exactly once",
          not overlay._state.dirty
          and overlay._state.coords is not coords_before)
    check("headless draw then latched the gpu failure loudly (rebuild "
          "ran before the gpu section)",
          overlay.last_draw_error() is not None)
    r = overlay._live_tick(5000.0)
    check("live tick refuses COMBINED+UV (no debounced rebuild can "
          "race the draw path)", r is None)
    result = bpy.ops.uv.island_overlay_refresh()
    check("refresh clears the latch in COMBINED mode",
          result == {'FINISHED'} and overlay.last_draw_error() is None)
    wm.uv_island_overlay = False

    # --- no-UV mesh: hint + draw NOTHING (no islands-only degrade) -------
    nme = bpy.data.meshes.new("CombinedNoUV")
    nme.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                    [], [(0, 1, 2, 3)])
    nme.update()
    nobj = bpy.data.objects.new("CombinedNoUV", nme)
    bpy.context.collection.objects.link(nobj)
    bpy.context.view_layer.objects.active = nobj
    nobj.select_set(True)
    wm.uv_island_overlay_source = 'SEAM'
    wm.uv_island_overlay = True                  # mode is COMBINED
    check("COMBINED on a UV-less mesh: enabled, flagged no-UVs, 0 "
          "islands, NO geometry (no silent islands-only fallback)",
          overlay.is_enabled() and overlay.has_no_uvs()
          and overlay.active_mode() == 'COMBINED'
          and overlay.island_count() == 0
          and overlay._state.coords is None)
    check("no-UV state latched no error (a state, not a failure)",
          overlay.last_draw_error() is None)
    check("no-UV COMBINED+SEAM still stores a checksum (live gate keeps "
          "working while the user adds seams/UVs)",
          overlay._state.seam_checksum is not None)
    overlay._draw()
    check("draw with no UVs no-ops without touching the error latch",
          overlay.last_draw_error() is None)
    wm.uv_island_overlay_source = 'UV'
    check("UV island source on the UV-less mesh draws nothing either "
          "(both sources honor the no-UV contract)",
          overlay.has_no_uvs() and overlay._state.coords is None)
    wm.uv_island_overlay = False
    wm.uv_island_overlay_source = 'SEAM'

    # --- WM properties are session-only: the default flip needs no
    # migration. Save a file with mode ISLANDS, change the session value,
    # reopen the file: the saved value must NOT come back (background
    # loads keep the session WindowManager; WM values are not restored
    # from .blend userdata).
    wm.uv_island_overlay_mode = 'ISLANDS'
    probe_path = os.path.join(bpy.app.tempdir,
                              "uv_island_overlay_combined_probe.blend")
    bpy.ops.wm.save_as_mainfile(filepath=probe_path)
    wm.uv_island_overlay_mode = 'DENSITY'
    bpy.ops.wm.open_mainfile(filepath=probe_path)
    wm2 = bpy.context.window_manager
    check("WM mode value is NOT restored from the .blend (runtime-only "
          "property -> default flip needs no migration)",
          getattr(wm2, "uv_island_overlay_mode", 'COMBINED') != 'ISLANDS',
          "got %r" % getattr(wm2, "uv_island_overlay_mode", None))

    uv_island_overlay.unregister()
    check("unregister clean after the combined run", True)


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("COMBINED_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("COMBINED_TESTS_PASSED")
sys.stdout.flush()
