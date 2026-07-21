# SPDX-License-Identifier: GPL-2.0-or-later
"""Channel summaries and add-channel menus used by the Impasto sidebar."""

import bpy

from . import engine
from . import gpu_engine
from . import model
from . import ops

CORE_CHANNEL_BADGES = {
    'base_color': 'B', 'metallic': 'M', 'roughness': 'R',
    'normal': 'N', 'height': 'H', 'alpha': 'A',
}


def image_dimensions(image):
    """Return a Blender image datablock's usable pixel dimensions."""
    if image is None or len(image.size) < 2:
        return None
    width, height = int(image.size[0]), int(image.size[1])
    return (width, height) if width > 0 and height > 0 else None


def format_image_dimensions(image):
    """Compact actual-size readout for imported and generated images alike."""
    size = image_dimensions(image)
    return "%d × %d" % size if size else "size unavailable"


def paint_layer_image_sizes(layer):
    """Collect real dimensions for the images currently bound to a layer."""
    sizes = {}
    for binding in layer.bindings:
        if not binding.enabled:
            continue
        image = bpy.data.images.get(binding.image_name or layer.image_name)
        size = image_dimensions(image)
        if size is not None:
            sizes[binding.name] = size
    return sizes


def layer_channel_summary(keys):
    """Return the compact summary shown in each layer-list row."""
    ordered = [key for key in sorted(
        keys, key=lambda key: model.CHANNEL_ORDER.get(key, 99))
        if key in model.CHANNEL_MAP]
    parts = ["".join(CORE_CHANNEL_BADGES[key] for key in ordered
                     if key in CORE_CHANNEL_BADGES)]
    emission_count = sum(model.CHANNEL_MAP[key].panel_group == 'Emission'
                         for key in ordered)
    subsurface_count = sum(model.CHANNEL_MAP[key].panel_group == 'Subsurface'
                           for key in ordered)
    if emission_count:
        parts.append("E(%d)" % emission_count)
    if subsurface_count:
        parts.append("SS(%d)" % subsurface_count)
    return " ".join(part for part in parts if part)


def missing_channels(context, bind_active_layer):
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


def draw_missing_channels(layout, context, bind_active_layer):
    missing = missing_channels(context, bind_active_layer)
    if missing is None:
        layout.label(text="No Impasto stack", icon='INFO')
        return
    if not missing:
        layout.label(text="All supported channels are registered",
                     icon='CHECKMARK')
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
        draw_missing_channels(self.layout, context, False)


class IMPASTO_MT_add_channel(bpy.types.Menu):
    bl_idname = "IMPASTO_MT_add_channel"
    bl_label = "Add Material Channel"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Add to stack + selected layer",
                     icon='LAYER_ACTIVE')
        draw_missing_channels(layout, context, True)
        layout.separator()
        layout.menu(IMPASTO_MT_add_channel_register.bl_idname,
                    text="Register Without Layer Binding", icon='NODETREE')
