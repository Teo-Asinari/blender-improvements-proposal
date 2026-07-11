# SPDX-License-Identifier: GPL-2.0-or-later
"""EZ-Bake — guided high-poly -> low-poly normal-baking workflow.

One sidebar panel walks the pipeline as three sequential stages:

1. Pair — pick the high-poly sculpt and the low-poly target (or
   generate a starting-point low-poly with QuadriFlow/Decimate).
2. Seams & UVs — a readiness checklist for the low-poly (UV layer,
   non-degenerate layout, applied scale, no mirrored transform), with
   soft-integration buttons for the sibling seam/overlay add-ons when
   they are installed (no hard dependency, no cross-imports).
3. Bake — one button that runs the whole high->low tangent-space
   normal bake and saves the PNG, replacing Blender's ~15-step manual
   setup across three editors.

Settings live in a SCENE-level PropertyGroup (bpy.types.Scene.ez_bake)
— unlike the sibling overlay add-on's WindowManager properties, these
must survive save/load: the high/low pair and bake settings are part of
the asset, not of the UI session (WM properties are runtime-only and
reset every session, which is right for a viewport toggle and wrong for
a bake configuration).
"""

bl_info = {
    "name": "EZ-Bake",
    "author": "Teo Asinari",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > EZ-Bake tab",
    "description": "Guided high-poly to low-poly normal baking: pair "
                   "setup, UV readiness checklist, one-button Cycles "
                   "normal bake with saving and material wiring",
    "category": "Render",
}

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)

if "flowcore" in locals():
    import importlib
    flowcore = importlib.reload(flowcore)
    readiness = importlib.reload(readiness)
    retopo = importlib.reload(retopo)
    baking = importlib.reload(baking)
else:
    from . import flowcore
    from . import readiness
    from . import retopo
    from . import baking


# ---------------------------------------------------------------------------
# Soft integration with the sibling add-ons (no hard dependency)
#
# Probed at draw time. NOTE: hasattr(bpy.ops.mesh, "any_name") is
# useless as a probe — on 5.1.2 bpy.ops attribute access lazily returns
# a stub for ANY name, so it is True even when nothing is registered.
# The registered operator TYPE on bpy.types is the reliable signal.
# ---------------------------------------------------------------------------

SEAM_TOOL_OT_TYPE = "MESH_OT_seam_path_interactive"    # Seam Path Tool
OVERLAY_OT_TYPE = "UV_OT_island_overlay_toggle"        # UV Island Overlay


def sibling_op_available(op_type_name):
    """True when the operator class is registered (add-on installed
    and enabled). Never raises — absent siblings simply hide their
    convenience buttons."""
    try:
        return hasattr(bpy.types, op_type_name)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Settings (scene-level; see the module docstring for the WM-vs-Scene
# rationale)
# ---------------------------------------------------------------------------

def _mesh_object_poll(self, obj):
    return obj.type == 'MESH'


class EZBakeSettings(bpy.types.PropertyGroup):
    high_object: PointerProperty(
        name="High-Poly",
        description="Source of detail: the dense sculpt to bake FROM",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    low_object: PointerProperty(
        name="Low-Poly",
        description="Bake target: the retopologized mesh with UVs to "
                    "bake ONTO",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    target_faces: IntProperty(
        name="Target Faces",
        description="Approximate face count for the generated low-poly "
                    "candidate (QuadriFlow remesh; Decimate fallback)",
        default=5000, min=50, soft_max=200000,
    )
    resolution: EnumProperty(
        name="Resolution",
        description="Square bake image size",
        items=(
            ('1K', "1024", "1024 x 1024"),
            ('2K', "2048", "2048 x 2048"),
            ('4K', "4096", "4096 x 4096"),
        ),
        default='2K',
    )
    margin: IntProperty(
        name="Margin",
        description="Bake margin in pixels (bleed past UV island "
                    "borders, hides seams at mip levels)",
        default=16, min=0, soft_max=64,
    )
    use_auto_distances: BoolProperty(
        name="Auto Distances",
        description="Derive cage extrusion (2%) and max ray distance "
                    "(4%) from the pair's combined bounding-box "
                    "diagonal (see README); disable to set both "
                    "manually",
        default=True,
    )
    cage_extrusion: FloatProperty(
        name="Extrusion",
        description="Inflate the low-poly cage by this distance before "
                    "casting rays toward the high-poly",
        default=0.05, min=0.0, soft_max=10.0, subtype='DISTANCE',
    )
    max_ray_distance: FloatProperty(
        name="Max Ray Distance",
        description="Furthest a cage ray may travel to hit the "
                    "high-poly (0 = unlimited, grabs far surfaces - "
                    "usually wrong)",
        default=0.1, min=0.0, soft_max=10.0, subtype='DISTANCE',
    )
    output_path: StringProperty(
        name="Output Path",
        description="Where the baked PNG is saved. Empty = "
                    "//textures/<lowpoly>_normal.png next to the saved "
                    ".blend (directories are created). Accepts "
                    "absolute and //-relative paths; a trailing slash "
                    "means 'this directory, default file name'",
        default="", subtype='FILE_PATH',
        # Without this flag, assigning a "//" path raises a
        # RuntimeWarning on Blender 5.x.
        options={'PATH_SUPPORTS_BLEND_RELATIVE'},
    )
    bake_type: EnumProperty(
        name="Bake Type",
        description="What to bake (the machinery — image naming, "
                    "colorspace, node wiring, operator settings — "
                    "switches on this)",
        items=(
            ('NORMAL', "Normal", "Tangent-space normal map"),
            # TODO: AO, cavity/curvature, displacement — same
            # machinery: add items here + entries in
            # flowcore.BAKE_TYPES + a branch in baking._bake_kwargs.
        ),
        default='NORMAL',
    )
    wire_normal_map: BoolProperty(
        name="Wire Into Material",
        description="After baking, connect Image Texture -> Normal Map "
                    "-> Principled BSDF Normal in the low-poly's "
                    "material",
        default=True,
    )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class OBJECT_OT_ez_bake_create_lowpoly(bpy.types.Operator):
    """Duplicate the high-poly and remesh it to the target face count
    (QuadriFlow, or Decimate when QuadriFlow fails) — a starting point
    for retopo, not animation-grade topology"""
    bl_idname = "object.ez_bake_create_lowpoly"
    bl_label = "Create Low-Poly Candidate"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "ez_bake", None)
        return (s is not None and s.high_object is not None
                and s.high_object.type == 'MESH')

    def execute(self, context):
        s = context.scene.ez_bake
        try:
            low, method, detail = retopo.create_lowpoly_candidate(
                context, s.high_object, s.target_faces)
        except retopo.RetopoError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        s.low_object = low
        readiness.invalidate()
        if method == 'QUADRIFLOW':
            self.report({'INFO'},
                        "QuadriFlow candidate %r: %d faces (target %d). "
                        "Next: mark seams and unwrap (stage 2)"
                        % (low.name, len(low.data.polygons),
                           s.target_faces))
        else:
            self.report({'WARNING'},
                        "%s - used Decimate fallback instead: %r, %d "
                        "faces (target %d). Next: mark seams and unwrap"
                        % (detail, low.name, len(low.data.polygons),
                           s.target_faces))
        return {'FINISHED'}


class OBJECT_OT_ez_bake_bake(bpy.types.Operator):
    """Bake the high-poly's normals onto the low-poly's UV layout with
    Cycles, save the PNG and (optionally) wire it into the material.
    Blocks the UI while baking (synchronous) — see the README"""
    bl_idname = "object.ez_bake_bake"
    bl_label = "Bake Normal Map"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "ez_bake", None)
        return (s is not None and s.high_object is not None
                and s.low_object is not None)

    def execute(self, context):
        s = context.scene.ez_bake
        try:
            info = baking.run_bake(context, s, self.report)
        except baking.EZBakeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        readiness.invalidate()
        self.report({'INFO'},
                    "Baked %dpx %s map to %s%s (extrusion %.4g, max "
                    "ray %.4g)"
                    % (info["resolution"],
                       flowcore.BAKE_TYPES[s.bake_type]["label"].lower(),
                       info["path"],
                       ", wired into material" if info["wired"] else "",
                       info["extrusion"], info["max_ray_distance"]))
        return {'FINISHED'}


class OBJECT_OT_ez_bake_apply_scale(bpy.types.Operator):
    """Apply the low-poly object's scale (checklist fix: non-unit
    scale distorts the bake cage and tangent basis)"""
    bl_idname = "object.ez_bake_apply_scale"
    bl_label = "Apply Scale (Low-Poly)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "ez_bake", None)
        return s is not None and s.low_object is not None

    def execute(self, context):
        low = context.scene.ez_bake.low_object
        prev_selected = [ob.name for ob in context.selected_objects]
        prev_active = context.view_layer.objects.active
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            for ob in context.selected_objects:
                ob.select_set(False)
            low.select_set(True)
            context.view_layer.objects.active = low
            bpy.ops.object.transform_apply(
                location=False, rotation=False, scale=True)
        except RuntimeError as exc:
            self.report({'ERROR'}, "Apply Scale failed: %s"
                        % str(exc).strip().splitlines()[-1])
            return {'CANCELLED'}
        finally:
            for ob in context.selected_objects:
                ob.select_set(False)
            for name in prev_selected:
                ob = context.view_layer.objects.get(name)
                if ob is not None:
                    ob.select_set(True)
            if prev_active is not None:
                context.view_layer.objects.active = prev_active
            readiness.invalidate()
        self.report({'INFO'}, "Scale applied on %r" % low.name)
        return {'FINISHED'}


class OBJECT_OT_ez_bake_recalc_outside(bpy.types.Operator):
    """Recalculate the low-poly's normals to point outside (checklist
    fix for mirrored/flipped normals)"""
    bl_idname = "object.ez_bake_recalc_outside"
    bl_label = "Recalculate Outside (Low-Poly)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "ez_bake", None)
        return s is not None and s.low_object is not None

    def execute(self, context):
        low = context.scene.ez_bake.low_object
        prev_active = context.view_layer.objects.active
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            context.view_layer.objects.active = low
            low.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.normals_make_consistent(inside=False)
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError as exc:
            self.report({'ERROR'}, "Recalculate Outside failed: %s"
                        % str(exc).strip().splitlines()[-1])
            return {'CANCELLED'}
        finally:
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            if prev_active is not None:
                context.view_layer.objects.active = prev_active
            readiness.invalidate()
        self.report({'INFO'}, "Normals recalculated outside on %r"
                    % low.name)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI: one N-panel tab presenting the pipeline as sequential stages.
# Discoverability lesson from the sibling add-ons: a sidebar panel plus
# F3-searchable operators with menu entries — never popover-only.
# ---------------------------------------------------------------------------

_STATE_ICONS = {
    readiness.OK: 'CHECKMARK',
    readiness.WARN: 'ERROR',      # Blender's warning-triangle icon
    readiness.FAIL: 'CANCEL',
}


def _pair_ready(s):
    return (s.high_object is not None and s.low_object is not None
            and s.high_object.type == 'MESH'
            and s.low_object.type == 'MESH'
            and s.high_object != s.low_object)


class VIEW3D_PT_ez_bake(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_ez_bake"
    bl_label = "EZ-Bake"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "EZ-Bake"

    def draw(self, context):
        layout = self.layout
        s = context.scene.ez_bake
        pair_ok = _pair_ready(s)

        # --- Stage 1: pair ------------------------------------------------
        box = layout.box()
        box.label(text="1  High / Low Pair",
                  icon='CHECKMARK' if pair_ok else 'RADIOBUT_OFF')
        box.prop(s, "high_object")
        box.prop(s, "low_object")
        col = box.column(align=True)
        col.prop(s, "target_faces")
        col.operator(OBJECT_OT_ez_bake_create_lowpoly.bl_idname,
                     icon='MOD_REMESH')
        if s.high_object == s.low_object and s.high_object is not None:
            box.label(text="High and low are the same object",
                      icon='ERROR')

        # --- Stage 2: seams & UVs readiness --------------------------------
        box = layout.box()
        if not pair_ok:
            box.label(text="2  Seams && UVs", icon='RADIOBUT_OFF')
            box.label(text="Pick both objects above", icon='INFO')
        else:
            items = readiness.evaluate_cached(s.low_object)
            have_fail = any(i.state == readiness.FAIL for i in items)
            have_warn = any(i.state == readiness.WARN for i in items)
            box.label(text="2  Seams && UVs",
                      icon='CANCEL' if have_fail
                      else ('ERROR' if have_warn else 'CHECKMARK'))
            col = box.column(align=True)
            for item in items:
                col.label(text=item.label,
                          icon=_STATE_ICONS[item.state])
            fixes = box.column(align=True)
            states = {i.key: i.state for i in items}
            if states.get("scale") == readiness.WARN:
                fixes.operator(
                    OBJECT_OT_ez_bake_apply_scale.bl_idname,
                    icon='CON_SIZELIKE')
            if states.get("normals") == readiness.WARN:
                fixes.operator(
                    OBJECT_OT_ez_bake_recalc_outside.bl_idname,
                    icon='NORMALS_FACE')
            # Sibling conveniences: shown only when installed; absent
            # siblings show nothing (and never error).
            sib = box.column(align=True)
            if sibling_op_available(SEAM_TOOL_OT_TYPE):
                sib.operator("mesh.seam_path_interactive",
                             text="Mark Seams (interactive)",
                             icon='MOD_EDGESPLIT')
            if sibling_op_available(OVERLAY_OT_TYPE):
                sib.operator("uv.island_overlay_toggle",
                             text="Toggle Island/Density Overlay",
                             icon='UV_ISLANDSEL')

        # --- Stage 3: bake ---------------------------------------------------
        box = layout.box()
        box.label(text="3  Bake",
                  icon='RENDER_STILL' if pair_ok else 'RADIOBUT_OFF')
        col = box.column(align=True)
        col.prop(s, "bake_type")
        col.prop(s, "resolution")
        col.prop(s, "margin")
        col.prop(s, "use_auto_distances")
        if s.use_auto_distances:
            if pair_ok:
                ext, ray = flowcore.auto_distances(
                    readiness.pair_diagonal(s.high_object, s.low_object))
                col.label(text="Auto: extrusion %.4g, ray %.4g"
                          % (ext, ray))
        else:
            col.prop(s, "cage_extrusion")
            col.prop(s, "max_ray_distance")
        col.prop(s, "output_path")
        col.prop(s, "wire_normal_map")
        row = box.row()
        row.scale_y = 1.4
        row.operator(OBJECT_OT_ez_bake_bake.bl_idname,
                     icon='RENDER_STILL')


def _menu_draw(self, context):
    """Object menu entries so F3 search finds the operators."""
    self.layout.separator()
    self.layout.operator(OBJECT_OT_ez_bake_create_lowpoly.bl_idname)
    self.layout.operator(OBJECT_OT_ez_bake_bake.bl_idname)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    EZBakeSettings,
    OBJECT_OT_ez_bake_create_lowpoly,
    OBJECT_OT_ez_bake_bake,
    OBJECT_OT_ez_bake_apply_scale,
    OBJECT_OT_ez_bake_recalc_outside,
    VIEW3D_PT_ez_bake,
)

_MENUS = ("VIEW3D_MT_object",)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ez_bake = PointerProperty(type=EZBakeSettings)
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            menu.append(_menu_draw)


def unregister():
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            try:
                menu.remove(_menu_draw)
            except Exception:
                pass
    del bpy.types.Scene.ez_bake
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
