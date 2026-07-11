# SPDX-License-Identifier: GPL-2.0-or-later
"""GPU Paint Spike — feasibility probe: can brush dabs be rasterized
into a texture ON the GPU from a Python modal operator at interactive
rates, with occlusion-correct 3D projection and live viewport feedback,
paying CPU cost only once per stroke?

RESEARCH PROTOTYPE. Not a paint tool. See FINDINGS.md for the spike
question, probe results, measurements and verdict; README.md for the
exact measurement protocol.
"""

bl_info = {
    "name": "GPU Paint Spike (Experimental)",
    "author": "Teo Asinari",
    "version": (0, 3, 0),
    "blender": (5, 1, 0),
    "location": "3D Viewport > Sidebar (N) > GPU Paint tab; "
                "Object menu > GPU Paint Spike",
    "description": "Feasibility spike: GPU-rasterized texture painting "
                   "via the Python gpu module (instrumented, "
                   "experimental, not a tool)",
    "category": "Development",
}

import time

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty)

if "engine" in locals():
    import importlib
    engine = importlib.reload(engine)
else:
    from . import engine


IMAGE_NAME_FMT = "GPUPaintSpike_%d"
IMAGE_NAME_CH_FMT = "GPUPaintSpike_%d_ch%d"


def _ensure_image(size, channel=0):
    """The spike's target Image datablock for one channel at the
    requested resolution (created or rebuilt on size mismatch).
    Channel 0 keeps the v0.2.0 name so old sessions' images are
    reused."""
    name = (IMAGE_NAME_FMT % size if channel == 0
            else IMAGE_NAME_CH_FMT % (size, channel))
    img = bpy.data.images.get(name)
    if img is not None and tuple(img.size) != (size, size):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, size, size, alpha=True)
        # Mid-grey seed so both the painted color and the untouched
        # background are visible in the preview and the Image editor.
        # Extra channels seed black (they accumulate from empty).
        img.generated_color = ((0.5, 0.5, 0.5, 1.0) if channel == 0
                               else (0.0, 0.0, 0.0, 0.0))
    return img


def _ensure_images(size, channels):
    """One Image datablock per paint channel (channel 0 first)."""
    return [_ensure_image(size, i) for i in range(channels)]


class OBJECT_OT_gpu_paint_spike(bpy.types.Operator):
    """Start the GPU paint spike on the active mesh (research prototype;
    paint with LMB, stop with RMB or Esc)"""
    bl_idname = "object.gpu_paint_spike"
    bl_label = "GPU Paint Spike"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if engine.session_active():
            return False   # one session at a time
        obj = context.active_object
        return (obj is not None and obj.type == 'MESH'
                and obj.mode == 'OBJECT'
                and obj.data.uv_layers.active is not None)

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'},
                        "Run from a 3D Viewport (N-panel > GPU Paint)")
            return {'CANCELLED'}
        # The modal loop needs region-relative mouse coordinates; find
        # the WINDOW region of the invoking area (the panel button
        # invokes from the UI region, so context.region is not it).
        region = None
        for r in context.area.regions:
            if r.type == 'WINDOW':
                region = r
                break
        if region is None:
            self.report({'WARNING'}, "No drawable viewport region found")
            return {'CANCELLED'}

        wm = context.window_manager
        size = int(wm.gpu_paint_spike_resolution)
        channels = int(wm.gpu_paint_spike_channels)
        images = _ensure_images(size, channels)
        obj = context.active_object
        if not engine.start_session(obj, images, region, channels=channels):
            self.report({'WARNING'}, "Mesh has no UVs / no faces")
            return {'CANCELLED'}

        self._region = region
        self._stopping = False
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        region.tag_redraw()
        return {'RUNNING_MODAL'}

    # -- helpers ----------------------------------------------------------

    def _mouse_region(self, event):
        return (event.mouse_x - self._region.x,
                event.mouse_y - self._region.y)

    def _inside_region(self, event):
        rx, ry = self._mouse_region(event)
        return (0 <= rx < self._region.width
                and 0 <= ry < self._region.height)

    def _apply_pending_sync(self):
        """Write a finished stroke's readback into the Image datablocks
        — one per channel (CPU side of the sync-back; timed
        separately, totals across channels)."""
        pending = engine.take_pending_pixels()
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
        engine.record_sync_stats(write_ms, update_ms)
        # Repaint the panel stats and any Image editors showing the map.
        for area in bpy.context.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                area.tag_redraw()

    def _finish(self, context):
        engine.stop_session()
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._region is not None:
            self._region.tag_redraw()
        return {'FINISHED'}

    # -- modal loop --------------------------------------------------------

    def modal(self, context, event):
        if not engine.session_active():
            return self._finish(context)

        # Image writes must happen here, never in a draw callback.
        self._apply_pending_sync()

        if self._stopping:
            if not engine.busy():
                return self._finish(context)
            self._region.tag_redraw()   # keep draw callbacks pumping
            return {'RUNNING_MODAL'}

        if engine.last_error() is not None:
            # Latched GPU failure: stop cleanly; the console has the
            # traceback and the panel shows the error row.
            self.report({'WARNING'},
                        "GPU paint spike failed — see console")
            return self._finish(context)

        etype = event.type
        if etype == 'LEFTMOUSE':
            if event.value == 'PRESS' and self._inside_region(event):
                rx, ry = self._mouse_region(event)
                engine.begin_stroke(rx, ry, event.pressure)
                self._region.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and engine.stroke_active():
                engine.end_stroke()
                self._region.tag_redraw()
                return {'RUNNING_MODAL'}
        elif etype == 'MOUSEMOVE' and engine.stroke_active():
            rx, ry = self._mouse_region(event)
            wm = context.window_manager
            engine.move_stroke(rx, ry, event.pressure,
                               wm.gpu_paint_spike_radius)
            self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        elif etype in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            if engine.stroke_active():
                engine.end_stroke()
            self._stopping = True
            self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        elif etype == 'TIMER':
            return {'RUNNING_MODAL'}

        # Everything else (orbit, zoom, pan, N-panel clicks while not
        # stroking) passes through; the depth prepass re-renders itself
        # at the next draw after any view change.
        return {'PASS_THROUGH'}

    def cancel(self, context):
        engine.stop_session()
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


class VIEW3D_PT_gpu_paint_spike(bpy.types.Panel):
    bl_label = "GPU Paint Spike"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GPU Paint"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        col = layout.column(align=True)
        col.label(text="Research prototype — see FINDINGS.md",
                  icon='EXPERIMENTAL')

        if engine.session_active():
            box = layout.box()
            box.label(text="Painting… LMB paints, RMB/Esc stops",
                      icon='BRUSH_DATA')
            err = engine.last_error()
            if err:
                box.label(text="Draw failed — see console", icon='ERROR')
        else:
            layout.operator(OBJECT_OT_gpu_paint_spike.bl_idname,
                            text="Start GPU Paint", icon='PLAY')

        col = layout.column(align=True)
        col.enabled = not engine.session_active()
        col.prop(wm, "gpu_paint_spike_resolution", text="Texture")
        col.prop(wm, "gpu_paint_spike_channels", text="Channels")
        col = layout.column(align=True)
        col.prop(wm, "gpu_paint_spike_radius")
        col.prop(wm, "gpu_paint_spike_hardness")
        col.prop(wm, "gpu_paint_spike_strength")
        col.prop(wm, "gpu_paint_spike_color", text="")
        col.prop(wm, "gpu_paint_spike_occlusion")
        col.prop(wm, "gpu_paint_spike_subrect")
        col.prop(wm, "gpu_paint_spike_preview_channel")

        stats = engine.last_stroke_stats()
        box = layout.box()
        box.label(text="Last stroke", icon='TIME')
        if not stats:
            box.label(text="No stroke measured yet")
        else:
            col = box.column(align=True)
            for key, label, fmt in engine.STATS_LAYOUT:
                if key in stats:
                    try:
                        value = fmt % stats[key]
                    except (TypeError, ValueError):
                        value = str(stats[key])
                    row = col.row()
                    row.label(text=label)
                    row.label(text=value)


def _menu_func(self, context):
    self.layout.operator(OBJECT_OT_gpu_paint_spike.bl_idname,
                         text="GPU Paint Spike (Experimental)")


_CLASSES = (
    OBJECT_OT_gpu_paint_spike,
    VIEW3D_PT_gpu_paint_spike,
)


def register():
    wm = bpy.types.WindowManager
    wm.gpu_paint_spike_radius = FloatProperty(
        name="Radius",
        description="Brush radius in screen pixels",
        default=50.0, min=2.0, max=500.0, subtype='PIXEL')
    wm.gpu_paint_spike_hardness = FloatProperty(
        name="Hardness",
        description="Fraction of the radius painted at full strength "
                    "before the falloff begins",
        default=0.5, min=0.0, max=1.0)
    wm.gpu_paint_spike_strength = FloatProperty(
        name="Strength",
        description="Dab opacity; multiplied by tablet pressure",
        default=1.0, min=0.0, max=1.0)
    wm.gpu_paint_spike_color = FloatVectorProperty(
        name="Color", description="Brush color",
        subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(0.9, 0.2, 0.1))
    wm.gpu_paint_spike_occlusion = BoolProperty(
        name="Occlusion Test",
        description="Reject dab texels hidden behind the mesh (depth "
                    "prepass); disable to diagnose depth issues",
        default=True)
    wm.gpu_paint_spike_resolution = EnumProperty(
        name="Texture Size",
        description="Paint texture resolution (pick each size to fill "
                    "the readback measurement table in FINDINGS.md)",
        items=(('1024', "1024 (1K)", ""),
               ('2048', "2048 (2K)", ""),
               ('4096', "4096 (4K)", "")),
        default='2048')
    wm.gpu_paint_spike_channels = EnumProperty(
        name="Channels",
        description="Paint channel count: N RGBA16F targets on one "
                    "framebuffer (MRT); every dab writes all channels "
                    "through the same falloff mask (pick each count to "
                    "fill the multi-channel tables in FINDINGS.md)",
        items=(('1', "1", "Single channel (v0.2.0 baseline path)"),
               ('2', "2", ""),
               ('4', "4", ""),
               ('8', "8", "")),
        default='1')
    wm.gpu_paint_spike_subrect = BoolProperty(
        name="Sub-rect Readback",
        description="Read back only the stroke's conservative dirty "
                    "rect (per-triangle screen bbox vs dab bbox) "
                    "instead of the full texture; disable to force "
                    "full-frame reads for the baseline measurement",
        default=True)
    wm.gpu_paint_spike_preview_channel = IntProperty(
        name="Preview Channel",
        description="Which paint channel the viewport preview shows "
                    "(clamped to the session's channel count)",
        default=0, min=0, max=engine.MAX_CHANNELS - 1)

    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_object.append(_menu_func)


def unregister():
    engine.stop_session()
    bpy.types.VIEW3D_MT_object.remove(_menu_func)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    wm = bpy.types.WindowManager
    for name in ("gpu_paint_spike_radius", "gpu_paint_spike_hardness",
                 "gpu_paint_spike_strength", "gpu_paint_spike_color",
                 "gpu_paint_spike_occlusion",
                 "gpu_paint_spike_resolution",
                 "gpu_paint_spike_channels",
                 "gpu_paint_spike_subrect",
                 "gpu_paint_spike_preview_channel"):
        try:
            delattr(wm, name)
        except AttributeError:
            pass


if __name__ == "__main__":
    register()
