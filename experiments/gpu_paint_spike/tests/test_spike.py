# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test suite for the GPU paint spike (run inside
``blender --background --python``).

Structurally limited by design: there is NO GPU in --background (gpu
object creation raises SystemError — probed on 5.1.2, and re-asserted
here so the environment fact stays locked). What CAN be verified
headless: registration lifecycle, pure math (falloff, dab spacing/
interpolation), mesh-soup extraction, create-info descriptor
population, GLSL structural consistency, and the engine's lazy/guarded
session lifecycle. The GUI measurement protocol in README.md is the
real test of the spike question.

Prints SPIKE_TESTS_PASSED on success.
"""

import ast
import inspect
import os
import sys
import traceback

import bpy

_SPIKE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPERIMENTS_ROOT = os.path.dirname(_SPIKE_DIR)
if _EXPERIMENTS_ROOT not in sys.path:
    sys.path.insert(0, _EXPERIMENTS_ROOT)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def main():
    import gpu_paint_spike
    from gpu_paint_spike import engine

    # -- environment facts the whole design leans on -----------------------
    import gpu
    check("background mode", bpy.app.background)
    try:
        gpu.types.GPUTexture((4, 4), format='RGBA16F')
        check("gpu raises headless", False, "GPUTexture created?!")
    except SystemError:
        check("gpu raises headless (SystemError)", True)
    except Exception as e:
        check("gpu raises headless (SystemError)", False, repr(e))
    try:
        gpu.types.GPUShader("void main(){}", "void main(){}")
        check("legacy GPUShader removed", False, "constructed?!")
    except TypeError:
        check("legacy GPUShader removed (TypeError)", True)
    except Exception as e:
        check("legacy GPUShader removed (TypeError)", False, repr(e))

    # -- pure math ----------------------------------------------------------
    f = engine.brush_falloff
    check("falloff center", f(0.0, 0.5) == 1.0)
    check("falloff rim", f(1.0, 0.5) == 0.0)
    check("falloff core", f(0.49, 0.5) == 1.0)
    check("falloff mid in (0,1)", 0.0 < f(0.75, 0.5) < 1.0)
    seq = [f(t / 20.0, 0.3) for t in range(21)]
    check("falloff monotonic",
          all(a >= b for a, b in zip(seq, seq[1:])), str(seq))
    check("falloff hardness 1 clamped", f(0.999, 1.0) == 1.0)

    dabs, leftover = engine.interpolate_dabs(0, 0, 25, 0, 10.0, 0.0)
    check("interp count", len(dabs) == 2, str(dabs))
    check("interp positions",
          abs(dabs[0][0] - 10.0) < 1e-6 and abs(dabs[1][0] - 20.0) < 1e-6,
          str(dabs))
    check("interp leftover", abs(leftover - 5.0) < 1e-6, str(leftover))
    dabs2, leftover2 = engine.interpolate_dabs(25, 0, 32, 0, 10.0, leftover)
    check("interp carries leftover",
          len(dabs2) == 1 and abs(dabs2[0][0] - 30.0) < 1e-6,
          str((dabs2, leftover2)))
    dabs3, leftover3 = engine.interpolate_dabs(1, 1, 1, 1, 10.0, 3.0)
    check("interp zero distance", dabs3 == [] and leftover3 == 3.0)
    check("dab spacing floor",
          engine.dab_spacing(1.0) == engine.MIN_DAB_SPACING_PX)
    check("dab spacing factor",
          engine.dab_spacing(100.0) == 100.0 * engine.DAB_SPACING_FACTOR)

    # -- Buffer->numpy conversion ladder (pure logic; a real gpu Buffer
    #    needs a GPU context, so stand-ins exercise each rung: ndarray
    #    has __array_interface__, bytearray exports the C buffer
    #    protocol, ToListOnly mimics a protocol-less Buffer) ------------
    import numpy as np

    names = [n for n, _f in engine.BUFFER_TO_NUMPY_LADDER]
    check("ladder rung order",
          names == ["asarray", "frombuffer", "memoryview",
                    "to_list_fallback"], str(names))
    check("ladder ends in always-correct fallback",
          engine.BUFFER_TO_NUMPY_LADDER[-1][1] is engine._conv_to_list)

    ref = np.arange(16, dtype=np.float32)

    class ToListOnly:
        """Stand-in for a gpu Buffer with no fast conversion path."""
        def to_list(self):
            return ref.reshape(4, 4).tolist()

    out = engine.buffer_to_numpy(ToListOnly(), "to_list_fallback")
    check("to_list rung converts",
          out.dtype == np.float32 and np.array_equal(out, ref), str(out))
    out = engine.buffer_to_numpy(ToListOnly(), "asarray")
    check("failing rung falls back to to_list", np.array_equal(out, ref))
    out = engine.buffer_to_numpy(ToListOnly(), "not_a_rung")
    check("unknown rung falls back to to_list", np.array_equal(out, ref))

    check("asarray rung gated on __array_interface__: ndarray passes",
          np.array_equal(engine._conv_asarray(ref), ref))
    try:
        engine._conv_asarray(bytearray(ref.tobytes()))
        check("asarray rung gated on __array_interface__: bytes rejected",
              False, "converted?!")
    except TypeError:
        check("asarray rung gated on __array_interface__: bytes rejected",
              True)
    raw = bytearray(ref.tobytes())
    check("frombuffer rung converts raw bytes",
          np.array_equal(engine._conv_frombuffer(raw), ref))
    check("memoryview rung converts raw bytes",
          np.array_equal(engine._conv_memoryview(raw), ref))

    check("probe picks asarray for array-interface objects",
          engine.probe_buffer_to_numpy_path(ref.copy(), ref) == "asarray")
    check("probe picks frombuffer for buffer-protocol objects",
          engine.probe_buffer_to_numpy_path(raw, ref) == "frombuffer")
    check("probe falls back for protocol-less objects",
          engine.probe_buffer_to_numpy_path(ToListOnly(), ref)
          == "to_list_fallback")
    check("probe rejects wrong values",
          engine.probe_buffer_to_numpy_path(
              np.zeros(16, dtype=np.float32), ref) == "to_list_fallback")

    # Probe results are GUI-only; headless the latches must hold the
    # safe defaults (never consulted headless — finalize needs a draw).
    check("headless readback latches at safe defaults",
          engine._buffer_numpy_path == "to_list_fallback"
          and engine._read_into_numpy is False)

    # -- finalize structure: ONE production read, A/B behind the flag ----
    check("DEBUG_COMPARE_READS off by default",
          engine.DEBUG_COMPARE_READS is False)
    fin_src = inspect.getsource(engine._finalize_stroke_gpu)
    check("tex.read gated behind DEBUG_COMPARE_READS",
          "if DEBUG_COMPARE_READS" in fin_src
          and fin_src.count("paint_tex.read") == 1)
    check("finalize prefers read-into-numpy",
          "if _read_into_numpy" in fin_src
          and "read_into_numpy" in fin_src)
    check("finalize converts via probed ladder rung",
          "buffer_to_numpy(buf, _buffer_numpy_path)" in fin_src)
    check("finalize no bare np.asarray on the readback buffer",
          "np.asarray(buf" not in fin_src, "0.1.0 slow path resurfaced")
    check("stats layout reports readback_path",
          any(k == "readback_path" for k, _l, _f in engine.STATS_LAYOUT))
    sync_src = inspect.getsource(engine.record_sync_stats)
    check("syncback_total keeps semantics (drain+read+conv+write+update)",
          all(key in sync_src for key in
              ("drain_ms", "fb_read_ms", "to_numpy_ms")))

    # -- mesh soup extraction (default cube has a UV layer) ------------------
    cube = bpy.data.objects.get("Cube")
    check("factory cube present", cube is not None)
    coords, uvs = engine.build_mesh_soup(cube)
    check("soup coords shape", coords is not None
          and coords.shape == (36, 3), str(getattr(coords, "shape", None)))
    check("soup uvs shape", uvs is not None and uvs.shape == (36, 2),
          str(getattr(uvs, "shape", None)))
    check("soup uv range", float(uvs.min()) >= 0.0
          and float(uvs.max()) <= 1.0, "%s..%s" % (uvs.min(), uvs.max()))

    me = bpy.data.meshes.new("spike_nouv")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    nouv_obj = bpy.data.objects.new("spike_nouv", me)
    bpy.context.collection.objects.link(nouv_obj)
    c2, u2 = engine.build_mesh_soup(nouv_obj)
    check("soup no-UV mesh -> None", c2 is None and u2 is None)

    # -- create-info population (pure bookkeeping; headless-safe) -----------
    for name in ("dab_shader_create_info", "prepass_shader_create_info",
                 "preview_shader_create_info"):
        try:
            getattr(engine, name)()
            check("create-info %s builds headless" % name, True)
        except Exception as e:
            check("create-info %s builds headless" % name, False, repr(e))

    # -- GLSL structural checks ----------------------------------------------
    check("dab vert emits UV clip pos",
          "vec4(uv * 2.0 - 1.0" in engine.DAB_VERT_SRC)
    check("dab vert passes worldPos",
          "worldPos" in engine.DAB_VERT_SRC)
    for uniform in ("model_matrix", "view_proj_matrix", "region_size",
                    "brush_center_px", "brush_radius_px", "brush_hardness",
                    "depth_epsilon", "use_occlusion", "brush_color"):
        srcs = engine.DAB_VERT_SRC + engine.DAB_FRAG_SRC
        check("dab uniform %s referenced" % uniform, uniform in srcs)
    check("dab frag samples prepass depth",
          "texture(scene_depth_tex" in engine.DAB_FRAG_SRC)
    check("dab frag falloff matches python mirror",
          "1.0 - smoothstep(h, 1.0, t)" in engine.DAB_FRAG_SRC)
    check("prepass stores post-divide NDC depth",
          "clipPos.z / clipPos.w" in engine.PREPASS_FRAG_SRC)
    check("dab frag compares same NDC quantity",
          "clip.xyz / clip.w" in engine.DAB_FRAG_SRC)
    check("preview has depth bias",
          repr(engine.CLIP_DEPTH_BIAS) in engine.PREVIEW_VERT_SRC)
    for src_name in ("DAB_VERT_SRC", "PREPASS_VERT_SRC",
                     "PREVIEW_VERT_SRC"):
        check("%s writes gl_Position" % src_name,
              "gl_Position" in getattr(engine, src_name))
    for src_name in ("DAB_FRAG_SRC", "PREPASS_FRAG_SRC",
                     "PREVIEW_FRAG_SRC"):
        check("%s writes fragColor" % src_name,
              "fragColor" in getattr(engine, src_name))

    # -- lazy-gpu audit: nothing at engine module level touches gpu ----------
    tree = ast.parse(inspect.getsource(engine))
    offenders = []
    for node in tree.body:   # module level only, deliberately shallow
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                parts = []
                f = sub.func
                while isinstance(f, ast.Attribute):
                    parts.append(f.attr)
                    f = f.value
                if isinstance(f, ast.Name) and f.id == "gpu":
                    offenders.append(node.lineno)
    check("no module-level gpu calls in engine", not offenders,
          str(offenders))

    # -- registration lifecycle ----------------------------------------------
    check("version 0.2.0",
          gpu_paint_spike.bl_info["version"] == (0, 2, 0),
          str(gpu_paint_spike.bl_info["version"]))
    gpu_paint_spike.register()
    check("operator registered",
          hasattr(bpy.types, "OBJECT_OT_gpu_paint_spike"))
    check("panel registered",
          hasattr(bpy.types, "VIEW3D_PT_gpu_paint_spike"))
    wm = bpy.context.window_manager
    check("radius prop", hasattr(wm, "gpu_paint_spike_radius"))
    check("color prop", hasattr(wm, "gpu_paint_spike_color"))
    check("resolution prop",
          wm.gpu_paint_spike_resolution in {'1024', '2048', '4096'})

    op = bpy.types.OBJECT_OT_gpu_paint_spike
    bpy.context.view_layer.objects.active = cube
    check("poll true on UV'd mesh", op.poll(bpy.context))
    bpy.context.view_layer.objects.active = nouv_obj
    check("poll false without UVs", not op.poll(bpy.context))
    bpy.context.view_layer.objects.active = cube

    # Menu entry present (F3 discoverability rides menu search).
    menu_ok = any(getattr(f, "__name__", "") == "_menu_func"
                  for f in bpy.types.VIEW3D_MT_object._dyn_ui_initialize())
    check("object menu entry appended", menu_ok)

    # -- engine session lifecycle headless (gpu never touched: handlers
    #    fail quietly, dabs queue purely, no draw callback ever runs) -------
    from gpu_paint_spike import engine as eng
    img = bpy.data.images.new("spike_test_img", 256, 256, alpha=True)
    check("start_session", eng.start_session(cube, img, None))
    check("session active", eng.session_active())
    check("start blocks poll", not op.poll(bpy.context))
    eng.begin_stroke(10, 10, 1.0)
    check("stroke active", eng.stroke_active())
    eng.move_stroke(60, 10, 0.8, radius_px=40.0)
    s = eng._session
    check("dabs queued", len(s.dab_queue) > 1, str(len(s.dab_queue)))
    eng.end_stroke()
    check("finalize pending", eng.busy())
    check("no pixels without a draw", eng.take_pending_pixels() is None)
    check("no error latched headless", eng.last_error() is None)
    eng.stop_session()
    check("session stopped", not eng.session_active())
    check("stats dict", isinstance(eng.last_stroke_stats(), dict))

    # -- unregister / re-register cycle --------------------------------------
    gpu_paint_spike.unregister()
    check("operator unregistered",
          not hasattr(bpy.types, "OBJECT_OT_gpu_paint_spike"))
    gpu_paint_spike.register()
    check("re-register survives",
          hasattr(bpy.types, "OBJECT_OT_gpu_paint_spike"))
    gpu_paint_spike.unregister()

    if FAILURES:
        print("FAILED: %d checks: %s" % (len(FAILURES), FAILURES))
        return False
    print("SPIKE_TESTS_PASSED")
    return True


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
