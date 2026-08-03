"""Headless UV-health analysis tests."""

import os
import sys

import bpy

ADDONS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from uv_island_overlay import health


bpy.ops.wm.read_factory_settings(use_empty=True)
verts = []
faces = []
for i in range(5):
    x = i * 2.0
    start = len(verts)
    verts.extend(((x, 0, 0), (x + 1, 0, 0), (x, 1, 0)))
    faces.append((start, start + 1, start + 2))
mesh = bpy.data.meshes.new("UV Health Test")
mesh.from_pydata(verts, [], faces)
obj = bpy.data.objects.new("UV Health Test", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
uv = mesh.uv_layers.new(name="UVMap")
tri_uvs = (
    ((0.05, 0.05), (0.45, 0.05), (0.05, 0.45)),
    ((0.50, 0.05), (0.51, 0.05), (0.50, 0.06)),
    ((0.60, 0.60), (0.70, 0.60), (0.60, 0.70)),
    ((0.60, 0.60), (0.70, 0.60), (0.60, 0.70)),
    ((0.80, 0.80), (0.80, 0.80), (0.80, 0.80)),
)
for polygon, coords in zip(mesh.polygons, tri_uvs):
    for loop_index, coord in zip(polygon.loop_indices, coords):
        uv.data[loop_index].uv = coord
mesh.update()

result = health.analyze_object(
    obj, texture_size=1024, low_density_ratio=0.25,
    minimum_island_span_px=16)
assert result.island_count == 5, result
assert result.duplicate_triangles == 1, result
assert result.duplicate_faces == frozenset((2, 3)), result
assert result.zero_area_faces == frozenset((4,)), result
assert 1 in result.low_density_faces, result
assert 1 in result.tiny_island_faces, result

health.select_faces(obj, result.duplicate_faces)
obj.update_from_editmode()
assert {face.index for face in mesh.polygons if face.select} == {2, 3}

print("HEALTH_TESTS_PASSED")
