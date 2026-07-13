# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless registration/lifecycle test: register, verify operators +
scene-level settings + panel + menu entries + soft-integration probes,
prove scene props survive save/reopen (the Scene-vs-WindowManager
rationale), unregister cleanly, survive a re-register cycle.

Prints REGISTER_TESTS_PASSED on success.
"""

import inspect
import os
import sys
import tempfile
import traceback

import bpy

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


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    import kiln

    # --- bl_info ------------------------------------------------------------
    check("bl_info name/author/version",
          kiln.bl_info.get("name") == "Kiln"
          and kiln.bl_info.get("author") == "Teo Asinari"
          and kiln.bl_info.get("version") == (1, 1, 1))

    # --- register -------------------------------------------------------------
    kiln.register()

    for op in ("kiln_create_lowpoly", "kiln_bake",
               "kiln_apply_scale", "kiln_recalc_outside",
               "kiln_cage_preview", "kiln_cage_refresh",
               "kiln_cage_paint"):
        check("operator object.%s registered" % op,
              hasattr(bpy.types, "OBJECT_OT_%s" % op)
              and getattr(bpy.ops.object, op).idname_py()
              == "object.%s" % op)

    check("scene settings pointer registered",
          hasattr(bpy.context.scene, "kiln"))
    s = bpy.context.scene.kiln
    props = s.bl_rna.properties
    check("defaults: resolution 2K, margin 16, target 5000, auto "
          "distances on, wiring on",
          props["resolution"].default == '2K'
          and props["margin"].default == 16
          and props["target_faces"].default == 5000
          and props["use_auto_distances"].default is True
          and props["use_explicit_cage"].default is False
          and props["use_painted_cage"].default is False
          and props["wire_normal_map"].default is True)
    check("bake type enum is NORMAL-only for now, default NORMAL",
          {i.identifier for i in props["bake_type"].enum_items}
          == {'NORMAL'} and props["bake_type"].default == 'NORMAL')
    check("low-poly source enum EXISTING/GENERATE, default EXISTING "
          "(stage 1 shows picker or generator, never both)",
          {i.identifier for i in props["low_source"].enum_items}
          == {'EXISTING', 'GENERATE'}
          and props["low_source"].default == 'EXISTING'
          and s.low_source == 'EXISTING')
    check("high/low are Object pointers",
          props["high_object"].type == 'POINTER'
          and props["low_object"].type == 'POINTER'
          and props["high_object"].fixed_type.identifier == 'Object')

    # The pointer poll filters the UI dropdown (direct Python
    # assignment bypasses poll by design — the bake operator's own
    # validation covers that path); assert the poll logic itself.
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    bpy.context.collection.objects.link(cam)
    bpy.ops.mesh.primitive_cube_add()
    mesh_ob = bpy.context.active_object
    check("mesh-only poll rejects a camera, accepts a mesh",
          kiln._mesh_object_poll(s, cam) is False
          and kiln._mesh_object_poll(s, mesh_ob) is True)

    # --- panel + menu discoverability --------------------------------------------
    check("N-panel registered in its own 'Kiln' tab",
          hasattr(bpy.types, "VIEW3D_PT_kiln")
          and bpy.types.VIEW3D_PT_kiln.bl_category == "Kiln"
          and bpy.types.VIEW3D_PT_kiln.bl_space_type == 'VIEW_3D'
          and bpy.types.VIEW3D_PT_kiln.bl_region_type == 'UI')

    def menu_has_entry(menu):
        try:
            return any(getattr(f, "__name__", "") == "_menu_draw"
                       for f in menu._dyn_ui_initialize())
        except Exception:
            return False
    check("Object menu has the F3-searchable entries",
          menu_has_entry(bpy.types.VIEW3D_MT_object))

    # Menu entries appear without the panel's stage context, so their
    # text must carry the "Kiln: " prefix (F3 menu search matches
    # substrings, so "Bake Normal Map" still finds the prefixed entry).
    # Drive _menu_draw with a recording layout stub.
    class _RecordingLayout:
        def __init__(self):
            self.entries = []

        def separator(self):
            pass

        def operator(self, idname, text="", **kw):
            self.entries.append((idname, text))

    class _FakeMenu:
        layout = _RecordingLayout()

    fake = _FakeMenu()
    kiln._menu_draw(fake, bpy.context)
    labels = dict(fake.layout.entries)
    check("menu entries are prefixed 'Kiln: ' (context-free menu "
          "text; panel buttons stay short)",
          labels.get("object.kiln_create_lowpoly")
          == "Kiln: Create Low-Poly Candidate"
          and labels.get("object.kiln_bake")
          == "Kiln: Bake Normal Map",
          "got %r" % (fake.layout.entries,))

    # --- soft integration probes ------------------------------------------------------
    # Probed on 5.1.2: hasattr(bpy.ops.mesh, "<anything>") is ALWAYS
    # True (lazy stub), so availability is probed via the registered
    # operator TYPE instead. With the siblings absent the probe must
    # return False without raising.
    check("sibling probes are False when the add-ons are absent "
          "(and do not explode)",
          kiln.sibling_op_available(kiln.SEAM_TOOL_OT_TYPE)
          is False
          and kiln.sibling_op_available(kiln.OVERLAY_OT_TYPE)
          is False)

    class MESH_OT_seam_path_interactive(bpy.types.Operator):
        """Stand-in with the sibling's exact idname."""
        bl_idname = "mesh.seam_path_interactive"
        bl_label = "Fake Seam Path"

        def execute(self, context):
            return {'FINISHED'}

    bpy.utils.register_class(MESH_OT_seam_path_interactive)
    try:
        check("probe turns True when the sibling operator registers",
              kiln.sibling_op_available(kiln.SEAM_TOOL_OT_TYPE)
              is True)
    finally:
        bpy.utils.unregister_class(MESH_OT_seam_path_interactive)
    check("probe returns to False after the sibling unregisters",
          kiln.sibling_op_available(kiln.SEAM_TOOL_OT_TYPE)
          is False)

    draw_src = inspect.getsource(bpy.types.VIEW3D_PT_kiln.draw)
    check("panel draw gates the sibling buttons on the probe (no "
          "cross-imports)",
          "sibling_op_available" in draw_src
          and "SEAM_TOOL_OT_TYPE" in draw_src
          and "OVERLAY_OT_TYPE" in draw_src)
    check("panel draw branches stage 1 on low_source (picker in "
          "EXISTING, generator in GENERATE - never both)",
          "low_source" in draw_src
          and "'EXISTING'" in draw_src
          and "Generate from High (QuadriFlow)" in draw_src)
    pkg_src = open(os.path.join(_ADDON_DIR, "__init__.py")).read()
    check("no cross-imports of the sibling packages",
          "import seam_path_tool" not in pkg_src
          and "import uv_island_overlay" not in pkg_src)

    # --- scene props survive save/reopen (the reason they are Scene-level) -------------
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.name = "PersistLow"
    s.low_object = cube
    s.margin = 33
    s.output_path = "//maps/"
    blend = os.path.join(tempfile.gettempdir(), "kiln_persist.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    s2 = bpy.context.scene.kiln
    check("scene settings survive save/reopen (margin, path)",
          s2.margin == 33 and s2.output_path == "//maps/",
          "got %r %r" % (s2.margin, s2.output_path))
    check("object pointer survives save/reopen",
          s2.low_object is not None
          and s2.low_object.name == "PersistLow")

    # --- unregister ----------------------------------------------------------------------
    kiln.unregister()
    check("operators unregistered",
          not hasattr(bpy.types, "OBJECT_OT_kiln_bake"))
    check("scene property removed",
          "kiln" not in bpy.types.Scene.bl_rna.properties)
    check("panel unregistered",
          not hasattr(bpy.types, "VIEW3D_PT_kiln"))
    check("menu entry removed",
          not menu_has_entry(bpy.types.VIEW3D_MT_object))

    # --- re-register cycle ------------------------------------------------------------------
    kiln.register()
    check("re-register restores the operators and settings",
          hasattr(bpy.types, "OBJECT_OT_kiln_bake")
          and hasattr(bpy.context.scene, "kiln"))
    kiln.unregister()
    kiln.register()
    kiln.unregister()
    check("register/unregister cycle clean", True)


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("REGISTER_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("REGISTER_TESTS_PASSED")
sys.stdout.flush()
