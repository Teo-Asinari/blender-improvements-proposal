# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto operators. Every operator is discoverable three ways —
sidebar panel, menu entry, F3 search (labels carry the add-on prefix);
all are REGISTER | UNDO. Bulk edits run inside stack_edit_session so a
whole operator costs exactly one compile+reconcile (design §4.7)."""

import json

import bpy
from bpy.props import EnumProperty, StringProperty

from . import compat
from . import engine
from . import model
from . import paint

DEFAULT_IMAGE_SIZE = 2048

TEMPLATE_IDS = {
    'PRINCIPLED_STANDARD': "Principled — Standard",
    'PRINCIPLED_FULL': "Principled — Full",
    'EMISSIVE_PROP': "Emissive prop",
    'SKIN_ORGANIC': "Skin / organic",
}
TEMPLATE_ITEMS = tuple(
    (tid, label, "Channels: " + ", ".join(
        model.CHANNEL_MAP[k].label for k in model.TEMPLATES[label]))
    for tid, label in TEMPLATE_IDS.items())


def _context_material(context):
    obj = context.object
    if obj is None:
        return None
    return obj.active_material


def _context_stack(context):
    """(material, root stack tree) for the active object, or Nones."""
    mat = _context_material(context)
    if mat is None:
        return None, None
    return mat, engine.find_stack_for_material(mat)


def _unique_uid(state):
    existing = ({ly.name for ly in state.layers}
                | {m.name for ly in state.layers for m in ly.masks})
    return model.new_uid(existing)


def _active_uv_map(context):
    obj = context.object
    if obj is not None and obj.type == 'MESH':
        uvs = obj.data.uv_layers
        if uvs and uvs.active:
            return uvs.active.name
    return ""


def new_layer_image(name, colorspace, size=DEFAULT_IMAGE_SIZE,
                    generated_color=(0.0, 0.0, 0.0, 0.0)):
    """Registry-driven image creation (design §5.3): colorspace comes
    from the channel table, the caller chooses the channel-neutral canvas,
    and a fake user protects paint data while a layer is hidden/pruned."""
    img = bpy.data.images.new(name, size, size, alpha=True)
    img.generated_color = generated_color
    img.use_fake_user = True
    compat.set_image_colorspace(img, colorspace)
    return img


class IMPASTO_OT_stack_init(bpy.types.Operator):
    """Create an Impasto layer stack on the active material (a new
    material is created if the object has none)"""
    bl_idname = "impasto.stack_init"
    bl_label = "Impasto: New Layer Stack"
    bl_options = {'REGISTER', 'UNDO'}

    template: EnumProperty(name="Template", items=TEMPLATE_ITEMS,
                           default='PRINCIPLED_STANDARD')

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def execute(self, context):
        obj = context.object
        mat = obj.active_material
        if mat is None:
            mat = bpy.data.materials.new(obj.name + " Material")
            if obj.material_slots:
                obj.material_slots[obj.active_material_index].material \
                    = mat
            else:
                obj.data.materials.append(mat)
        mat.use_nodes = True
        if engine.find_stack_for_material(mat) is not None:
            self.report({'WARNING'},
                        "Material %r already has an Impasto stack"
                        % mat.name)
            return {'CANCELLED'}
        principled = compat.find_principled(mat.node_tree)
        if principled is None:
            self.report({'ERROR'},
                        "Material %r has no Principled BSDF node"
                        % mat.name)
            return {'CANCELLED'}

        keys = model.TEMPLATES[TEMPLATE_IDS[self.template]]
        tree = bpy.data.node_groups.new("Impasto Stack (%s)" % mat.name,
                                        "ShaderNodeTree")
        # Attach the root tree before the edit session exits. This makes
        # material_for_stack() able to discover the material, allowing the
        # compiler's material TreeSpec to create and wire the generated node.
        group_node = mat.node_tree.nodes.new("ShaderNodeGroup")
        group_node.name = model.n_material_stack()
        group_node.label = "Impasto (generated)"
        group_node.node_tree = tree
        with engine.stack_edit_session(tree):
            state = tree.impasto
            state.is_stack = True
            state.schema_version = model.SCHEMA_VERSION
            state.blender_version = bpy.app.version
            for key in keys:
                ch = state.channels.add()
                ch.name = key
            # remember the Principled links we are about to displace so
            # Remove Stack can restore the material (ori_bsdf pattern).
            displaced = []
            targets = [model.CHANNEL_MAP[k].socket for k in keys
                       if k != "height"]
            if "height" in keys:
                targets.append("Normal")
            # Normal and Height converge on the same Principled socket.
            targets = list(dict.fromkeys(targets))
            for sock_name in targets:
                sock = compat.find_socket(principled.inputs, sock_name)
                if sock is None:
                    continue
                for link in sock.links:
                    displaced.append({
                        "from_node": link.from_node.name,
                        "from_socket": link.from_socket.identifier,
                        "to_socket": sock_name,
                    })
            mstate = mat.impasto_mat
            mstate.displaced_links = json.dumps(displaced)
            mstate.stack_tree = tree.name
        self.report({'INFO'}, "Impasto stack created on %r (%s)"
                    % (mat.name, ", ".join(keys)))
        return {'FINISHED'}


class IMPASTO_OT_stack_remove(bpy.types.Operator):
    """Remove the Impasto stack from the active material and restore
    the Principled links it displaced (images are kept)"""
    bl_idname = "impasto.stack_remove"
    bl_label = "Impasto: Remove Layer Stack"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _context_stack(context)[1] is not None

    def execute(self, context):
        mat, tree = _context_stack(context)
        nt = mat.node_tree
        group_node = nt.nodes.get(model.n_material_stack())
        if group_node is not None:
            nt.nodes.remove(group_node)
        principled = compat.find_principled(nt)
        restored = 0
        try:
            displaced = json.loads(mat.impasto_mat.displaced_links
                                   or "[]")
        except Exception:
            displaced = []
        if principled is not None:
            for entry in displaced:
                src_node = nt.nodes.get(entry.get("from_node", ""))
                dst = compat.find_socket(principled.inputs,
                                         entry.get("to_socket", ""))
                if src_node is None or dst is None:
                    continue
                src = compat.find_socket(src_node.outputs,
                                         entry.get("from_socket", ""))
                if src is None:
                    continue
                nt.links.new(src, dst)
                restored += 1
        mat.impasto_mat.displaced_links = ""
        mat.impasto_mat.stack_tree = ""
        # drop generated trees (images are deliberately kept)
        for ly in tree.impasto.layers:
            lt = bpy.data.node_groups.get(model.layer_tree_name(ly.name))
            if lt is not None:
                bpy.data.node_groups.remove(lt)
        bpy.data.node_groups.remove(tree)
        self.report({'INFO'},
                    "Impasto stack removed (%d link(s) restored, "
                    "images kept)" % restored)
        return {'FINISHED'}


class IMPASTO_OT_layer_add(bpy.types.Operator):
    """Add a layer to the top of the stack (paint layers get a fresh
    transparent canvas bound to Base Color)"""
    bl_idname = "impasto.layer_add"
    bl_label = "Impasto: Add Layer"
    bl_options = {'REGISTER', 'UNDO'}

    layer_type: EnumProperty(
        name="Type",
        items=(('PAINT', "Paint", "Painted image layer"),
               ('FILL', "Fill", "Constant color/value layer"),
               ('GROUP', "Group", "Organizational group")),
        default='PAINT')
    channel_key: EnumProperty(
        name="Initial Channel",
        items=tuple((ch.key, ch.label, "Paint a dedicated %s image" % ch.label)
                    for ch in model.CHANNELS),
        default="base_color",
        description="Channel initially bound to a new Paint layer")

    @classmethod
    def poll(cls, context):
        return _context_stack(context)[1] is not None

    def execute(self, context):
        _, tree = _context_stack(context)
        state = tree.impasto
        uid = _unique_uid(state)
        count = sum(1 for ly in state.layers
                    if ly.layer_type == self.layer_type) + 1
        initial = model.CHANNEL_MAP.get(self.channel_key,
                                        model.CHANNEL_MAP["base_color"])
        base = {'PAINT': ("Height Detail" if initial.key == "height"
                          else "Paint Layer"), 'FILL': "Fill Layer",
                'GROUP': "Group"}[self.layer_type]
        with engine.stack_edit_session(tree):
            ly = state.layers.add()
            ly.name = uid
            ly.label = "%s %d" % (base, count)
            ly.layer_type = self.layer_type
            if self.layer_type == 'PAINT':
                ch = initial
                generated = ((0.5, 0.5, 0.5, 1.0)
                             if ch.key == "height"
                             else (0.0, 0.0, 0.0, 0.0))
                img = new_layer_image("Impasto %s %s" % (ly.label, uid),
                                      ch.colorspace,
                                      generated_color=generated)
                ly.image_name = img.name
                ly.uv_map = _active_uv_map(context)
                b = ly.bindings.add()
                b.name = ch.key
            elif self.layer_type == 'FILL':
                ch = model.CHANNEL_MAP["base_color"]
                b = ly.bindings.add()
                b.name = "base_color"
                b.mode = 'COLOR'
                b.color = model.seed_rgba(ch)
            state.layers.move(len(state.layers) - 1, 0)
            state.active_index = 0
        self.report({'INFO'}, "%s added" % state.layers[0].label)
        return {'FINISHED'}


class IMPASTO_OT_layer_remove(bpy.types.Operator):
    """Delete the active layer (its image, if any, is kept)"""
    bl_idname = "impasto.layer_remove"
    bl_label = "Impasto: Delete Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        return (tree is not None
                and tree.impasto.active_layer() is not None)

    def execute(self, context):
        _, tree = _context_stack(context)
        state = tree.impasto
        idx = next((i for i, ly in enumerate(state.layers)
                    if ly.name == state.active_layer_uid), -1)
        if idx < 0:
            return {'CANCELLED'}
        label = state.layers[idx].label
        with engine.stack_edit_session(tree):
            state.layers.remove(idx)
            state.active_index = min(idx, len(state.layers) - 1)
        self.report({'INFO'}, "%s deleted (image kept)" % label)
        return {'FINISHED'}


class IMPASTO_OT_layer_move(bpy.types.Operator):
    """Move the active layer up or down the stack (uids, bindings and
    animation paths are untouched — order is presentation-only)"""
    bl_idname = "impasto.layer_move"
    bl_label = "Impasto: Move Layer"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(items=(('UP', "Up", ""),
                                   ('DOWN', "Down", "")),
                            default='UP')

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        return (tree is not None
                and tree.impasto.active_layer() is not None)

    def execute(self, context):
        _, tree = _context_stack(context)
        state = tree.impasto
        idx = next((i for i, ly in enumerate(state.layers)
                    if ly.name == state.active_layer_uid), -1)
        dst = idx + (-1 if self.direction == 'UP' else 1)
        if idx < 0 or not (0 <= dst < len(state.layers)):
            return {'CANCELLED'}
        with engine.stack_edit_session(tree):
            state.layers.move(idx, dst)
            state.active_index = dst
        return {'FINISHED'}


class IMPASTO_OT_binding_add(bpy.types.Operator):
    """Bind the active layer to a channel (paint layers share their
    canvas; fill layers deposit the channel's default constant)"""
    bl_idname = "impasto.binding_add"
    bl_label = "Impasto: Bind Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel_key: StringProperty()

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        ly = tree.impasto.active_layer() if tree else None
        return ly is not None and ly.layer_type != 'GROUP'

    def execute(self, context):
        _, tree = _context_stack(context)
        state = tree.impasto
        ly = state.active_layer()
        ch = model.CHANNEL_MAP.get(self.channel_key)
        if ly is None or ch is None:
            return {'CANCELLED'}
        with engine.stack_edit_session(tree):
            b = ly.bindings.get(self.channel_key)
            if b is None:
                if (ly.layer_type == 'PAINT'
                        and any(existing.mode == 'SHARED'
                                for existing in ly.bindings)):
                    self.report({"WARNING"},
                                "Native Paint layers use one channel image; "
                                "add a dedicated %s Paint layer" % ch.label)
                    return {'CANCELLED'}
                b = ly.bindings.add()
                b.name = self.channel_key
                if ly.layer_type == 'PAINT':
                    b.mode = 'SHARED'
                elif ch.kind == 'COLOR':
                    b.mode = 'COLOR'
                    b.color = model.seed_rgba(ch)
                else:
                    b.mode = 'VALUE'
                    b.value = float(ch.default_value[0])
            b.enabled = True
        return {'FINISHED'}


class IMPASTO_OT_binding_remove(bpy.types.Operator):
    """Unbind a channel from the active layer"""
    bl_idname = "impasto.binding_remove"
    bl_label = "Impasto: Unbind Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel_key: StringProperty()

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        return tree is not None and tree.impasto.active_layer()

    def execute(self, context):
        _, tree = _context_stack(context)
        ly = tree.impasto.active_layer()
        idx = next((i for i, b in enumerate(ly.bindings)
                    if b.name == self.channel_key), -1)
        if idx < 0:
            return {'CANCELLED'}
        with engine.stack_edit_session(tree):
            ly.bindings.remove(idx)
        return {'FINISHED'}


class IMPASTO_OT_stack_rebuild(bpy.types.Operator):
    """Drop caches and rebuild the stack's node trees from scratch —
    the one repair operator for tampered or stale graphs"""
    bl_idname = "impasto.stack_rebuild"
    bl_label = "Impasto: Rebuild Stack"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _context_stack(context)[1] is not None

    def execute(self, context):
        _, tree = _context_stack(context)
        deltas = engine.rebuild(tree)
        self.report({'INFO'}, "Impasto rebuild: %s" % deltas)
        return {'FINISHED'}


class IMPASTO_OT_paint_activate(bpy.types.Operator):
    """Use the active Impasto paint layer as Blender's native brush canvas
    and enter Texture Paint mode"""
    bl_idname = "impasto.paint_activate"
    bl_label = "Impasto: Paint Active Layer"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type == 'PAINT'

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer()
        try:
            repaired = paint.activate_paint_target(context, layer)
        except paint.PaintTargetError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        obj = context.object
        try:
            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
        except RuntimeError as exc:
            self.report({'ERROR'}, "Could not enter Texture Paint mode: %s" % exc)
            return {'CANCELLED'}
        paint.maybe_switch_material_preview(context)
        message = "Painting %s" % layer.label
        if repaired:
            message += " (image colorspace repaired)"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class IMPASTO_OT_detail_paint(bpy.types.Operator):
    """Activate a Height Detail canvas and configure an accumulating native
    brush. Raise adds white; Lower subtracts white around neutral gray."""
    bl_idname = "impasto.detail_paint"
    bl_label = "Impasto: Paint Height Detail"
    bl_options = {'REGISTER'}

    direction: EnumProperty(
        items=(('RAISE', "Raise", "Repeated strokes build raised detail"),
               ('LOWER', "Lower", "Repeated strokes build recessed detail")),
        default='RAISE')

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return (layer is not None and layer.layer_type == 'PAINT'
                and any(b.enabled and b.name == 'height'
                        for b in layer.bindings))

    def execute(self, context):
        result = bpy.ops.impasto.paint_activate()
        if result != {'FINISHED'}:
            return result
        brush = context.scene.tool_settings.image_paint.brush
        if brush is None:
            self.report({'ERROR'}, "No Texture Paint brush is active")
            return {'CANCELLED'}
        brush.blend = 'ADD' if self.direction == 'RAISE' else 'SUB'
        brush.color = (1.0, 1.0, 1.0)
        self.report({'INFO'}, "%s height detail; repeated strokes accumulate"
                    % self.direction.title())
        return {'FINISHED'}


_classes = (
    IMPASTO_OT_stack_init,
    IMPASTO_OT_stack_remove,
    IMPASTO_OT_layer_add,
    IMPASTO_OT_layer_remove,
    IMPASTO_OT_layer_move,
    IMPASTO_OT_binding_add,
    IMPASTO_OT_binding_remove,
    IMPASTO_OT_stack_rebuild,
    IMPASTO_OT_paint_activate,
    IMPASTO_OT_detail_paint,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
