import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto.gpu.uv_gutters import (
    OFFSET_SENTINEL,
    bounded_jump_steps,
    build_gutter_plan,
    build_compact_offset_map,
    build_compact_offset_map_from_uvs,
    apply_gutters_disconnected,
    exact_relaxation_steps,
    expand_pixel_rect,
    offset_map_bytes,
    reference_offset_map,
    triangle_uv_islands,
)


class UVGutterPlanningTests(unittest.TestCase):
    def test_connected_triangles_with_continuous_uv_share_island(self):
        vertices = ((0, 1, 2), (2, 1, 3))
        uvs = (((0, 0), (1, 0), (0, 1)),
               ((0, 1), (1, 0), (1, 1)))
        self.assertEqual(triangle_uv_islands(vertices, uvs), (0, 0))

    def test_uv_seam_splits_shared_mesh_edge(self):
        vertices = ((0, 1, 2), (2, 1, 3))
        uvs = (((0, 0), (1, 0), (0, 1)),
               ((0.5, 0.5), (0.75, 0.5), (1, 1)))
        self.assertEqual(triangle_uv_islands(vertices, uvs), (0, 1))

    def test_coincident_disconnected_geometry_stays_separate(self):
        vertices = ((0, 1, 2), (3, 4, 5))
        uvs = (((0, 0), (1, 0), (0, 1)),) * 2
        self.assertEqual(triangle_uv_islands(vertices, uvs), (0, 1))

    def test_rect_expansion_is_clipped(self):
        self.assertEqual(expand_pixel_rect((1, 2, 10, 20), 4, 32),
                         (0, 0, 15, 26))
        self.assertEqual(expand_pixel_rect((30, 30, 2, 2), 4, 32),
                         (26, 26, 6, 6))

    def test_plan_combines_ownership_and_dirty_rect_contract(self):
        plan = build_gutter_plan(((0, 1, 2),),
                                 (((0, 0), (1, 0), (0, 1)),), 8, 4096)
        self.assertEqual(plan.triangle_islands, (0,))
        self.assertEqual(plan.expanded_rect((100, 100, 20, 20)),
                         (92, 92, 36, 36))

    def test_compact_schedule_and_exact_memory_budget(self):
        self.assertEqual(bounded_jump_steps(8), (8, 4, 2, 1))
        self.assertEqual(exact_relaxation_steps(8), (1,) * 8)
        self.assertEqual(bounded_jump_steps(0), ())
        self.assertEqual(exact_relaxation_steps(0), ())
        self.assertEqual(offset_map_bytes(2048), 16 * 1024 * 1024)
        self.assertEqual(offset_map_bytes(4096), 64 * 1024 * 1024)
        self.assertEqual(offset_map_bytes(8192), 256 * 1024 * 1024)

    def test_compact_memory_report_separates_gpu_and_cpu_peaks(self):
        # One retained RG16F map, two during ping-pong construction, plus the
        # transient float32 two-component CPU seed owned by the builder.
        pixels = 4096 * 4096
        self.assertEqual(offset_map_bytes(4096), pixels * 4)
        self.assertEqual(pixels * 8, 128 * 1024 * 1024)


class UVGutterGPUPrototypeTests(unittest.TestCase):
    @staticmethod
    def _read(mask, radius=8):
        import numpy as np
        result = build_compact_offset_map(mask, radius)
        values = np.asarray(result.texture.read().to_list(),
                            dtype=np.float32)
        return result, values.reshape(result.height, result.width, 2)

    def test_interior_is_preserved_and_radius_is_bounded(self):
        import numpy as np
        mask = np.zeros((25, 25), dtype=bool)
        mask[12, 12] = True
        _result, offsets = self._read(mask)
        self.assertTrue(np.array_equal(offsets[12, 12], (0.0, 0.0)))
        self.assertTrue(np.array_equal(offsets[12, 20], (-8.0, 0.0)))
        self.assertGreaterEqual(abs(offsets[12, 21, 0]), 1024.0)
        # Euclidean radius rejects the diagonal outside the 8 px circle.
        self.assertGreaterEqual(abs(offsets[18, 18, 0]), 1024.0)

    def test_close_seeds_get_deterministic_bounded_source_with_stable_tie(self):
        import numpy as np
        mask = np.zeros((17, 17), dtype=bool)
        mask[8, 4] = True
        mask[8, 12] = True
        _result, first = self._read(mask)
        _result, second = self._read(mask)
        self.assertTrue(np.array_equal(first, second))
        # Equidistant x=8 resolves to the lexicographically earlier source.
        self.assertTrue(np.array_equal(first[8, 8], (-4.0, 0.0)))
        self.assertTrue(np.array_equal(first[8, 9], (3.0, 0.0)))

    def test_edges_do_not_wrap_and_empty_seed_remains_invalid(self):
        import numpy as np
        mask = np.zeros((12, 12), dtype=bool)
        mask[0, 0] = True
        _result, offsets = self._read(mask, 3)
        self.assertTrue(np.array_equal(offsets[0, 3], (-3.0, 0.0)))
        self.assertGreaterEqual(abs(offsets[0, 11, 0]), 1024.0)
        empty = np.zeros((7, 9), dtype=bool)
        _result, invalid = self._read(empty)
        self.assertTrue(np.all(invalid == OFFSET_SENTINEL))

    def test_radius_zero_keeps_only_alpha_independent_seed_mask(self):
        import numpy as np
        mask = np.zeros((5, 5), dtype=bool)
        mask[2, 2] = True
        _result, offsets = self._read(mask, 0)
        self.assertTrue(np.array_equal(offsets[2, 2], (0.0, 0.0)))
        self.assertEqual(offsets[2, 1, 0], OFFSET_SENTINEL)

    def test_exact_gpu_matches_bruteforce_on_adversarial_and_random_masks(self):
        import numpy as np
        masks = []
        # Corners, boundaries, close competitors, and deterministic ties.
        adversarial = np.zeros((23, 27), dtype=bool)
        adversarial[0, 0] = adversarial[0, 26] = True
        adversarial[22, 0] = adversarial[22, 26] = True
        adversarial[11, 8] = adversarial[11, 16] = True
        adversarial[5, 12] = adversarial[17, 12] = True
        masks.append(adversarial)
        rng = np.random.default_rng(731942)
        for probability in (0.01, 0.08, 0.35):
            masks.append(rng.random((31, 29)) < probability)
        for radius in (0, 1, 3, 8):
            for mask in masks:
                _result, actual = self._read(mask, radius)
                expected = reference_offset_map(mask, radius)
                self.assertTrue(
                    np.array_equal(actual, expected),
                    "GPU mismatch for radius=%d density=%d" %
                    (radius, int(mask.sum())))

    @staticmethod
    def _rgba_texture(values):
        import gpu
        values = values.astype("float32")
        return gpu.types.GPUTexture(
            (values.shape[1], values.shape[0]), format="RGBA16F",
            data=gpu.types.Buffer("FLOAT", values.shape, values))

    @staticmethod
    def _read_rgba(texture, height, width):
        import numpy as np
        return np.asarray(texture.read().to_list(), dtype=np.float32).reshape(
            height, width, 4)

    def test_disconnected_apply_preserves_interior_and_copies_full_erase_texel(self):
        import numpy as np
        mask = np.zeros((9, 9), dtype=bool)
        mask[4, 4] = True
        offsets = build_compact_offset_map(mask, 2)
        source = np.zeros((9, 9, 4), dtype=np.float32)
        source[4, 4] = (0.25, 0.5, 0.75, 0.0)  # alpha-zero erase/RGB transport
        source[0, 0] = (1.0, 0.0, 1.0, 1.0)
        source[2, 2] = (0.0, 1.0, 0.5, 0.25)  # in rect, sentinel/unowned
        result = apply_gutters_disconnected(
            self._rgba_texture(source), offsets, (4, 4, 1, 1))
        actual = self._read_rgba(result.texture, 9, 9)
        self.assertEqual(result.work_rect, (2, 2, 5, 5))
        self.assertTrue(np.array_equal(actual[4, 4], source[4, 4]))
        self.assertTrue(np.array_equal(actual[4, 6], source[4, 4]))
        self.assertTrue(np.array_equal(actual[2, 2], source[2, 2]))
        self.assertGreaterEqual(result.apply_ms, 0.0)
        # Outside the expanded/scissored work rect remains the initial copy.
        self.assertTrue(np.array_equal(actual[0, 0], source[0, 0]))

    def test_disconnected_apply_keeps_adjacent_sources_separate_and_clips_edge(self):
        import numpy as np
        mask = np.zeros((7, 10), dtype=bool)
        mask[0, 0] = mask[3, 3] = mask[3, 7] = True
        offsets = build_compact_offset_map(mask, 3)
        source = np.zeros((7, 10, 4), dtype=np.float32)
        source[0, 0] = (0.5, 0.25, 0.125, 0.0)
        source[3, 3] = (1.0, 0.0, 0.0, 1.0)
        source[3, 7] = (0.0, 0.0, 1.0, 1.0)
        result = apply_gutters_disconnected(
            self._rgba_texture(source), offsets, (0, 0, 10, 7))
        actual = self._read_rgba(result.texture, 7, 10)
        self.assertEqual(result.work_rect, (0, 0, 10, 7))
        self.assertTrue(np.array_equal(actual[0, 2], source[0, 0]))
        self.assertTrue(np.array_equal(actual[3, 4], source[3, 3]))
        self.assertTrue(np.array_equal(actual[3, 6], source[3, 7]))
        # Exact tie at x=5 resolves to lower source x=3.
        self.assertTrue(np.array_equal(actual[3, 5], source[3, 3]))

    def test_uv_raster_multi_island_offsets_copy_only_their_source_texels(self):
        import numpy as np
        size = 16
        uvs = np.asarray((
            ((2 / size, 2 / size), (6 / size, 2 / size),
             (2 / size, 6 / size)),
            ((10 / size, 9 / size), (14 / size, 9 / size),
             (14 / size, 13 / size))), dtype=np.float32)
        offsets, diagnostics = build_compact_offset_map_from_uvs(
            uvs, size, 2)
        self.assertEqual(diagnostics.exact_duplicate_triangles, 0)
        raw = np.asarray(offsets.texture.read().to_list(),
                         dtype=np.float32).reshape(size, size, 2)
        interior = np.all(raw == 0.0, axis=2)
        valid_gutter = ((np.max(np.abs(raw), axis=2) < 2048.0)
                        & ~interior)
        source = np.zeros((size, size, 4), dtype=np.float32)
        ys, xs = np.nonzero(interior)
        source[ys, xs] = np.where(
            (xs < size // 2)[:, None],
            np.asarray((1.0, 0.0, 0.0, 0.0), dtype=np.float32),
            np.asarray((0.0, 0.0, 1.0, 0.5), dtype=np.float32))
        result = apply_gutters_disconnected(
            self._rgba_texture(source), offsets, (0, 0, size, size))
        actual = self._read_rgba(result.texture, size, size)
        gutter_y, gutter_x = np.nonzero(valid_gutter)
        self.assertGreater(len(gutter_x), 0)
        for y, x in zip(gutter_y, gutter_x):
            dx, dy = (int(raw[y, x, 0]), int(raw[y, x, 1]))
            self.assertTrue(np.array_equal(
                actual[y, x], source[y + dy, x + dx]))


class UVGutterSessionLifecycleTests(unittest.TestCase):
    @staticmethod
    def _session(enabled=True):
        import numpy as np
        return SimpleNamespace(
            settings={"experimental_uv_gutters": enabled,
                      "uv_gutter_padding_px": 8},
            gutter_uvs=np.asarray((((0, 0), (1, 0), (0, 1)),),
                                  dtype=np.float32),
            size=64, gutter_offset_map=None, gutter_map_key=None,
            gutter_diagnostics=None)

    def test_default_off_never_allocates(self):
        from impasto import gpu_engine
        session = self._session(False)
        with mock.patch.object(
                gpu_engine.uv_gutters,
                "build_compact_offset_map_from_uvs") as builder:
            self.assertFalse(gpu_engine._ensure_uv_gutter_map(session))
            builder.assert_not_called()

    def test_success_is_cached_and_release_drops_reference(self):
        from impasto import gpu_engine
        session = self._session()
        result = SimpleNamespace(
            persistent_bytes=64, peak_gpu_build_bytes=128,
            transient_cpu_bytes=0, initialization_ms=1.0)
        diagnostics = SimpleNamespace(
            triangle_count=1, subpixel_triangles=0, outside_unit_square=0,
            exact_duplicate_triangles=0)
        with mock.patch.object(
                gpu_engine.uv_gutters,
                "build_compact_offset_map_from_uvs",
                return_value=(result, diagnostics)) as builder, \
                mock.patch.object(
                    gpu_engine.uv_gutters, "create_gutter_apply_resources",
                    return_value=("shader", "batch")), \
                mock.patch.object(gpu_engine, "_log_line"):
            self.assertTrue(gpu_engine._ensure_uv_gutter_map(session))
            self.assertTrue(gpu_engine._ensure_uv_gutter_map(session))
            builder.assert_called_once()
        gpu_engine._release_gpu_references(session)
        self.assertIsNone(session.gutter_offset_map)
        self.assertIsNone(session.gutter_map_key)

    def test_build_failure_disables_experiment_without_raising(self):
        from impasto import gpu_engine
        session = self._session()
        with mock.patch.object(
                gpu_engine.uv_gutters,
                "build_compact_offset_map_from_uvs",
                side_effect=RuntimeError("unsupported RG16F")), \
                mock.patch.object(gpu_engine, "_log_line"):
            self.assertFalse(gpu_engine._ensure_uv_gutter_map(session))
        self.assertFalse(session.settings["experimental_uv_gutters"])
        self.assertIsNone(session.gutter_offset_map)

    def test_apply_resource_failure_disables_experiment_atomically(self):
        from impasto import gpu_engine
        session = self._session()
        result = SimpleNamespace()
        diagnostics = SimpleNamespace()
        with mock.patch.object(
                gpu_engine.uv_gutters,
                "build_compact_offset_map_from_uvs",
                return_value=(result, diagnostics)), \
                mock.patch.object(
                    gpu_engine.uv_gutters, "create_gutter_apply_resources",
                    side_effect=RuntimeError("shader compile failed")), \
                mock.patch.object(gpu_engine, "_log_line"):
            self.assertFalse(gpu_engine._ensure_uv_gutter_map(session))
        self.assertFalse(session.settings["experimental_uv_gutters"])
        self.assertIsNone(session.gutter_offset_map)
        self.assertIsNone(session.gutter_apply_shader)
        self.assertIsNone(session.gutter_apply_batch)

    def test_initial_padding_covers_active_and_resolved_baseline_textures(self):
        from impasto import gpu_engine
        events = []

        class Backend:
            def _draw_copy(self, source, framebuffer, viewport, origin, scale):
                events.append(("copy", source, viewport, origin, scale))

        class Framebuffer:
            def __init__(self, color_slots):
                self.target = color_slots[0]

        session = SimpleNamespace(
            gutter_offset_map="offsets", gutter_apply_shader="shader",
            gutter_apply_batch="batch", history_backend=Backend(),
            size=64, paint_texs=("base", "rough"),
            single_fbs=("base_fb", "rough_fb"),
            baseline_texs={"base_color": "lower_base"},
            soften_scratch="scratch", soften_scratch_fb="scratch_fb",
            gutter_apply_ms=1.0)

        def apply(_source, target, _offsets, rect, _shader, _batch):
            events.append(("apply", getattr(target, "target", target), rect))
            return 0.5

        with mock.patch.object(gpu_engine.gpu.types, "GPUFrameBuffer",
                               Framebuffer), \
                mock.patch.object(gpu_engine.uv_gutters,
                                  "apply_gutters_into", side_effect=apply), \
                mock.patch.object(gpu_engine, "_log_line"):
            count = gpu_engine._apply_initial_uv_gutters(session)

        self.assertEqual(count, 3)
        self.assertEqual([event[:2] for event in events], [
            ("copy", "base"), ("apply", "base_fb"),
            ("copy", "rough"), ("apply", "rough_fb"),
            ("copy", "lower_base"), ("apply", "lower_base"),
        ])
        self.assertEqual(session.gutter_apply_ms, 2.5)

    def test_initial_padding_is_noop_without_ready_offset_map(self):
        from impasto import gpu_engine
        session = SimpleNamespace(
            gutter_offset_map=None, gutter_apply_shader="shader",
            gutter_apply_batch="batch", history_backend=object())
        self.assertEqual(gpu_engine._apply_initial_uv_gutters(session), 0)

    def test_finalize_applies_each_channel_rect_before_history_commit(self):
        from impasto import gpu_engine
        events = []

        class Backend:
            def _draw_copy(self, source, framebuffer, viewport, origin, scale):
                events.append(("copy", source, viewport))

        class Transaction:
            def commit(self):
                events.append(("commit",))

        session = SimpleNamespace(
            pending_finalize=True,
            stroke_gutter_rects={"base_color": (2, 3, 5, 6),
                                 "roughness": (11, 12, 3, 4)},
            gutter_offset_map=SimpleNamespace(),
            gutter_apply_shader="shader", gutter_apply_batch="batch",
            history_backend=Backend(),
            settings={"channel_keys": ("base_color", "roughness")},
            channels=2, paint_texs=("base", "rough"),
            soften_scratch_fb="scratch_fb", soften_scratch="scratch",
            single_fbs=("base_fb", "rough_fb"), size=64,
            gutter_apply_ms=0.0, stroke_dirty=None,
            stroke_dirty_full=False, session_dirty_full=False,
            session_dirty=None, stroke_transaction=Transaction())

        def apply(_source, target, _offsets, rect, _shader, _batch):
            events.append(("apply", target, rect))
            return 0.25

        with mock.patch.object(gpu_engine.uv_gutters,
                               "apply_gutters_into", side_effect=apply), \
                mock.patch.object(gpu_engine, "_stroke_stats",
                                  return_value={}), \
                mock.patch.object(gpu_engine, "_log_line"):
            gpu_engine._finalize_stroke_gpu(session)
        self.assertEqual(events, [
            ("copy", "base", (2, 3, 5, 6)),
            ("apply", "base_fb", (2, 3, 5, 6)),
            ("copy", "rough", (11, 12, 3, 4)),
            ("apply", "rough_fb", (11, 12, 3, 4)),
            ("commit",),
        ])
        self.assertEqual(session.session_dirty,
                         (2 / 64, 3 / 64, 14 / 64, 16 / 64))
        self.assertEqual(session.gutter_apply_ms, 0.5)
        self.assertEqual(session.stroke_gutter_rects, {})


if __name__ == "__main__":
    result = unittest.main(argv=[__file__], exit=False)
    try:
        import bpy
        bpy.ops.wm.quit_blender()
    except ImportError:
        pass
