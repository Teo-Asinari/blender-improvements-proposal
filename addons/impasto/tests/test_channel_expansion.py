# SPDX-License-Identifier: GPL-2.0-or-later
"""Post-creation Emission/Subsurface channel expansion regression."""

import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import compat, engine, model


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


EMISSION = ("emission_color", "emission_strength")
SUBSURFACE = ("sss_weight", "sss_radius", "sss_scale",
              )
REGISTER_ONLY = ("sss_ior", "sss_anisotropy")


try:
    impasto.register()
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    check("default Standard stack", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {'FINISHED'})
    check("selected Paint layer", bpy.ops.impasto.layer_add(
        layer_type='PAINT', canvas_size='1024') == {'FINISHED'})
    mat = obj.active_material
    tree = engine.find_stack_for_material(mat)
    layer = tree.impasto.active_layer()
    original_binding = layer.bindings.get("base_color")
    original_image = original_binding.image_name
    original_pixels = tuple(bpy.data.images[original_image].pixels[:4])

    for key in EMISSION + SUBSURFACE:
        check("add %s after stack creation" % key,
              bpy.ops.impasto.channel_add(
                  channel_key=key, bind_active_layer=True) == {'FINISHED'})
    for key in REGISTER_ONLY:
        check("register non-paintable %s without a canvas" % key,
              bpy.ops.impasto.channel_add(
                  channel_key=key, bind_active_layer=False) == {'FINISHED'})

    expected = set(EMISSION + SUBSURFACE)
    registered_expected = expected | set(REGISTER_ONLY)
    check("expanded channels registered",
          registered_expected.issubset(
              {item.name for item in tree.impasto.channels}))
    check("expanded channels bound to selected Paint layer",
          expected.issubset({item.name for item in layer.bindings}))
    check("every added Paint binding owns a canvas",
          all(layer.bindings[key].image_name in bpy.data.images
              for key in expected))
    check("canvases inherit existing layer resolution",
          all(tuple(bpy.data.images[layer.bindings[key].image_name].size)
              == (1024, 1024) for key in expected))
    check("color/data canvas colorspaces follow channel domains",
          bpy.data.images[layer.bindings["emission_color"].image_name]
              .colorspace_settings.name == 'sRGB'
          and all(bpy.data.images[layer.bindings[key].image_name]
                  .colorspace_settings.name == 'Non-Color'
                  for key in expected - {"emission_color"}))
    check("vector/scalar channel types remain registered",
          model.CHANNEL_MAP["sss_radius"].kind == 'VECTOR'
          and all(model.CHANNEL_MAP[key].kind == 'SCALAR'
                  for key in expected - {"emission_color", "sss_radius"}))
    check("IOR and anisotropy remain register-only",
          all(layer.bindings.get(key) is None for key in REGISTER_ONLY))

    principled = compat.find_principled(mat.node_tree)
    check("compiler links expanded channels to Principled",
          all(compat.find_socket(principled.inputs,
                                 model.CHANNEL_MAP[key].socket).is_linked
              for key in registered_expected))

    images_before_duplicate = set(bpy.data.images.keys())
    bindings_before_duplicate = tuple(item.name for item in layer.bindings)
    for key in EMISSION + SUBSURFACE:
        check("duplicate %s add is safe" % key,
              bpy.ops.impasto.channel_add(
                  channel_key=key, bind_active_layer=True) == {'FINISHED'})
    check("duplicate adds create no images or bindings",
          set(bpy.data.images.keys()) == images_before_duplicate
          and tuple(item.name for item in layer.bindings)
          == bindings_before_duplicate)
    check("existing canvas identity and pixels survive expansion",
          layer.bindings["base_color"].image_name == original_image
          and tuple(bpy.data.images[original_image].pixels[:4])
          == original_pixels)

    tree_name, mat_name, layer_uid = tree.name, mat.name, layer.name
    bound_images = {key: layer.bindings[key].image_name for key in expected}
    path = os.path.join(tempfile.gettempdir(),
                        "impasto_channel_expansion_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
    tree = bpy.data.node_groups.get(tree_name)
    mat = bpy.data.materials.get(mat_name)
    layer = tree.impasto.layers.get(layer_uid)
    check("expanded registry and bindings persist after reopen",
          registered_expected.issubset(
              {item.name for item in tree.impasto.channels})
          and expected.issubset({item.name for item in layer.bindings}))
    check("expanded canvas identities persist after reopen",
          all(layer.bindings[key].image_name == name
              and bpy.data.images.get(name) is not None
              for key, name in bound_images.items()))
    principled = compat.find_principled(mat.node_tree)
    check("expanded Principled links persist after reopen",
          all(compat.find_socket(principled.inputs,
                                 model.CHANNEL_MAP[key].socket).is_linked
              for key in registered_expected))
    print("IMPASTO_CHANNEL_EXPANSION_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_CHANNEL_EXPANSION_FAILED")
