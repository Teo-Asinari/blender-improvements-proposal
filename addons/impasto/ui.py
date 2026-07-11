# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto UI: N-panel ("Impasto" tab, 3D Viewport), layer UIList, and
menu entries (Object menu) so every operator is reachable via panel,
menu, and F3 search. Standard Blender widgets, single-purpose rows —
deliberately minimal for phase 1 (UX iterates with the user)."""

import bpy

from . import engine
from . import model
from . import ops

_TYPE_ICONS = {'PAINT': 'BRUSH_DATA', 'FILL': 'SNAP_FACE',
               'GROUP': 'FILE_FOLDER'}


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
            chips = "".join(model.CHANNEL_MAP[k].label[0]
                            for k in sorted(
                                keys, key=lambda k:
                                model.CHANNEL_ORDER.get(k, 99))
                            if k in model.CHANNEL_MAP)
            sub.label(text=chips)


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
        layout.label(text="Channels: %s" % chan_labels)

        row = layout.row()
        row.template_list("IMPASTO_UL_layers", "", state, "layers",
                          state, "active_index", rows=4)
        col = row.column(align=True)
        op = col.operator(ops.IMPASTO_OT_layer_add.bl_idname, text="",
                          icon='BRUSH_DATA')
        op.layer_type = 'PAINT'
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
                row.prop(layer, "opacity", slider=True)
                self._draw_bindings(box, state, layer)
                if layer.layer_type == 'PAINT':
                    image = bpy.data.images.get(layer.image_name)
                    row = box.row(align=True)
                    row.label(text=image.name if image else "Missing image",
                              icon='IMAGE_DATA' if image else 'ERROR')
                    row = box.row()
                    row.scale_y = 1.25
                    row.operator(ops.IMPASTO_OT_paint_activate.bl_idname,
                                 text="Paint Active Layer",
                                 icon='TPAINT_HLT')
            else:
                box.prop(layer, "opacity", slider=True)

        layout.separator()
        layout.operator(ops.IMPASTO_OT_stack_rebuild.bl_idname,
                        text="Rebuild", icon='FILE_REFRESH')

    def _draw_bindings(self, box, state, layer):
        col = box.column(align=True)
        col.label(text="Channels")
        for c in state.channels:
            ch = model.CHANNEL_MAP.get(c.name)
            if ch is None or not c.enabled:
                continue
            row = col.row(align=True)
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
                continue
            row.prop(binding, "enabled", text=ch.label)
            sub = row.row(align=True)
            sub.enabled = binding.enabled
            if binding.mode == 'COLOR':
                sub.prop(binding, "color", text="")
            elif binding.mode == 'VALUE':
                sub.prop(binding, "value", text="")
            else:
                sub.label(text="painted")
            sub.prop(binding, "opacity", text="", slider=True)
            op = sub.operator(ops.IMPASTO_OT_binding_remove.bl_idname,
                              text="", icon='X', emboss=False)
            op.channel_key = c.name


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
    IMPASTO_PT_main,
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
