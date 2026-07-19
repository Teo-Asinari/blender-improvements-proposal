# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto UI: N-panel ("Impasto" tab, 3D Viewport), layer UIList, and
menu entries (Object menu) so every operator is reachable via panel,
menu, and F3 search. Standard Blender widgets, single-purpose rows —
deliberately minimal for phase 1 (UX iterates with the user)."""

import bpy

from . import bl_info as _BL_INFO
from . import engine
from . import gpu_engine
from . import model
from . import ops

_TYPE_ICONS = {'PAINT': 'BRUSH_DATA', 'FILL': 'SNAP_FACE',
               'GROUP': 'FILE_FOLDER'}
_CORE_CHANNEL_BADGES = {
    'base_color': 'B', 'metallic': 'M', 'roughness': 'R',
    'normal': 'N', 'height': 'H', 'alpha': 'A',
}
_VERSION_LABEL = "Impasto %s" % ".".join(
    str(part) for part in _BL_INFO["version"])


def _layer_channel_summary(keys):
    """Compact layer-list summary; expanded controls carry exact names."""
    ordered = [key for key in sorted(
        keys, key=lambda key: model.CHANNEL_ORDER.get(key, 99))
        if key in model.CHANNEL_MAP]
    parts = ["".join(_CORE_CHANNEL_BADGES[key] for key in ordered
                     if key in _CORE_CHANNEL_BADGES)]
    emission_count = sum(model.CHANNEL_MAP[key].panel_group == 'Emission'
                         for key in ordered)
    subsurface_count = sum(model.CHANNEL_MAP[key].panel_group == 'Subsurface'
                           for key in ordered)
    if emission_count:
        parts.append("E(%d)" % emission_count)
    if subsurface_count:
        parts.append("SS(%d)" % subsurface_count)
    return " ".join(part for part in parts if part)


class IMPASTO_UL_layers(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname):
        row = layout.row(align=True)
        row.prop(item, "visible", text="", emboss=False,
                 icon='HIDE_OFF' if item.visible else 'HIDE_ON')
        row.label(icon=_TYPE_ICONS.get(item.layer_type, 'BLANK1'))
        row.prop(item, "label", text="", emboss=False)
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        keys = [b.name for b in item.bindings if b.enabled]
        if keys:
            sub.label(text=_layer_channel_summary(keys))


def _missing_channels(context, bind_active_layer):
    obj = context.object
    mat = obj.active_material if obj is not None else None
    tree = engine.find_stack_for_material(mat)
    if tree is None:
        return None
    registered = {channel.name for channel in tree.impasto.channels}
    missing = [channel for channel in model.CHANNELS
               if channel.key not in registered]
    active_layer = tree.impasto.active_layer()
    if (bind_active_layer and active_layer is not None
            and active_layer.layer_type == 'PAINT'):
        paintable = set(gpu_engine.GPU_PAINT_CHANNEL_KEYS)
        missing = [channel for channel in missing
                   if channel.key in paintable]
    return missing


def _draw_missing_channels(layout, context, bind_active_layer):
    missing = _missing_channels(context, bind_active_layer)
    if missing is None:
        layout.label(text="No Impasto stack", icon='INFO')
        return
    if not missing:
        layout.label(text="All supported channels are registered", icon='CHECKMARK')
        return
    previous_group = None
    for channel in missing:
        if channel.panel_group != previous_group:
            if previous_group is not None:
                layout.separator()
            layout.label(text=channel.panel_group)
            previous_group = channel.panel_group
        op = layout.operator(ops.IMPASTO_OT_channel_add.bl_idname,
                             text=channel.label, icon='ADD')
        op.channel_key = channel.key
        op.bind_active_layer = bind_active_layer


class IMPASTO_MT_add_channel_register(bpy.types.Menu):
    bl_idname = "IMPASTO_MT_add_channel_register"
    bl_label = "Register Without Layer Binding"

    def draw(self, context):
        _draw_missing_channels(self.layout, context, False)


class IMPASTO_MT_add_channel(bpy.types.Menu):
    bl_idname = "IMPASTO_MT_add_channel"
    bl_label = "Add Material Channel"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Add to stack + selected layer", icon='LAYER_ACTIVE')
        _draw_missing_channels(layout, context, True)
        layout.separator()
        layout.menu(IMPASTO_MT_add_channel_register.bl_idname,
                    text="Register Without Layer Binding", icon='NODETREE')


class IMPASTO_PT_main(bpy.types.Panel):
    """Sidebar home for the layer stack (always visible in the Impasto
    tab so the feature is discoverable)"""
    bl_idname = "IMPASTO_PT_main"
    bl_label = "Layer Stack"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Impasto"

    def draw(self, context):
        layout = self.layout
        header = layout.row()
        header.alignment = 'RIGHT'
        header.label(text=_VERSION_LABEL, icon='BRUSH_DATA')
        obj = context.object
        if obj is None or obj.type != 'MESH':
            layout.label(text="Select a mesh object", icon='INFO')
            return
        mat = obj.active_material
        tree = engine.find_stack_for_material(mat)
        if tree is None:
            if mat is not None:
                layout.label(text="Material: %s" % mat.name)
            layout.operator_menu_enum(ops.IMPASTO_OT_stack_init.bl_idname,
                                      "template",
                                      text="New Layer Stack",
                                      icon='ADD')
            return

        state = tree.impasto
        row = layout.row(align=True)
        row.label(text="Stack: %s" % mat.name, icon='NODETREE')
        chan_labels = ", ".join(
            model.CHANNEL_MAP[c.name].label for c in state.channels
            if c.enabled and c.name in model.CHANNEL_MAP)
        layout.label(text=chan_labels, icon='MATERIAL')
        kiln_tex = (mat.node_tree.nodes.get("Kiln Bake Target")
                    if mat.use_nodes else None)
        if (kiln_tex is not None
                and kiln_tex.bl_idname == 'ShaderNodeTexImage'
                and kiln_tex.image is not None):
            layout.operator(
                ops.IMPASTO_OT_import_kiln_normal.bl_idname,
                text="Import / Repair Kiln Normal",
                icon='NORMALS_FACE')

        row = layout.row()
        row.template_list("IMPASTO_UL_layers", "", state, "layers",
                          state, "active_index", rows=4)
        col = row.column(align=True)
        op = col.operator(ops.IMPASTO_OT_layer_add.bl_idname, text="",
                          icon='BRUSH_DATA')
        op.layer_type = 'PAINT'
        op.channel_key = 'base_color'
        op = col.operator(ops.IMPASTO_OT_layer_add.bl_idname, text="",
                          icon='SNAP_FACE')
        op.layer_type = 'FILL'
        col.operator(ops.IMPASTO_OT_layer_remove.bl_idname, text="",
                     icon='TRASH')
        col.separator()
        op = col.operator(ops.IMPASTO_OT_layer_move.bl_idname, text="",
                          icon='TRIA_UP')
        op.direction = 'UP'
        op = col.operator(ops.IMPASTO_OT_layer_move.bl_idname, text="",
                          icon='TRIA_DOWN')
        op.direction = 'DOWN'

        layer = state.active_layer()
        if layer is not None:
            box = layout.box()
            box.label(text=layer.label,
                      icon=_TYPE_ICONS.get(layer.layer_type, 'BLANK1'))
            if layer.layer_type != 'GROUP':
                row = box.row(align=True)
                row.prop(layer, "blend_mode", text="")
                row.prop(layer, "opacity", text="Layer Opacity", slider=True)
                channels_box = box.box()
                row = channels_box.row(align=True)
                row.prop(layer, "ui_show_channels", text="Layer Channels",
                         icon='TRIA_DOWN' if layer.ui_show_channels
                         else 'TRIA_RIGHT', emboss=False)
                row.menu("IMPASTO_MT_add_channel", text="", icon='ADD')
                if layer.ui_show_channels:
                    self._draw_bindings(channels_box, state, layer)
                if layer.layer_type == 'PAINT':
                    self._draw_paint_tools(context, box, layer)
            else:
                box.prop(layer, "opacity", slider=True)

        layout.separator()
        layout.operator(ops.IMPASTO_OT_stack_rebuild.bl_idname,
                        text="Rebuild", icon='FILE_REFRESH')

    def _draw_paint_tools(self, context, box, layer):
        keys = [key for key, _image in ops.gpu_paint_targets(layer)]
        paint = box.box()
        row = paint.row(align=True)
        row.label(text="Brush Controls", icon='BRUSH_DATA')
        row.label(text="%d channel%s" %
                  (len(keys), "s" if len(keys) != 1 else ""))
        if not keys:
            paint.label(text="Enable a painted channel above", icon='INFO')
            return

        row = paint.row(align=True)
        row.label(text="Painting Engine")
        row.prop(layer, "paint_workflow", text="")
        if layer.paint_workflow == 'BLENDER':
            warning = paint.column(align=True)
            warning.alert = True
            warning.label(text="Prototype demo — fundamentally slow",
                          icon='ERROR')
            warning.label(text="Not intended for serious painting")
        values = paint.column(align=True)
        if 'base_color' in keys:
            values.prop(layer, "paint_color", text="Base Color")
        if 'roughness' in keys:
            values.prop(layer, "paint_roughness", text="Roughness", slider=True)
        if 'metallic' in keys:
            values.prop(layer, "paint_metallic", text="Metallic", slider=True)
        if 'normal' in keys:
            values.prop(layer, "paint_normal", text="Normal")
        if 'height' in keys:
            row = values.row(align=True)
            row.prop(layer, "paint_height_direction", expand=True)
            row.prop(layer, "paint_height_strength", text="Step")
        if 'emission_color' in keys or 'emission_strength' in keys:
            emission = paint.box()
            emission.label(text="Emission", icon='LIGHT')
            if 'emission_color' in keys:
                emission.prop(layer, "paint_emission_color", text="Color")
            if 'emission_strength' in keys:
                emission.prop(layer, "paint_emission_strength",
                              text="Strength")
        if any(k in keys for k in
               ('sss_weight', 'sss_radius', 'sss_scale')):
            subsurface = paint.box()
            subsurface.label(text="Subsurface", icon='SHADING_RENDERED')
            if 'sss_weight' in keys:
                subsurface.prop(layer, "paint_sss_weight", text="Weight",
                                slider=True)
            if 'sss_radius' in keys:
                subsurface.prop(layer, "paint_sss_radius", text="Radius RGB")
            if 'sss_scale' in keys:
                subsurface.prop(layer, "paint_sss_scale", text="Scale")
            subsurface.label(text="Weight = amount; Scale = travel distance",
                             icon='INFO')
            subsurface.label(text="Radius sets relative RGB travel",
                             icon='INFO')

        if layer.paint_workflow == 'GPU':
            row = paint.row(align=True)
            row.prop(layer, "brush_radius")
            row.prop(layer, "brush_hardness", slider=True)
            paint.prop(layer, "brush_opacity", slider=True)
            row = paint.row(align=True)
            row.label(text="Pressure")
            row.prop(layer, "brush_pressure_opacity", toggle=True)
            row.prop(layer, "brush_pressure_size", toggle=True)

        row = paint.row()
        row.scale_y = 1.35
        row.enabled = not gpu_engine.session_active()
        if layer.paint_workflow == 'GPU':
            row.operator(ops.IMPASTO_OT_gpu_paint.bl_idname,
                         text="Start GPU Painting", icon='BRUSH_DATA')
        else:
            row.operator(ops.IMPASTO_OT_native_multichannel_paint.bl_idname,
                         text="Start Blender Brush Replay", icon='TPAINT_HLT')

        row = paint.row(align=True)
        row.prop(layer, "ui_show_advanced", text="Advanced",
                 icon='TRIA_DOWN' if layer.ui_show_advanced
                 else 'TRIA_RIGHT', emboss=False)
        if layer.ui_show_advanced:
            self._draw_advanced_paint(paint, layer)
        if gpu_engine.session_active():
            self._draw_gpu_session(paint)
        if gpu_engine.last_error():
            paint.label(text="GPU paint failed — see console", icon='ERROR')

    def _draw_advanced_paint(self, box, layer):
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(layer, "gpu_preview_mode", text="Live Preview")
        row.popover(panel="IMPASTO_PT_preview_lighting", text="", icon='LIGHT')
        stencil_box = col.box()
        stencil_box.prop(layer, "brush_stencil_enabled")
        controls = stencil_box.column(align=True)
        controls.enabled = layer.brush_stencil_enabled
        self._draw_stencil_controls(controls, layer)
        row = col.row(align=True)
        row.prop(layer, "auto_material_preview", text="Idle Sync")
        delay = row.row(align=True)
        delay.enabled = layer.auto_material_preview
        delay.prop(layer, "auto_material_preview_delay", text="Delay")

    def _draw_stencil_controls(self, col, layer):
        """Present projection, source data, and effect as distinct choices."""
        col.template_ID(layer, "brush_stencil_image", open="image.open")

        col.separator()
        col.label(text="Placement", icon='VIEW_CAMERA')
        col.prop(layer, "brush_stencil_projection", expand=True)
        if layer.brush_stencil_projection == 'VIEW_STENCIL':
            col.label(text="Fixed camera-facing image", icon='INFO')
            col.prop(layer, "brush_stencil_position", text="Position")
            col.prop(layer, "brush_stencil_scale", text="Viewport Scale")
        else:
            col.label(text="Image follows every brush dab", icon='INFO')
            col.prop(layer, "brush_stencil_brush_scale", text="Brush Scale")
        col.prop(layer, "brush_stencil_rotation", text="Rotation")

        col.separator()
        col.label(text="Read Image From", icon='IMAGE_DATA')
        col.prop(layer, "brush_stencil_interpretation", expand=True)

        col.separator()
        col.label(text="Apply As", icon='MODIFIER')
        col.prop(layer, "brush_stencil_usage", expand=True)
        col.prop(layer, "brush_stencil_opacity", text="Stamp Opacity",
                 slider=True)
        if layer.brush_stencil_usage == 'NORMAL_PROFILE':
            col.prop(layer, "brush_stencil_profile_strength",
                     text="Relief Strength", slider=True)
            col.prop(layer, "brush_stencil_profile_invert",
                     text="Invert Relief")
            col.label(text="Grayscale gradients write Normal only",
                      icon='NORMALS_FACE')
        else:
            col.label(text="Modulates every enabled paint channel",
                      icon='INFO')

    def _draw_gpu_session(self, box):
        if gpu_engine.material_inspect_requested():
            box.label(text="Synchronizing material…", icon='FILE_REFRESH')
        elif gpu_engine.material_inspect_active():
            box.label(text="Inspecting Blender material", icon='SHADING_RENDERED')
        elif gpu_engine.input_paused():
            box.label(text="Painting paused — P to resume", icon='PAUSE')
        else:
            box.label(text="GPU painting active — P to pause", icon='REC')
        if (not gpu_engine.material_inspect_active()
                and not gpu_engine.material_inspect_requested()):
            row = box.row()
            row.enabled = not gpu_engine.stroke_active()
            row.operator(ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                         text="Inspect Material", icon='SHADING_RENDERED')
        elif gpu_engine.material_inspect_active():
            box.operator(ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                         text="Resume Painting", icon='PLAY')
        row = box.row()
        row.enabled = not gpu_engine.stroke_active()
        row.operator(ops.IMPASTO_OT_gpu_flush.bl_idname,
                     text="Flush for Save / Export", icon='FILE_REFRESH')

    def _draw_legacy_paint_tools(self, context, box, layer):
        bound = [b.name for b in layer.bindings
                 if b.enabled and b.mode == 'SHARED'
                 and b.name in model.CHANNEL_MAP]
        replay_keys = [key for key, _image in ops.gpu_paint_targets(layer)]
        image = bpy.data.images.get(layer.image_name)
        row = box.row(align=True)
        row.label(text=image.name if image else "Missing image",
                  icon='IMAGE_DATA' if image else 'ERROR')
        painting = (context.object is not None
                    and context.object.mode == 'TEXTURE_PAINT')
        paint_status = ("Texture Paint active"
                        if painting else "Object Mode")
        if len(replay_keys) > 1:
            paint_status += " — use multi-channel control below"
        elif not painting:
            paint_status += " — press Start Painting"
        box.label(text=paint_status,
                  icon='CHECKMARK' if painting else 'INFO')
        if 'normal' in bound:
            box.label(text="RGB normal: absolute direction", icon='INFO')
        if 'height' in bound:
            box.label(text="Height: repeated strokes accumulate",
                      icon='INFO')
            row = box.row(align=True)
            op = row.operator(ops.IMPASTO_OT_detail_paint.bl_idname,
                              text="Raise", icon='TRIA_UP')
            op.direction = 'RAISE'
            op = row.operator(ops.IMPASTO_OT_detail_paint.bl_idname,
                              text="Lower", icon='TRIA_DOWN')
            op.direction = 'LOWER'
        if len(replay_keys) <= 1 and (any(k != 'height' for k in bound)
                                     or not bound):
            row = box.row()
            row.scale_y = 1.25
            op = row.operator(ops.IMPASTO_OT_paint_activate.bl_idname,
                              text="Start Painting",
                              icon='TPAINT_HLT')
            op.channel_key = ""

        gpu_keys = replay_keys
        if gpu_keys:
            col = box.column(align=True)
            col.label(text="Shared Multi-Channel Values", icon='BRUSH_DATA')
            col.label(text="Used by Blender replay and GPU paint",
                      icon='INFO')
            col.label(text="Targets (%d): %s" % (
                len(gpu_keys), ", ".join(
                    model.CHANNEL_MAP[k].label for k in gpu_keys)))
            if len(gpu_keys) == 1:
                col.label(text="Add channels below for simultaneous paint",
                          icon='INFO')
            if 'base_color' in gpu_keys:
                col.prop(layer, "paint_color", text="Stroke Base Color")
            if 'roughness' in gpu_keys:
                col.prop(layer, "paint_roughness", text="Stroke Roughness",
                         slider=True)
            if 'metallic' in gpu_keys:
                col.prop(layer, "paint_metallic", text="Stroke Metallic",
                         slider=True)
            if 'normal' in gpu_keys:
                col.prop(layer, "paint_normal", text="Normal")
            if 'height' in gpu_keys:
                row = col.row(align=True)
                row.prop(layer, "paint_height_direction", expand=True)
                col.prop(layer, "paint_height_strength")
            if ('emission_color' in gpu_keys
                    or 'emission_strength' in gpu_keys):
                emission = col.box()
                emission.label(text="Emission", icon='LIGHT')
                if 'emission_color' in gpu_keys:
                    emission.prop(layer, "paint_emission_color",
                                  text="Color")
                if 'emission_strength' in gpu_keys:
                    emission.prop(layer, "paint_emission_strength",
                                  text="Strength")
                    emission.label(text="HDR strength may exceed 1",
                                   icon='INFO')
            if any(k in gpu_keys for k in
                   ('sss_weight', 'sss_radius', 'sss_scale')):
                subsurface = col.box()
                subsurface.label(text="Subsurface", icon='SHADING_RENDERED')
                if 'sss_weight' in gpu_keys:
                    subsurface.prop(layer, "paint_sss_weight",
                                    text="Weight", slider=True)
                if 'sss_radius' in gpu_keys:
                    subsurface.prop(layer, "paint_sss_radius",
                                    text="Radius RGB")
                if 'sss_scale' in gpu_keys:
                    subsurface.prop(layer, "paint_sss_scale", text="Scale")
                subsurface.label(
                    text="Weight = amount; Scale = travel distance",
                    icon='INFO')
                subsurface.label(
                    text="Radius sets relative RGB travel",
                    icon='INFO')
            row = box.row()
            row.scale_y = 1.25
            row.enabled = not gpu_engine.session_active()
            row.operator(
                ops.IMPASTO_OT_native_multichannel_paint.bl_idname,
                text=("Blender Brush → %d Channels" % len(gpu_keys)),
                icon='TPAINT_HLT')
            box.label(text="Uses the active Blender brush asset; applies at pen-up",
                      icon='INFO')

            box.separator()
            box.label(text="Experimental GPU Brush", icon='BRUSH_DATA')
            gpu_col = box.column(align=True)
            gpu_col.prop(layer, "gpu_preview_mode", text="Live Preview")
            gpu_col.label(text="Display only — painted channels are unchanged",
                          icon='INFO')
            row = gpu_col.row(align=True)
            row.prop(layer, "brush_radius")
            row.prop(layer, "brush_hardness", slider=True)
            gpu_col.prop(layer, "brush_opacity", slider=True)
            row = gpu_col.row(align=True)
            row.label(text="Pressure")
            row.prop(layer, "brush_pressure_opacity", toggle=True)
            row.prop(layer, "brush_pressure_size", toggle=True)
            stencil_box = gpu_col.box()
            stencil_box.prop(layer, "brush_stencil_enabled")
            stencil_controls = stencil_box.column(align=True)
            stencil_controls.enabled = layer.brush_stencil_enabled
            self._draw_stencil_controls(stencil_controls, layer)
            row = gpu_col.row(align=True)
            row.prop(layer, "auto_material_preview", text="Idle Material Sync")
            sub = row.row(align=True)
            sub.enabled = layer.auto_material_preview
            sub.prop(layer, "auto_material_preview_delay", text="Delay")
            if layer.auto_material_preview:
                gpu_col.label(text="Idle sync adds GPU readback; disable for "
                                   "lowest latency",
                              icon='ERROR')
            row = box.row()
            row.scale_y = 1.25
            row.enabled = not gpu_engine.session_active()
            row.operator(
                ops.IMPASTO_OT_gpu_paint.bl_idname,
                text=("GPU Paint %d Channel%s" %
                      (len(gpu_keys), "s" if len(gpu_keys) != 1 else "")),
                icon='BRUSH_DATA')
            if gpu_engine.session_active():
                box.label(text=("Syncing Blender material…"
                                if gpu_engine.material_inspect_requested()
                                else "Blender material inspection"
                                if gpu_engine.material_inspect_active()
                                else "GPU input paused — edit settings"
                                if gpu_engine.input_paused()
                                else "GPU painting… live GPU preview"),
                          icon='BRUSH_DATA')
                box.label(text=("Please wait — resident session preserved"
                                if gpu_engine.material_inspect_requested()
                                else "Press V to return to GPU painting"
                                if gpu_engine.material_inspect_active()
                                else "Press P to resume painting"
                                if gpu_engine.input_paused()
                                else "Press P to pause and edit settings"),
                          icon='INFO')
                if (not gpu_engine.material_inspect_active()
                        and not gpu_engine.material_inspect_requested()):
                    row = box.row()
                    row.enabled = not gpu_engine.stroke_active()
                    row.operator(
                        ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                        text="Inspect Blender Material",
                        icon='SHADING_RENDERED')
                elif gpu_engine.material_inspect_active():
                    box.operator(
                        ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                        text="Resume GPU Preview",
                        icon='PLAY')
                undo_count, redo_count = gpu_engine.history_counts()
                box.label(text="GPU Undo %d / Redo %d (Ctrl-Z / Ctrl-Shift-Z)"
                          % (undo_count, redo_count), icon='LOOP_BACK')
                row = box.row()
                row.enabled = not gpu_engine.stroke_active()
                row.operator(
                    ops.IMPASTO_OT_gpu_flush.bl_idname,
                    text="Flush for Save / Export",
                    icon='FILE_REFRESH')
                box.label(text="Ctrl-S flushes before saving; use Flush before "
                               "menu Save/Export",
                          icon='INFO')
                box.label(text="RMB/Esc flushes and stops",
                          icon='INFO')
            else:
                box.label(text="Draw brushes reuse Blender spacing, strength, "
                               "pressure and falloff",
                          icon='INFO')
                box.label(text="Radius and Hardness use the controls above",
                          icon='INFO')
            err = gpu_engine.last_error()
            if err:
                box.label(text="GPU paint failed — see console",
                          icon='ERROR')

    def _draw_bindings(self, box, state, layer):
        col = box.column(align=True)
        col.label(text="Channel Images / Layer Influence")

        def draw_channel(parent, c):
            ch = model.CHANNEL_MAP.get(c.name)
            if ch is None or not c.enabled:
                return
            row = parent.row(align=True)
            binding = None
            for b in layer.bindings:
                if b.name == c.name:
                    binding = b
                    break
            if binding is None:
                op = row.operator(ops.IMPASTO_OT_binding_add.bl_idname,
                                  text=ch.label, icon='ADD',
                                  emboss=False)
                op.channel_key = c.name
                return
            row.prop(binding, "enabled", text=ch.label)
            sub = row.row(align=True)
            sub.enabled = binding.enabled
            if binding.mode == 'COLOR':
                sub.prop(binding, "color", text="")
            elif binding.mode == 'VALUE':
                sub.prop(binding, "value", text="")
            elif layer.layer_type == 'PAINT':
                # Native single-channel edit of this channel's canvas.
                op = sub.operator(ops.IMPASTO_OT_paint_activate.bl_idname,
                                  text="", icon='TPAINT_HLT', emboss=False)
                op.channel_key = c.name
            else:
                sub.label(text="painted")
            sub.prop(binding, "opacity", text="Influence", slider=True)
            op = sub.operator(ops.IMPASTO_OT_binding_remove.bl_idname,
                              text="", icon='X', emboss=False)
            op.channel_key = c.name
            if layer.layer_type == 'PAINT':
                image = bpy.data.images.get(binding.image_name
                                            or layer.image_name)
                detail = parent.row()
                detail.enabled = False
                detail.label(text=("Image: %s" % image.name if image else
                                   "Image: missing"),
                             icon='IMAGE_DATA' if image else 'ERROR')

        grouped = {'Core': [], 'Emission': [], 'Subsurface': []}
        for c in state.channels:
            ch = model.CHANNEL_MAP.get(c.name)
            if ch is not None and c.enabled:
                grouped.setdefault(ch.panel_group, []).append(c)

        for c in grouped.get('Core', []):
            draw_channel(col, c)

        sections = (
            ('Emission', 'ui_show_emission_channels', 'LIGHT'),
            ('Subsurface', 'ui_show_subsurface_channels', 'SHADING_RENDERED'),
        )
        for group_name, prop_name, group_icon in sections:
            channels = grouped.get(group_name, [])
            if not channels:
                continue
            section = col.box()
            expanded = getattr(layer, prop_name)
            row = section.row(align=True)
            row.prop(layer, prop_name, text=group_name,
                     icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
                     emboss=False)
            row.label(text="%d" % len(channels), icon=group_icon)
            if expanded:
                for c in channels:
                    draw_channel(section, c)


class IMPASTO_PT_preview_lighting(bpy.types.Panel):
    """Compact popover; keeps five diagnostic controls out of the sidebar."""
    bl_idname = "IMPASTO_PT_preview_lighting"
    bl_label = "Preview Lighting"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'

    def draw(self, context):
        obj = context.object
        mat = obj.active_material if obj is not None else None
        tree = engine.find_stack_for_material(mat)
        layer = tree.impasto.active_layer() if tree is not None else None
        if layer is None:
            self.layout.label(text="No active Impasto layer", icon='INFO')
            return
        col = self.layout.column(align=True)
        col.label(text="Environment", icon='WORLD')
        col.prop(layer, "preview_environment_exposure", text="Exposure")
        col.prop(layer, "preview_environment_rotation", text="Rotation")
        col.separator()
        col.label(text="Studio Lights", icon='LIGHT')
        col.prop(layer, "preview_key_strength", text="Key")
        col.prop(layer, "preview_key_rotation", text="Key Rotation")
        col.prop(layer, "preview_fill_strength", text="Fill")
        col.separator()
        col.label(text="Base Normal Map", icon='NORMALS_FACE')
        col.template_ID(layer, "preview_base_normal_image", open="image.open")
        if obj is not None and getattr(obj, "data", None) is not None:
            col.prop_search(layer, "preview_base_normal_uv_map",
                            obj.data, "uv_layers", text="UV Map")
        else:
            col.prop(layer, "preview_base_normal_uv_map", text="UV Map")
        row = col.row(align=True)
        row.enabled = layer.preview_base_normal_image is not None
        row.prop(layer, "preview_base_normal_strength", text="Strength")
        row.prop(layer, "preview_base_normal_invert_green", text="Invert Green",
                 toggle=True)
        col.label(text="Clear the image field to disable", icon='INFO')
        col.label(text="Preview only — paint data is unchanged", icon='INFO')


class IMPASTO_MT_main(bpy.types.Menu):
    bl_idname = "IMPASTO_MT_main"
    bl_label = "Impasto"

    def draw(self, context):
        layout = self.layout
        layout.operator(ops.IMPASTO_OT_stack_init.bl_idname)
        op = layout.operator(ops.IMPASTO_OT_layer_add.bl_idname,
                             text="Impasto: Add Paint Layer")
        op.layer_type = 'PAINT'
        op = layout.operator(ops.IMPASTO_OT_layer_add.bl_idname,
                             text="Impasto: Add Fill Layer")
        op.layer_type = 'FILL'
        layout.operator(ops.IMPASTO_OT_stack_rebuild.bl_idname)
        layout.operator(ops.IMPASTO_OT_paint_activate.bl_idname)
        layout.operator(ops.IMPASTO_OT_native_multichannel_paint.bl_idname)
        layout.operator(ops.IMPASTO_OT_gpu_paint.bl_idname)
        layout.operator(ops.IMPASTO_OT_stack_remove.bl_idname)


def _menu_draw(self, context):
    self.layout.separator()
    self.layout.menu(IMPASTO_MT_main.bl_idname)


# Texture Paint mode has no Paint menu on 5.1.2 (probed: only
# vertex/weight/grease-pencil paint menus exist), so the Object menu +
# the always-visible sidebar tab + F3 cover discoverability.
_MENUS = ("VIEW3D_MT_object",)

_classes = (
    IMPASTO_UL_layers,
    IMPASTO_MT_add_channel_register,
    IMPASTO_MT_add_channel,
    IMPASTO_PT_main,
    IMPASTO_PT_preview_lighting,
    IMPASTO_MT_main,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
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
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
