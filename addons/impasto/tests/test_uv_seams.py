import sys
import unittest
from pathlib import Path

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto.gpu.uv_seams import build_seam_correspondence
from impasto import gpu_engine


class UVSeamCorrespondenceTests(unittest.TestCase):
    def test_touched_filter_selects_only_intersecting_owner_face(self):
        import numpy as np
        records = ((0, None, None, (0, 0, 1, 1)),
                   (1, None, None, (4, 4, 1, 1)))
        boxes = np.asarray(((0, 0, 10, 10), (20, 20, 30, 30)),
                           dtype=np.float32)
        self.assertEqual(gpu_engine.touched_seam_record_indices(
            records, boxes, (5, 5, 8, 8)), (0,))

    def test_conservative_records_include_edge_and_vertex_caps(self):
        import numpy as np
        vertices = ((0, 1, 2), (2, 1, 3))
        uvs = np.asarray((((0, 0), (0.4, 0), (0, 0.4)),
                          ((0.8, 0.8), (0.6, 0.8), (1, 1))),
                         dtype=np.float32)
        positions = np.asarray((((0, 0, 0), (1, 0, 0), (0, 1, 0)),
                                ((0, 1, 0), (1, 0, 0), (1, 1, 0))),
                               dtype=np.float32)
        correspondence = build_seam_correspondence(vertices, uvs)
        records = gpu_engine.build_conservative_seam_records(
            correspondence, uvs, positions, 1024)
        self.assertEqual(len(records), 2)
        # Six edge vertices plus two six-vertex corner caps.
        self.assertEqual(records[0][1].shape, (18, 3))
        self.assertEqual(records[0][2].shape, (18, 2))
        self.assertTrue(np.allclose(records[0][1][1], records[0][1][2]))

    def test_literal_transport_excludes_tangent_normal_only(self):
        keys = ("base_color", "metallic", "roughness", "normal", "height",
                "emission_color", "emission_strength", "sss_weight")
        self.assertEqual(gpu_engine.seam_continuation_channel_keys(keys), (
            "base_color", "metallic", "roughness", "height",
            "emission_color", "emission_strength", "sss_weight"))

    def test_coverage_shader_reuses_exact_dab_visibility_prelude(self):
        source = gpu_engine.SEAM_COVERAGE_FRAG_SRC
        self.assertIn("impasto_visible_surface", source)
        self.assertIn("stencil_factor", source)
        self.assertIn("dab_params.paint_flags.y * f", source)

    def test_sparse_strips_pair_distant_islands_bidirectionally(self):
        import numpy as np
        vertex_triangles = ((0, 1, 2), (2, 1, 3))
        uv_triangles = np.asarray((
            ((0.0, 0.0), (0.4, 0.0), (0.0, 0.4)),
            ((0.8, 0.8), (0.6, 0.8), (1.0, 1.0))), dtype=np.float32)
        correspondence = build_seam_correspondence(
            vertex_triangles, uv_triangles)
        destination, source, rects = gpu_engine.build_sparse_seam_strips(
            correspondence, uv_triangles, 1024, 2)
        self.assertEqual(destination.shape, (12, 2))
        self.assertEqual(source.shape, (12, 2))
        self.assertEqual(len(rects), 2)
        # Both directed strips connect UV regions which are far apart in the
        # atlas; atlas proximity is never used as ownership.
        self.assertGreater(abs(float(destination[0, 0] - source[0, 0])), 0.1)

    def test_transfer_shader_never_overwrites_touched_destination(self):
        source = gpu_engine.SEAM_TRANSFER_FRAG_SRC
        self.assertIn("source_coverage <= 1e-6", source)
        self.assertIn("destination_coverage > 1e-6", source)
        self.assertIn("discard", source)

    def test_pairs_distant_uv_edges_by_mesh_topology(self):
        result = build_seam_correspondence(
            ((0, 1, 2), (2, 1, 3)),
            (((0, 0), (0.4, 0), (0, 0.4)),
             ((0.8, 0.8), (0.6, 0.8), (1, 1))))
        self.assertEqual(len(result.pairs), 1)
        pair = result.pairs[0]
        self.assertEqual(pair.mesh_edge, (1, 2))
        # Endpoints correspond to mesh vertices 1 then 2 despite winding.
        self.assertEqual(pair.first.uv0, (0.4, 0.0))
        self.assertEqual(pair.first.uv1, (0.0, 0.4))
        self.assertEqual(pair.second.uv0, (0.6, 0.8))
        self.assertEqual(pair.second.uv1, (0.8, 0.8))
        self.assertEqual(result.diagnostics.seam_edges, 1)
        self.assertEqual(len(result.diagnostics.boundary_edges), 4)

    def test_continuous_shared_edge_is_not_a_seam(self):
        result = build_seam_correspondence(
            ((0, 1, 2), (2, 1, 3)),
            (((0, 0), (1, 0), (0, 1)),
             ((0, 1), (1, 0), (1, 1))))
        self.assertEqual(result.pairs, ())
        self.assertEqual(result.diagnostics.continuous_edges, 1)

    def test_boundary_and_non_manifold_edges_are_reported(self):
        result = build_seam_correspondence(
            ((0, 1, 2), (1, 0, 3), (0, 1, 4)),
            (((0, 0), (1, 0), (0, 1)),
             ((1, 0), (0, 0), (1, 1)),
             ((0, 0), (1, 0), (0.5, 1))))
        self.assertIn((0, 1), result.diagnostics.non_manifold_edges)
        self.assertEqual(len(result.diagnostics.boundary_edges), 6)
        self.assertEqual(result.pairs, ())

    def test_unrelated_mesh_edges_with_same_uv_segment_are_ambiguous(self):
        result = build_seam_correspondence(
            ((0, 1, 2), (3, 4, 5)),
            (((0, 0), (1, 0), (0, 1)),
             ((0, 0), (1, 0), (1, 1))))
        self.assertIn(((0, 1), (3, 4)),
                      result.diagnostics.overlapping_uv_edges)

    def test_degenerate_mesh_edge_and_bad_inputs_are_explicit(self):
        result = build_seam_correspondence(
            ((0, 0, 1),), (((0, 0), (0, 0), (1, 0)),))
        self.assertEqual(result.diagnostics.degenerate_edges, ((0, 0),))
        with self.assertRaises(ValueError):
            build_seam_correspondence(((0, 1, 2),), (), 1e-7)
        with self.assertRaises(ValueError):
            build_seam_correspondence((), (), 0.0)


if __name__ == '__main__':
    unittest.main()
