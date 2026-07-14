# SPDX-License-Identifier: GPL-2.0-or-later
"""Persistent GPU preview state and resident-session UI routing checks."""

import sys
import traceback
from pathlib import Path
from types import SimpleNamespace as NS

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import engine, gpu_engine, ops, props


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


try:
    impasto.register()
    expected = {
        'LIT_PBR', 'RAW_TANGENT_NORMAL',
        'NEUTRAL_NORMAL_LIGHTING', 'HEIGHT_GRAYSCALE',
    }
    check("preview enum identifiers are stable",
          props.GPU_PREVIEW_MODE_IDS == expected)

    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {'FINISHED'})
    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {'FINISHED'})
    check("roughness binding add", bpy.ops.impasto.binding_add(
        channel_key="roughness") == {'FINISHED'})
    check("metallic binding add", bpy.ops.impasto.binding_add(
        channel_key="metallic") == {'FINISHED'})
    tree = engine.find_stack_for_material(obj.active_material)
    layer = tree.impasto.active_layer()
    check("new layers default to composed PBR preview",
          layer.gpu_preview_mode == 'LIT_PBR')
    rna = layer.bl_rna.properties['gpu_preview_mode']
    check("preview property is persistent RNA state",
          not rna.is_skip_save
          and {item.identifier for item in rna.enum_items} == expected)
    for mode in expected:
        layer.gpu_preview_mode = mode
        check("preview helper returns " + mode,
              ops.gpu_preview_mode(layer) == mode)
    check("invalid compatibility input falls back to Lit PBR",
          ops.gpu_preview_mode(NS(gpu_preview_mode="OLD_RAW_CHANNEL"))
          == 'LIT_PBR')

    targets = ops.gpu_paint_targets(layer)
    keys = tuple(key for key, _image in targets)
    images = [image for _key, image in targets]
    brush = ops._gpu_brush(layer)
    layer.gpu_preview_mode = 'RAW_TANGENT_NORMAL'
    check("headless resident session accepts persistent preview mode",
          gpu_engine.start_session(
              obj, images, None,
              payloads=gpu_engine.stroke_payloads(keys, brush),
              settings={"channel_keys": keys,
                        "preview_mode": ops.gpu_preview_mode(layer)}))
    check("session normalizes and exposes selected preview",
          gpu_engine.current_preview_mode() == 'RAW_TANGENT_NORMAL')

    # Construct only the non-GPU modal state needed by the helpers. The
    # headless session remains resident and no Image sync is requested.
    redraw = {"count": 0}
    region = NS(type='WINDOW', x=0, y=0, width=640, height=480,
                tag_redraw=lambda: redraw.__setitem__(
                    "count", redraw["count"] + 1))
    # Blender RNA operator classes cannot be directly constructed. A plain
    # namespace exercises these deliberately isolated runtime helpers.
    operator = NS(
        _region=region,
        _area=NS(regions=[region]),
        _tree_name=tree.name,
        _layer_uid=layer.name,
        _channel_keys=keys,
        _preview_mode='RAW_TANGENT_NORMAL',
        _stopping=False,
        _timer=None,
        _apply_pending_sync=lambda: None,
        report=lambda *_args: None,
    )
    operator._mouse_region = lambda event: \
        ops.IMPASTO_OT_gpu_paint._mouse_region(operator, event)
    operator._inside_region = lambda event: \
        ops.IMPASTO_OT_gpu_paint._inside_region(operator, event)
    operator._over_interface_region = lambda event: \
        ops.IMPASTO_OT_gpu_paint._over_interface_region(operator, event)

    layer.gpu_preview_mode = 'HEIGHT_GRAYSCALE'
    check("live preview edit applies without restarting session",
          ops.IMPASTO_OT_gpu_paint._refresh_preview_mode(operator)
          and gpu_engine.current_preview_mode() == 'HEIGHT_GRAYSCALE'
          and gpu_engine.session_active())
    check("preview edit queues no CPU image synchronization",
          gpu_engine.take_pending_pixels() is None)

    # Sidebar lies outside the 3D WINDOW region. Its clicks must reach normal
    # Blender UI while the modal painter remains active.
    sidebar_click = NS(type='LEFTMOUSE', value='PRESS',
                       mouse_x=700, mouse_y=100,
                       pressure=1.0, ctrl=False, shift=False)
    check("outside-region sidebar clicks pass through resident modal",
          ops.IMPASTO_OT_gpu_paint.modal(
              operator, bpy.context, sidebar_click) == {'PASS_THROUGH'}
          and gpu_engine.session_active())

    # Blender can configure its N-panel as an overlapping region whose bounds
    # still lie inside the WINDOW rectangle. It must win over paint hit-testing.
    overlay_sidebar = NS(type='UI', x=480, y=0, width=160, height=480)
    operator._area = NS(regions=[region, overlay_sidebar])
    overlapping_click = NS(type='LEFTMOUSE', value='PRESS',
                           mouse_x=550, mouse_y=100,
                           pressure=1.0, ctrl=False, shift=False)
    check("overlapping N-panel clicks pass through resident modal",
          ops.IMPASTO_OT_gpu_paint.modal(
              operator, bpy.context, overlapping_click) == {'PASS_THROUGH'}
          and not gpu_engine.stroke_active()
          and gpu_engine.session_active())

    pause_event = NS(type='P', value='PRESS', mouse_x=100, mouse_y=100,
                     pressure=1.0, ctrl=False, shift=False)
    check("P pauses dab capture without ending resident session",
          ops.IMPASTO_OT_gpu_paint.modal(
              operator, bpy.context, pause_event) == {'RUNNING_MODAL'}
          and gpu_engine.input_paused() and gpu_engine.session_active()
          and gpu_engine.take_pending_pixels() is None)
    paused_canvas_click = NS(type='LEFTMOUSE', value='PRESS',
                             mouse_x=100, mouse_y=100,
                             pressure=1.0, ctrl=False, shift=False)
    check("paused session passes even canvas clicks to Blender UI",
          ops.IMPASTO_OT_gpu_paint.modal(
              operator, bpy.context, paused_canvas_click) == {'PASS_THROUGH'}
          and not gpu_engine.stroke_active())
    check("P resumes dab capture with resident state intact",
          ops.IMPASTO_OT_gpu_paint.modal(
              operator, bpy.context, pause_event) == {'RUNNING_MODAL'}
          and not gpu_engine.input_paused() and gpu_engine.session_active())

    # All editable values are read at the next pen-down. This helper call is
    # the same path the modal uses immediately before begin_stroke().
    layer.paint_color = (0.11, 0.22, 0.33)
    layer.paint_roughness = 0.81
    layer.paint_metallic = 0.67
    layer.brush_radius = 93.0
    layer.brush_hardness = 0.27
    layer.brush_opacity = 0.42
    ops.IMPASTO_OT_gpu_paint._refresh_stroke_settings(
        operator, bpy.context)
    payloads, settings = gpu_engine.stroke_settings_snapshot()
    values = dict(zip(keys, payloads))
    check("next stroke sees edited PBR channel values",
          values['base_color']['value'] != (0.8, 0.2, 0.1)
          and all(abs(v - 0.81) < 1e-6
                  for v in values['roughness']['value'])
          and all(abs(v - 0.67) < 1e-6
                  for v in values['metallic']['value']))
    check("next stroke sees edited radius and hardness without restart",
          abs(settings['radius'] - 93.0) < 1e-6
          and abs(settings['hardness'] - 0.27) < 1e-6
          and (settings['brush_stamp'] is None
               or abs(settings['brush_stamp'].radius_px - 93.0) < 1e-6)
          and gpu_engine.session_active())
    check("next stroke sees explicit GPU opacity",
          abs(settings['opacity'] - 0.42) < 1e-6)
    check("between-stroke edits still queue no image sync",
          gpu_engine.take_pending_pixels() is None)

    inspect_event = NS(type='V', value='PRESS', mouse_x=100, mouse_y=100,
                       pressure=1.0, ctrl=False, shift=False)
    check("V enters authoritative material inspection without session exit",
          ops.IMPASTO_OT_gpu_paint.modal(
              operator, bpy.context, inspect_event) == {'RUNNING_MODAL'}
          and gpu_engine.material_inspect_active()
          and gpu_engine.input_paused() and gpu_engine.session_active())
    check("V returns directly to resident GPU painting",
          ops.IMPASTO_OT_gpu_paint.modal(
              operator, bpy.context, inspect_event) == {'RUNNING_MODAL'}
          and not gpu_engine.material_inspect_active()
          and not gpu_engine.input_paused() and gpu_engine.session_active())

    gpu_engine.stop_session()
    impasto.unregister()
    print("IMPASTO_PREVIEW_MODES_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_PREVIEW_MODES_FAILED")
