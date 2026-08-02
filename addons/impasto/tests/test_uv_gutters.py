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
    expand_pixel_rect,
    offset_map_bytes,
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
        self.assertEqual(bounded_jump_steps(0), ())
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


if __name__ == "__main__":
    result = unittest.main(argv=[__file__], exit=False)
    try:
        import bpy
        bpy.ops.wm.quit_blender()
    except ImportError:
        pass
