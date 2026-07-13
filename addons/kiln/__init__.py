# SPDX-License-Identifier: GPL-2.0-or-later
"""Kiln — guided high-poly -> low-poly normal-baking workflow.

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

Settings live in a SCENE-level PropertyGroup (bpy.types.Scene.kiln)
— unlike the sibling overlay add-on's WindowManager properties, these
must survive save/load: the high/low pair and bake settings are part of
the asset, not of the UI session (WM properties are runtime-only and
reset every session, which is right for a viewport toggle and wrong for
a bake configuration).
"""

bl_info = {
    "name": "Kiln",
    "author": "Teo Asinari",
    "version": (1, 1, 1),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Kiln tab",
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
    cage = importlib.reload(cage)
    baking = importlib.reload(baking)
else:
    from . import flowcore
    from . import readiness
    from . import retopo
    from . import cage
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


class KilnSettings(bpy.types.PropertyGroup):
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
    low_source: EnumProperty(
        name="Low Poly",
        description="Where the low-poly bake target comes from. This "
                    "only switches what stage 1 shows - the Low-Poly "
                    "picker is always the source of truth for the bake",
        items=(
            ('EXISTING', "Existing",
             "Pick an already-retopologized mesh as the bake target"),
            ('GENERATE', "Generate",
             "Generate a starting-point candidate from the high-poly "
             "(QuadriFlow remesh; Decimate fallback). On success the "
             "result fills the Low-Poly picker and this switches back "
             "to Existing"),
        ),
        default='EXISTING',
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
    use_explicit_cage: BoolProperty(
        name="Bake With Visible Cage",
        description="Bake from the generated outer guide object so the "
                    "previewed shell is the cage Blender actually uses",
        default=False,
    )
    use_painted_cage: BoolProperty(
        name="Painted Outer Distance",
        description="Multiply outer extrusion per low-poly vertex from "
                    "the Kiln Cage Scale group: 0.5 = 1x, 0 = 0x, 1 = 2x",
        default=False,
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

class OBJECT_OT_kiln_create_lowpoly(bpy.types.Operator):
    """Duplicate the high-poly and remesh it to the target face count
    (QuadriFlow, or Decimate when QuadriFlow fails) — a starting point
    for retopo, not animation-grade topology"""
    bl_idname = "object.kiln_create_lowpoly"
    bl_label = "Create Low-Poly Candidate"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "kiln", None)
        return (s is not None and s.high_object is not None
                and s.high_object.type == 'MESH')

    def execute(self, context):
        s = context.scene.kiln
        try:
            low, method, detail = retopo.create_lowpoly_candidate(
                context, s.high_object, s.target_faces)
        except retopo.RetopoError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        s.low_object = low
        # Land the user on a self-evident state: switch stage 1 back to
        # the Existing view so the picker is visible and visibly filled
        # with the fresh candidate. Real RNA property assignment (never
        # an idprop write like s["low_source"] = ...).
        s.low_source = 'EXISTING'
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


class OBJECT_OT_kiln_bake(bpy.types.Operator):
    """Bake the high-poly's normals onto the low-poly's UV layout with
    Cycles, save the PNG and (optionally) wire it into the material.
    Blocks the UI while baking (synchronous) — see the README"""
    bl_idname = "object.kiln_bake"
    bl_label = "Bake Normal Map"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "kiln", None)
        return (s is not None and s.high_object is not None
                and s.low_object is not None)

    def execute(self, context):
        s = context.scene.kiln
        try:
            info = baking.run_bake(context, s, self.report)
        except baking.KilnError as exc:
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


class OBJECT_OT_kiln_cage_preview(bpy.types.Operator):
    """Show or hide cached wireframe inner/outer projection shells"""
    bl_idname = "object.kiln_cage_preview"
    bl_label = "Toggle Cage Guide"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "kiln", None)
        return s is not None and s.low_object is not None

    def execute(self, context):
        s = context.scene.kiln
        low = s.low_object
        if cage.guides_visible(low):
            cage.hide_guides(low)
            self.report({'INFO'}, "Kiln cage guide hidden")
            return {'FINISHED'}
        extrusion, max_ray = baking.resolved_distances(
            s, s.high_object, low)
        try:
            cage.build_guides(context, low, extrusion, max_ray,
                              s.use_painted_cage)
        except cage.CageError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Outer and inner projection shells shown")
        return {'FINISHED'}


class OBJECT_OT_kiln_cage_refresh(bpy.types.Operator):
    """Rebuild projection shells after distance or weight painting changes"""
    bl_idname = "object.kiln_cage_refresh"
    bl_label = "Refresh Cage Guide"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "kiln", None)
        return s is not None and s.low_object is not None

    def execute(self, context):
        s = context.scene.kiln
        extrusion, max_ray = baking.resolved_distances(
            s, s.high_object, s.low_object)
        try:
            cage.build_guides(context, s.low_object, extrusion, max_ray,
                              s.use_painted_cage)
        except cage.CageError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Kiln cage guide refreshed")
        return {'FINISHED'}


class OBJECT_OT_kiln_cage_paint(bpy.types.Operator):
    """Initialize the cage multiplier and enter Blender Weight Paint mode"""
    bl_idname = "object.kiln_cage_paint"
    bl_label = "Paint Outer Cage Distance"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "kiln", None)
        return s is not None and s.low_object is not None

    def execute(self, context):
        s = context.scene.kiln
        low = s.low_object
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.selected_objects:
            obj.select_set(False)
        low.hide_set(False)
        low.hide_select = False
        low.select_set(True)
        context.view_layer.objects.active = low
        cage.ensure_paint_group(low)
        cage.hide_guides(low)
        s.use_painted_cage = True
        try:
            bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
        except RuntimeError as exc:
            self.report({'ERROR'}, "Could not enter Weight Paint: %s"
                        % str(exc).strip().splitlines()[-1])
            return {'CANCELLED'}
        self.report({'INFO'}, "Paint Kiln Cage Scale: 0.5 = 1x; then "
                    "return to Object Mode and Refresh Cage Guide")
        return {'FINISHED'}


class OBJECT_OT_kiln_apply_scale(bpy.types.Operator):
    """Apply the low-poly object's scale (checklist fix: non-unit
    scale distorts the bake cage and tangent basis)"""
    bl_idname = "object.kiln_apply_scale"
    bl_label = "Apply Scale (Low-Poly)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "kiln", None)
        return s is not None and s.low_object is not None

    def execute(self, context):
        low = context.scene.kiln.low_object
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


class OBJECT_OT_kiln_recalc_outside(bpy.types.Operator):
    """Recalculate the low-poly's normals to point outside (checklist
    fix for mirrored/flipped normals)"""
    bl_idname = "object.kiln_recalc_outside"
    bl_label = "Recalculate Outside (Low-Poly)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "kiln", None)
        return s is not None and s.low_object is not None

    def execute(self, context):
        low = context.scene.kiln.low_object
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


class VIEW3D_PT_kiln(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_kiln"
    bl_label = "Kiln"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Kiln"

    def draw(self, context):
        layout = self.layout
        s = context.scene.kiln
        pair_ok = _pair_ready(s)

        # --- Stage 1: pair ------------------------------------------------
        box = layout.box()
        box.label(text="1  High / Low Pair",
                  icon='CHECKMARK' if pair_ok else 'RADIOBUT_OFF')
        box.prop(s, "high_object")
        # Low-poly source switch: showing the picker AND the generator
        # side by side read as contradictory ("if this creates the
        # retopo mesh, why is there a selector?"), so stage 1 shows one
        # or the other. The picker stays the source of truth either
        # way: generating just fills it and switches back to Existing.
        row = box.row(align=True)
        row.label(text="Low Poly:")
        row.prop(s, "low_source", expand=True)
        if s.low_source == 'EXISTING':
            box.prop(s, "low_object")
        else:
            col = box.column(align=True)
            col.prop(s, "target_faces")
            col.operator(OBJECT_OT_kiln_create_lowpoly.bl_idname,
                         text="Generate from High (QuadriFlow)",
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
                    OBJECT_OT_kiln_apply_scale.bl_idname,
                    icon='CON_SIZELIKE')
            if states.get("normals") == readiness.WARN:
                fixes.operator(
                    OBJECT_OT_kiln_recalc_outside.bl_idname,
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
        if pair_ok:
            ext, ray = baking.resolved_distances(
                s, s.high_object, s.low_object)
            col.label(text="Inner reach: %.4g" % cage.inner_reach(ext, ray))
        guide = box.box()
        guide.label(text="Projection Shell Guide", icon='MOD_WIREFRAME')
        row = guide.row(align=True)
        row.operator(OBJECT_OT_kiln_cage_preview.bl_idname,
                     text="Hide Shells" if (s.low_object is not None
                                            and cage.guides_visible(
                                                s.low_object))
                     else "Show Shells",
                     icon='HIDE_ON' if (s.low_object is not None
                                       and cage.guides_visible(s.low_object))
                     else 'HIDE_OFF')
        row.operator(OBJECT_OT_kiln_cage_refresh.bl_idname,
                     text="Refresh", icon='FILE_REFRESH')
        guide.prop(s, "use_explicit_cage")
        guide.prop(s, "use_painted_cage")
        guide.operator(OBJECT_OT_kiln_cage_paint.bl_idname,
                       icon='BRUSH_DATA')
        guide.label(text="Paint: 0.5 = 1x, blue = 0x, red = 2x",
                    icon='INFO')
        col.prop(s, "output_path")
        col.prop(s, "wire_normal_map")
        row = box.row()
        row.scale_y = 1.4
        row.operator(OBJECT_OT_kiln_bake.bl_idname,
                     icon='RENDER_STILL')


def _menu_draw(self, context):
    """Object menu entries so F3 search finds the operators. The menu
    text carries an "Kiln: " prefix because these entries appear
    without the panel's stage context (a bare "Bake Normal Map" in the
    Object menu doesn't say whose); the panel buttons stay short."""
    self.layout.separator()
    self.layout.operator(OBJECT_OT_kiln_create_lowpoly.bl_idname,
                         text="Kiln: Create Low-Poly Candidate")
    self.layout.operator(OBJECT_OT_kiln_bake.bl_idname,
                         text="Kiln: Bake Normal Map")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    KilnSettings,
    OBJECT_OT_kiln_create_lowpoly,
    OBJECT_OT_kiln_bake,
    OBJECT_OT_kiln_cage_preview,
    OBJECT_OT_kiln_cage_refresh,
    OBJECT_OT_kiln_cage_paint,
    OBJECT_OT_kiln_apply_scale,
    OBJECT_OT_kiln_recalc_outside,
    VIEW3D_PT_kiln,
)

_MENUS = ("VIEW3D_MT_object",)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.kiln = PointerProperty(type=KilnSettings)
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            menu.append(_menu_draw)


def unregister():
    cage.remove_all_guides()
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            try:
                menu.remove(_menu_draw)
            except Exception:
                pass
    del bpy.types.Scene.kiln
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
