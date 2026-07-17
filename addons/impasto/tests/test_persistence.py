# SPDX-License-Identifier: GPL-2.0-or-later
"""Save/reopen persistence and load-time self-heal in real Blender."""

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
from impasto import engine, model


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


try:
    impasto.register()
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    check("stack init", bpy.ops.impasto.stack_init(template="PRINCIPLED_STANDARD") == {"FINISHED"})
    mat = obj.active_material
    tree = engine.find_stack_for_material(mat)
    check("stack state available", tree is not None and len(tree.impasto.channels) == 5)
    # Build representative stored state directly. Operator undo semantics are
    # covered separately; this test isolates serialization and load_post.
    with engine.stack_edit_session(tree):
        fill = tree.impasto.layers.add()
        fill.name = "aa11bb22"
        fill.label = "Persisted Fill"
        fill.layer_type = "FILL"
        binding = fill.bindings.add()
        binding.name = "base_color"
        binding.mode = "COLOR"
        paint = tree.impasto.layers.add()
        paint.name = "c3a91f02"
        paint.label = "Persisted Paint"
        paint.layer_type = "PAINT"
        paint.paint_emission_color = (0.2, 0.4, 0.8)
        paint.paint_emission_strength = 9.5
        paint.paint_sss_weight = 0.65
        paint.paint_sss_radius = (1.4, 0.35, 0.12)
        paint.paint_sss_scale = 0.025
        stencil_image = bpy.data.images.new("Persisted Brush Stencil", 8, 8,
                                            alpha=True)
        paint.brush_stencil_enabled = True
        paint.brush_stencil_image = stencil_image
        paint.brush_stencil_projection = 'BRUSH_ALPHA'
        paint.brush_stencil_interpretation = 'LUMINANCE'
        paint.brush_stencil_usage = 'NORMAL_PROFILE'
        paint.brush_stencil_opacity = 0.7
        paint.brush_stencil_position = (0.25, 0.75)
        paint.brush_stencil_scale = (0.2, 0.3)
        paint.brush_stencil_brush_scale = (1.2, 0.8)
        paint.brush_stencil_rotation = 0.4
        paint.brush_stencil_profile_strength = 2.5
        paint.brush_stencil_profile_invert = True
        binding = paint.bindings.add()
        binding.name = "roughness"
        canvas = bpy.data.images.new("Persisted Roughness Canvas", 8, 8,
                                     alpha=True)
        binding.image_name = canvas.name
    tree_name = tree.name
    uids = tuple(ly.name for ly in tree.impasto.layers)
    types = tuple(ly.layer_type for ly in tree.impasto.layers)
    bindings = tuple(tuple(b.name for b in ly.bindings) for ly in tree.impasto.layers)
    binding_images = tuple(tuple(b.image_name for b in ly.bindings)
                           for ly in tree.impasto.layers)
    mat_name = mat.name

    victim_name = model.n_root_out()
    victim = tree.nodes.get(victim_name)
    check("self-heal victim exists before tamper", victim is not None)
    tree.nodes.remove(victim)
    check("victim removed", tree.nodes.get(victim_name) is None)
    path = os.path.join(tempfile.gettempdir(), "impasto_persistence_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False)
    check("blend saved", os.path.exists(path))
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)

    mat = bpy.data.materials.get(mat_name)
    tree = bpy.data.node_groups.get(tree_name)
    check("material persisted", mat is not None)
    check("stack tree persisted", tree is not None and tree.impasto.is_stack)
    check("stack rediscovered", engine.find_stack_for_material(mat) is tree)
    check("UID order persisted", tuple(ly.name for ly in tree.impasto.layers) == uids)
    check("layer types persisted", tuple(ly.layer_type for ly in tree.impasto.layers) == types)
    check("bindings persisted", tuple(tuple(b.name for b in ly.bindings) for ly in tree.impasto.layers) == bindings)
    check("per-binding images persisted",
          tuple(tuple(b.image_name for b in ly.bindings)
                for ly in tree.impasto.layers) == binding_images)
    paint = next(ly for ly in tree.impasto.layers if ly.name == "c3a91f02")
    check("emission brush values persist across save/reopen",
          all(abs(a - b) < 1e-6 for a, b in zip(
              paint.paint_emission_color, (0.2, 0.4, 0.8)))
          and abs(paint.paint_emission_strength - 9.5) < 1e-6)
    check("subsurface brush units persist across save/reopen",
          abs(paint.paint_sss_weight - 0.65) < 1e-6
          and all(abs(a - b) < 1e-6 for a, b in zip(
              paint.paint_sss_radius, (1.4, 0.35, 0.12)))
          and abs(paint.paint_sss_scale - 0.025) < 1e-6)
    check("image stencil state persists across save/reopen",
          paint.brush_stencil_enabled
          and paint.brush_stencil_image is not None
          and paint.brush_stencil_image.name == "Persisted Brush Stencil"
          and paint.brush_stencil_projection == 'BRUSH_ALPHA'
          and paint.brush_stencil_interpretation == 'LUMINANCE'
          and paint.brush_stencil_usage == 'NORMAL_PROFILE'
          and abs(paint.brush_stencil_opacity - 0.7) < 1e-6
          and all(abs(a - b) < 1e-6 for a, b in zip(
              paint.brush_stencil_position, (0.25, 0.75)))
          and all(abs(a - b) < 1e-6 for a, b in zip(
              paint.brush_stencil_scale, (0.2, 0.3)))
          and all(abs(a - b) < 1e-6 for a, b in zip(
              paint.brush_stencil_brush_scale, (1.2, 0.8)))
          and abs(paint.brush_stencil_rotation - 0.4) < 1e-6
          and abs(paint.brush_stencil_profile_strength - 2.5) < 1e-6
          and paint.brush_stencil_profile_invert)
    check("load handler self-healed removed node", tree.nodes.get(victim_name) is not None)
    check("self-healed graph converges cleanly", not engine.reconcile_stack(tree).errors)
    print("IMPASTO_PERSISTENCE_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_PERSISTENCE_FAILED")
