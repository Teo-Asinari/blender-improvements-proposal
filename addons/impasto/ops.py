# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto operators. Every operator is discoverable three ways —
sidebar panel, menu entry, F3 search (labels carry the add-on prefix);
all are REGISTER | UNDO. Bulk edits run inside stack_edit_session so a
whole operator costs exactly one compile+reconcile (design §4.7)."""

import json
import time

import bpy
from bpy.props import EnumProperty, StringProperty

from . import compat
from . import engine
from . import gpu_engine
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


def _channel_canvas_seed(ch):
    """Fresh-canvas fill: Height accumulates around opaque neutral
    mid-gray; every other channel starts transparent so unpainted
    texels contribute nothing (alpha gates the root chain factor)."""
    return ((0.5, 0.5, 0.5, 1.0) if ch.key == "height"
            else (0.0, 0.0, 0.0, 0.0))


def _layer_canvas_size(layer):
    """Resolution of the layer's existing canvases, so every channel
    image of one logical layer matches (a GPU MRT session requires
    equal sizes). Falls back to the stack default."""
    for b in layer.bindings:
        img = bpy.data.images.get(b.image_name or layer.image_name)
        if img is not None and img.size[0] > 0:
            return img.size[0]
    img = bpy.data.images.get(layer.image_name)
    if img is not None and img.size[0] > 0:
        return img.size[0]
    return DEFAULT_IMAGE_SIZE


def import_normal_baseline(mat, image, uv_map="", fallback_node_name=""):
    """Insert or update an external tangent normal image at stack bottom.

    Public soft-integration seam used by Kiln. The image participates in the
    same encoded-RGB normal chain as painted Normal layers, so the stack keeps
    sole ownership of Principled Normal instead of two add-ons overwriting the
    same socket. Returns the imported layer, or raises ValueError.

    ``fallback_node_name`` is the material Normal Map node to restore if the
    user later removes Impasto; a newer external baseline supersedes whatever
    Normal link was displaced when the stack was first created.
    """
    tree = engine.find_stack_for_material(mat)
    if tree is None:
        raise ValueError("Material has no Impasto stack")
    if image is None:
        raise ValueError("Normal baseline image is missing")
    compat.set_image_colorspace(image, "Non-Color")
    image.use_fake_user = True
    state = tree.impasto
    active_uid = state.active_layer_uid
    imported = None
    with engine.stack_edit_session(tree):
        if not any(c.name == "normal" for c in state.channels):
            channel = state.channels.add()
            channel.name = "normal"
        for layer in state.layers:
            for binding in layer.bindings:
                if (binding.name == "normal"
                        and layer.label == "Kiln Baked Normal"):
                    imported = layer
                    binding.image_name = image.name
                    break
            if imported is not None:
                break
        if imported is None:
            imported = state.layers.add()  # append = bottom of UI/stack
            imported.name = _unique_uid(state)
            imported.label = "Kiln Baked Normal"
            imported.layer_type = 'PAINT'
            binding = imported.bindings.add()
            binding.name = "normal"
            binding.mode = 'SHARED'
        imported.image_name = image.name
        imported.uv_map = uv_map
        binding = next(b for b in imported.bindings if b.name == "normal")
        binding.image_name = image.name
        binding.enabled = True
        # Keep/re-establish it as the bottom baseline on repeated bakes.
        index = next(i for i, layer in enumerate(state.layers)
                     if layer.name == imported.name)
        if index != len(state.layers) - 1:
            state.layers.move(index, len(state.layers) - 1)

        if fallback_node_name:
            try:
                displaced = json.loads(mat.impasto_mat.displaced_links
                                       or "[]")
            except Exception:
                displaced = []
            displaced = [entry for entry in displaced
                         if entry.get("to_socket") != "Normal"]
            displaced.append({
                "from_node": fallback_node_name,
                "from_socket": "Normal",
                "to_socket": "Normal",
            })
            mat.impasto_mat.displaced_links = json.dumps(displaced)

    if active_uid:
        state.active_layer_uid = active_uid
        state.active_index = next(
            (i for i, layer in enumerate(state.layers)
             if layer.name == active_uid), -1)
    return imported


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
    canvas_size: EnumProperty(
        name="Canvas Size",
        items=(('1024', "1024 (1K)", ""),
               ('2048', "2048 (2K)", "Default; four-channel GPU strokes "
                "sync within the pen-lift budget"),
               ('4096', "4096 (4K)", "Four-channel GPU pen-lift sync "
                "exceeds 400 ms (Image.pixels writes; see README)")),
        default='2048',
        description="Resolution of every canvas this layer creates "
                    "(later channel canvases inherit it)")

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
                img = new_layer_image("Impasto %s %s" % (ly.label, uid),
                                      ch.colorspace,
                                      size=int(self.canvas_size),
                                      generated_color=_channel_canvas_seed(ch))
                # Explicit per-binding canvas (schema 2). The layer slot
                # mirrors the primary canvas for legacy compatibility.
                ly.image_name = img.name
                ly.uv_map = _active_uv_map(context)
                b = ly.bindings.add()
                b.name = ch.key
                b.image_name = img.name
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
    """Bind the active layer to a channel (paint layers get a dedicated
    per-channel canvas; fill layers deposit the channel's default
    constant)"""
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
                b = ly.bindings.add()
                b.name = self.channel_key
                if ly.layer_type == 'PAINT':
                    # One logical layer, separate per-channel canvases:
                    # every SHARED binding owns an image sized to match
                    # the layer's existing canvases (one GPU stroke can
                    # then deposit into all of them at once).
                    b.mode = 'SHARED'
                    img = new_layer_image(
                        "Impasto %s %s %s" % (ly.label, ch.label, ly.name),
                        ch.colorspace,
                        size=_layer_canvas_size(ly),
                        generated_color=_channel_canvas_seed(ch))
                    b.image_name = img.name
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


class IMPASTO_OT_import_kiln_normal(bpy.types.Operator):
    """Repair/import an existing Kiln bake beneath this Impasto stack"""
    bl_idname = "impasto.import_kiln_normal"
    bl_label = "Impasto: Import/Repair Kiln Normal"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        mat, tree = _context_stack(context)
        if mat is None or tree is None or not mat.use_nodes:
            return False
        tex = mat.node_tree.nodes.get("Kiln Bake Target")
        return (tex is not None and tex.bl_idname == 'ShaderNodeTexImage'
                and tex.image is not None)

    def execute(self, context):
        mat, _tree = _context_stack(context)
        tex = mat.node_tree.nodes.get("Kiln Bake Target")
        uv_map = _active_uv_map(context)
        try:
            layer = import_normal_baseline(
                mat, tex.image, uv_map=uv_map,
                fallback_node_name="Kiln Normal Map")
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "%s imported beneath the Impasto stack"
                    % layer.label)
        return {'FINISHED'}


class IMPASTO_OT_paint_activate(bpy.types.Operator):
    """Use one of the active Impasto paint layer's channel canvases as
    Blender's native brush canvas and enter Texture Paint mode"""
    bl_idname = "impasto.paint_activate"
    bl_label = "Impasto: Paint Active Layer"
    bl_options = {'REGISTER'}

    channel_key: StringProperty(
        name="Channel",
        description="Channel canvas to paint natively; empty picks the "
                    "layer's first enabled painted channel",
        default="")

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type == 'PAINT'

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer()
        try:
            repaired = paint.activate_paint_target(context, layer,
                                                   self.channel_key)
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
        brush_selected = paint.activate_brush_tool(context)
        paint.maybe_switch_material_preview(context)
        binding = paint.paint_binding(layer, self.channel_key)
        message = "Painting %s" % layer.label
        if binding is not None:
            message += " (%s)" % model.CHANNEL_MAP[binding.name].label
        if repaired:
            message += " (image colorspace repaired)"
        if not brush_selected and not bpy.app.background:
            message += " (Texture Paint active; select Paint tool if hidden)"
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
        result = bpy.ops.impasto.paint_activate(channel_key='height')
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


def gpu_paint_targets(layer):
    """Ordered (channel key, Image) pairs one GPU stroke deposits into:
    the layer's enabled SHARED bindings of GPU-paintable channels whose
    canvas exists, in registry order (payloads align by index)."""
    pairs = []
    for ch in model.CHANNELS:
        if ch.key not in gpu_engine.GPU_PAINT_CHANNEL_KEYS:
            continue
        b = layer.bindings.get(ch.key)
        if b is None or not b.enabled or b.mode != 'SHARED':
            continue
        image = bpy.data.images.get(b.image_name or layer.image_name)
        if image is not None:
            pairs.append((ch.key, image))
    return pairs


def _gpu_brush(layer):
    """Snapshot the layer's brush PropertyGroup values into the plain
    dict gpu_engine.stroke_payloads consumes (pure seam)."""
    return {"color": tuple(layer.paint_color),
            "roughness": layer.paint_roughness,
            "metallic": layer.paint_metallic,
            "normal": tuple(layer.paint_normal),
            "height_strength": layer.paint_height_strength,
            "height_direction": layer.paint_height_direction}


class IMPASTO_OT_gpu_paint(bpy.types.Operator):
    """Paint every bound channel of the active Impasto paint layer with
    one GPU stroke (LMB paints, RMB or Esc stops; canvases sync on
    pen-lift). Strokes are not undoable yet — see the README"""
    bl_idname = "impasto.gpu_paint"
    bl_label = "Impasto: GPU Paint All Channels"
    bl_options = {'REGISTER'}

    # The modal loop never touches gpu: it enqueues dabs / tags redraws
    # and writes finished readbacks into the Image datablocks. All GPU
    # work happens in gpu_engine's POST_VIEW draw callback.

    @classmethod
    def poll(cls, context):
        if gpu_engine.session_active():
            return False   # one session at a time
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        obj = context.object
        return (obj is not None and obj.type == 'MESH'
                and layer is not None and layer.layer_type == 'PAINT')

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'},
                        "Run from a 3D Viewport (Impasto sidebar)")
            return {'CANCELLED'}
        # The modal loop needs region-relative mouse coordinates; the
        # panel button invokes from the UI region, so find WINDOW.
        region = next((r for r in context.area.regions
                       if r.type == 'WINDOW'), None)
        if region is None:
            self.report({'ERROR'}, "No drawable viewport region found")
            return {'CANCELLED'}

        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer()
        targets = gpu_paint_targets(layer)
        if not targets:
            self.report({'ERROR'},
                        "The active layer has no paintable channel canvas")
            return {'CANCELLED'}
        sizes = {tuple(img.size) for _key, img in targets}
        if len(sizes) != 1 or any(w != h for w, h in sizes):
            self.report({'ERROR'},
                        "Channel canvases must share one square "
                        "resolution; got %s"
                        % sorted("%dx%d" % s for s in sizes))
            return {'CANCELLED'}

        obj = context.object
        if obj.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError as exc:
                self.report({'ERROR'},
                            "Could not enter Object Mode: %s" % exc)
                return {'CANCELLED'}
        if layer.uv_map:
            uv = obj.data.uv_layers.get(layer.uv_map)
            if uv is None:
                self.report({'ERROR'}, "Paint layer UV map %r is missing"
                            % layer.uv_map)
                return {'CANCELLED'}
            obj.data.uv_layers.active = uv

        keys = [key for key, _img in targets]
        images = [img for _key, img in targets]
        payloads = gpu_engine.stroke_payloads(keys, _gpu_brush(layer))
        settings = {
            "radius": layer.brush_radius,
            "hardness": layer.brush_hardness,
            "occlusion": True,
            "subrect": True,
            "channel_keys": tuple(keys),
            "preview_index": (keys.index(layer.preview_channel)
                              if layer.preview_channel in keys else 0),
        }
        if not gpu_engine.start_session(obj, images, region,
                                        payloads=payloads,
                                        settings=settings):
            self.report({'ERROR'}, "Mesh has no UV map or faces")
            return {'CANCELLED'}
        paint.maybe_switch_material_preview(context)

        self._region = region
        self._radius = layer.brush_radius
        self._stopping = False
        self._timer = context.window_manager.event_timer_add(
            0.05, window=context.window)
        context.window_manager.modal_handler_add(self)
        region.tag_redraw()
        self.report({'INFO'},
                    "GPU painting %s — LMB paints, RMB/Esc stops"
                    % ", ".join(model.CHANNEL_MAP[k].label for k in keys))
        return {'RUNNING_MODAL'}

    # -- helpers -----------------------------------------------------------

    def _mouse_region(self, event):
        return (event.mouse_x - self._region.x,
                event.mouse_y - self._region.y)

    def _inside_region(self, event):
        rx, ry = self._mouse_region(event)
        return (0 <= rx < self._region.width
                and 0 <= ry < self._region.height)

    def _apply_pending_sync(self):
        """Write a finished stroke's readback into the channel Image
        datablocks — always here, never in a draw callback."""
        pending = gpu_engine.take_pending_pixels()
        if pending is None:
            return
        write_ms = 0.0
        update_ms = 0.0
        for arr, image_name in pending:
            image = bpy.data.images.get(image_name)
            if image is None:
                continue
            t0 = time.perf_counter()
            image.pixels.foreach_set(arr)
            t1 = time.perf_counter()
            image.update()
            t2 = time.perf_counter()
            write_ms += (t1 - t0) * 1000.0
            update_ms += (t2 - t1) * 1000.0
        gpu_engine.record_sync_stats(write_ms, update_ms)
        # Repaint the composed material and any Image editors.
        for area in bpy.context.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                area.tag_redraw()

    def _finish(self, context):
        gpu_engine.stop_session()
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._region is not None:
            self._region.tag_redraw()
        return {'FINISHED'}

    # -- modal loop ----------------------------------------------------------

    def modal(self, context, event):
        if not gpu_engine.session_active():
            return self._finish(context)

        self._apply_pending_sync()

        if self._stopping:
            if not gpu_engine.busy():
                return self._finish(context)
            self._region.tag_redraw()   # keep draw callbacks pumping
            return {'RUNNING_MODAL'}

        if gpu_engine.last_error() is not None:
            # Latched GPU failure: stop cleanly; the console holds the
            # traceback. Native painting stays fully available.
            self.report({'WARNING'},
                        "Impasto GPU paint failed — see console; "
                        "native painting is unaffected")
            return self._finish(context)

        etype = event.type
        if etype == 'LEFTMOUSE':
            if event.value == 'PRESS' and self._inside_region(event):
                rx, ry = self._mouse_region(event)
                gpu_engine.begin_stroke(rx, ry, event.pressure)
                self._region.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and gpu_engine.stroke_active():
                gpu_engine.end_stroke()
                self._region.tag_redraw()
                return {'RUNNING_MODAL'}
        elif etype == 'MOUSEMOVE' and gpu_engine.stroke_active():
            rx, ry = self._mouse_region(event)
            gpu_engine.move_stroke(rx, ry, event.pressure, self._radius)
            self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        elif etype in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            if gpu_engine.stroke_active():
                gpu_engine.end_stroke()
            self._stopping = True
            self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        elif etype == 'TIMER':
            return {'RUNNING_MODAL'}

        # Orbit/zoom/pan and UI clicks pass through; the depth prepass
        # re-renders itself at the next draw after any view change.
        return {'PASS_THROUGH'}

    def cancel(self, context):
        gpu_engine.stop_session()
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


_classes = (
    IMPASTO_OT_stack_init,
    IMPASTO_OT_stack_remove,
    IMPASTO_OT_layer_add,
    IMPASTO_OT_layer_remove,
    IMPASTO_OT_layer_move,
    IMPASTO_OT_binding_add,
    IMPASTO_OT_binding_remove,
    IMPASTO_OT_stack_rebuild,
    IMPASTO_OT_import_kiln_normal,
    IMPASTO_OT_paint_activate,
    IMPASTO_OT_detail_paint,
    IMPASTO_OT_gpu_paint,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    # A modal GPU session owns viewport draw handlers and GPU resources.
    # Tear it down before unregistering its operator classes so an add-on
    # reload cannot leave a stale, invisible session behind.
    gpu_engine.stop_session()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
