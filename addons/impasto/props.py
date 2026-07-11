# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto stack state: PropertyGroups stored on the generated root
node group's ShaderNodeTree (A4 — free .blend persistence, append/link
portability, undo integration).

Stable-ID rules (A3, design §2.2):

- every Layer/Mask gets an immutable 8-hex uid at creation and
  ``PropertyGroup.name`` IS the uid (collections keyed by name), so
  F-Curve data_paths use the string-key form and survive reorder;
- ``ChannelState.name`` / ``ChannelBinding.name`` are registry channel
  keys, not uids;
- collection indices are presentation order ONLY; every cross-reference
  is by uid.

Every ``update=`` callback routes to engine.py's trigger classifier
(UNIFORM / TOGGLE / STRUCTURAL) — no callback mutates the graph (A1).
"""

import re

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, FloatVectorProperty, IntProperty,
                       IntVectorProperty, PointerProperty,
                       StringProperty)

from . import engine
from . import model

_LAYER_UID_RE = re.compile(r'layers\["([^"]+)"\]')


def _owner_layer_uid(pg):
    """Owning layer uid parsed from the PropertyGroup's RNA path, e.g.
    impasto.layers["c3a91f02"].bindings["roughness"] -> c3a91f02."""
    try:
        m = _LAYER_UID_RE.search(pg.path_from_id())
    except Exception:
        return ""
    return m.group(1) if m else ""


def _uniform(self, context):
    engine.on_uniform(self.id_data)


def _structural(self, context):
    engine.on_structural(self.id_data)


def _toggle(self, context):
    engine.on_toggle(self.id_data, _owner_layer_uid(self) or
                     getattr(self, "name", ""))


def _blend_items():
    labels = {"MIX": "Mix", "MULTIPLY": "Multiply", "SCREEN": "Screen",
              "ADD": "Add", "SUBTRACT": "Subtract",
              "OVERLAY": "Overlay"}
    return tuple((m, labels.get(m, m.title()), "") for m in
                 model.BLEND_MODES)


class ImpastoBinding(bpy.types.PropertyGroup):
    """Per-layer-per-channel participation (R1). SPARSE: a layer only
    has bindings for channels it touches. name = channel key."""
    enabled: BoolProperty(
        name="Enabled",
        description="Whether this layer deposits into this channel "
                    "(instant to flip; the shader slims after a pause)",
        default=True, update=_toggle)
    mode: EnumProperty(
        name="Mode",
        items=(('SHARED', "Shared",
                "Consume the layer's painted source"),
               ('VALUE', "Value", "Deposit a constant value"),
               ('COLOR', "Color", "Deposit a constant color")),
        default='SHARED', update=_structural)
    value: FloatProperty(
        name="Value", default=0.0, soft_min=0.0, soft_max=1.0,
        update=_uniform)
    color: FloatVectorProperty(
        name="Color", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.8, 0.8, 0.8, 1.0), update=_uniform)
    blend_mode: EnumProperty(
        name="Blend",
        items=(('LAYER', "Layer default", "Inherit the layer's blend "
                "mode"),) + _blend_items(),
        default='LAYER', update=_structural)
    opacity: FloatProperty(
        name="Opacity", default=1.0, min=0.0, max=1.0,
        subtype='FACTOR', update=_uniform)
    use_masks: BoolProperty(
        name="Use Masks",
        description="Gate this channel's deposit by the layer's mask "
                    "chain (and paint alpha)",
        default=True, update=_structural)


class ImpastoMask(bpy.types.PropertyGroup):
    """Mask state (compiled in phase 1; UI arrives with phase 3).
    name = uid."""
    label: StringProperty(name="Name", default="Mask")
    mask_type: EnumProperty(
        items=(('IMAGE', "Image", "Painted grayscale image mask"),),
        default='IMAGE')
    image_name: StringProperty(update=_structural)
    uv_map: StringProperty(update=_structural)
    blend: EnumProperty(
        items=(('MULTIPLY', "Multiply", ""),),
        default='MULTIPLY', update=_structural)
    invert: BoolProperty(default=False, update=_uniform)
    opacity: FloatProperty(default=1.0, min=0.0, max=1.0,
                           subtype='FACTOR', update=_uniform)
    visible: BoolProperty(default=True, update=_structural)


class ImpastoLayer(bpy.types.PropertyGroup):
    """One stack layer. name = uid; label is the user-facing name."""
    label: StringProperty(name="Name", default="Layer")
    layer_type: EnumProperty(
        items=(('PAINT', "Paint", "Painted image layer"),
               ('FILL', "Fill", "Constant color/value layer"),
               ('GROUP', "Group", "Organizational group "
                "(pass-through)")),
        default='PAINT')
    parent_uid: StringProperty(update=_structural)
    visible: BoolProperty(
        name="Visible", default=True, update=_toggle)
    opacity: FloatProperty(
        name="Opacity", default=1.0, min=0.0, max=1.0,
        subtype='FACTOR', update=_uniform)
    blend_mode: EnumProperty(
        name="Blend", items=_blend_items(), default='MIX',
        update=_structural)
    image_name: StringProperty(update=_structural)
    uv_map: StringProperty(update=_structural)
    bindings: CollectionProperty(type=ImpastoBinding)
    masks: CollectionProperty(type=ImpastoMask)


class ImpastoChannel(bpy.types.PropertyGroup):
    """A registry channel enabled on this stack. name = channel key."""
    enabled: BoolProperty(default=True, update=_structural)


def _active_index_update(self, context):
    if 0 <= self.active_index < len(self.layers):
        self.active_layer_uid = self.layers[self.active_index].name
    else:
        self.active_layer_uid = ""
    # Selection switches the native canvas but never forces a mode change.
    # A callback cannot report errors usefully, so invalid/non-paint targets
    # are left for the explicit paint operator to explain.
    layer = self.active_layer()
    if layer is not None and layer.layer_type == 'PAINT':
        try:
            from . import paint
            paint.activate_paint_target(context, layer)
        except paint.PaintTargetError:
            pass


class ImpastoStack(bpy.types.PropertyGroup):
    """Root stack state, stored on the generated root node group's
    tree. NO halt/batch flags here — batching is runtime-only
    (design §4.7)."""
    is_stack: BoolProperty(default=False)
    schema_version: IntProperty(default=model.SCHEMA_VERSION)
    blender_version: IntVectorProperty(size=3)
    channels: CollectionProperty(type=ImpastoChannel)
    layers: CollectionProperty(type=ImpastoLayer)
    active_layer_uid: StringProperty()
    # presentation-order UI slot for template_list; the uid above is
    # the source of truth for every cross-reference.
    active_index: IntProperty(default=-1, update=_active_index_update)

    def active_layer(self):
        for ly in self.layers:
            if ly.name == self.active_layer_uid:
                return ly
        return None


class ImpastoMaterialState(bpy.types.PropertyGroup):
    """Minimal per-material bookkeeping: only the Principled links we
    displaced (so removing the stack restores the material) and the
    stack tree name."""
    displaced_links: StringProperty(default="")   # JSON
    stack_tree: StringProperty(default="")


_classes = (
    ImpastoBinding,
    ImpastoMask,
    ImpastoLayer,
    ImpastoChannel,
    ImpastoStack,
    ImpastoMaterialState,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.ShaderNodeTree.impasto = PointerProperty(type=ImpastoStack)
    bpy.types.Material.impasto_mat = PointerProperty(
        type=ImpastoMaterialState)


def unregister():
    del bpy.types.Material.impasto_mat
    del bpy.types.ShaderNodeTree.impasto
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
