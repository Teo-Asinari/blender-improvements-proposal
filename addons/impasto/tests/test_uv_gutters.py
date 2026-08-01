import unittest
import sys
from pathlib import Path

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto.gpu.uv_gutters import (
    build_gutter_plan,
    expand_pixel_rect,
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


if __name__ == "__main__":
    unittest.main(argv=[__file__])
