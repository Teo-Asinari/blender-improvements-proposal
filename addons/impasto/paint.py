# SPDX-License-Identifier: GPL-2.0-or-later
"""Blender native texture-paint target activation (design section 5).

This module is the single owner of Blender paint state.  Keeping canvas
selection here prevents layer UI callbacks and operators from gradually
growing different paint-slot behaviour.
"""

import bpy

from . import compat
from . import model


class PaintTargetError(RuntimeError):
    """The active Impasto layer cannot be used as a paint canvas."""


def maybe_switch_material_preview(context, enabled=None):
    """Switch only the invoking VIEW_3D from Solid to Material Preview.

    Solid shading displays the active paint canvas rather than Impasto's
    composed shader and a transparent canvas can make the object disappear.
    Never touch other areas/viewports; return whether a switch occurred.
    """
    area = getattr(context, "area", None)
    if area is None or area.type != 'VIEW_3D':
        return False
    if enabled is None:
        addon = context.preferences.addons.get(__package__)
        enabled = (addon is None
                   or getattr(addon.preferences,
                              "auto_material_preview", True))
    if not enabled:
        return False
    space = getattr(area.spaces, "active", None)
    shading = getattr(space, "shading", None)
    if shading is None or shading.type != 'SOLID':
        return False
    shading.type = 'MATERIAL'
    return True


def _target_colorspace(layer):
    """Colorspace required by the first enabled shared channel binding."""
    bindings = {b.name: b for b in layer.bindings
                if b.enabled and b.mode == 'SHARED'}
    for channel in model.CHANNELS:
        if channel.key in bindings:
            return channel.colorspace
    return "sRGB"


def activate_paint_target(context, layer):
    """Make ``layer`` Blender's image-mode texture-paint canvas.

    This deliberately does not enter Texture Paint mode.  Layer selection is
    safe in Object/Edit mode; mode changes happen only through the explicit
    operator.  Returns whether the image colorspace needed repair.
    """
    obj = context.object
    if obj is None or obj.type != 'MESH':
        raise PaintTargetError("Select the mesh that owns this Impasto stack")
    if layer is None or layer.layer_type != 'PAINT':
        raise PaintTargetError("The active Impasto layer is not a paint layer")
    image = bpy.data.images.get(layer.image_name)
    if image is None:
        raise PaintTargetError("The active paint layer's image is missing")

    uv_layers = obj.data.uv_layers
    if not uv_layers:
        raise PaintTargetError("The mesh needs a UV map before texture painting")
    uv_name = layer.uv_map
    uv = uv_layers.get(uv_name) if uv_name else uv_layers.active
    if uv is None:
        raise PaintTargetError("Paint layer UV map %r is missing" % uv_name)
    uv_layers.active = uv

    wanted = compat.resolve_colorspace(image, _target_colorspace(layer))
    repaired = image.colorspace_settings.name != wanted
    if repaired:
        image.colorspace_settings.name = wanted

    image_paint = context.scene.tool_settings.image_paint
    image_paint.mode = 'IMAGE'
    image_paint.canvas = image

    # Keep Blender's conventional active image-node target aligned too.  The
    # explicit canvas above is authoritative, but selecting the generated
    # source node makes Image Editor/tool integrations show the same image.
    layer_tree = bpy.data.node_groups.get(model.layer_tree_name(layer.name))
    source = (layer_tree.nodes.get(model.n_src(layer.name))
              if layer_tree is not None else None)
    if source is not None:
        for node in layer_tree.nodes:
            node.select = False
        source.select = True
        layer_tree.nodes.active = source
    return repaired
