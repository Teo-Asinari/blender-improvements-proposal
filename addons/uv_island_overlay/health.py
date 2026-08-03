# SPDX-License-Identifier: GPL-2.0-or-later
"""UV-health analysis and problem-face selection."""

from dataclasses import dataclass
import collections
import math

import bmesh
from mathutils import Vector

from . import islands as island_math


@dataclass
class UVHealthResult:
    object_name: str
    uv_map: str
    face_count: int
    triangle_count: int
    island_count: int
    duplicate_triangles: int
    duplicate_groups: int
    zero_area_faces: frozenset
    duplicate_faces: frozenset
    out_of_bounds_faces: frozenset
    low_density_faces: frozenset
    tiny_island_faces: frozenset
    low_density_islands: tuple
    tiny_islands: tuple
    median_density: float | None
    minimum_density: float | None


_last_result = None


def last_result(object_name=None):
    if (_last_result is None or object_name is None
            or _last_result.object_name == object_name):
        return _last_result
    return None


def _polygon_uv_area(face, uv_layer):
    points = [loop[uv_layer].uv for loop in face.loops]
    return abs(sum(points[i].x * points[(i + 1) % len(points)].y
                   - points[(i + 1) % len(points)].x * points[i].y
                   for i in range(len(points)))) * 0.5


def _world_face_area(face, matrix_world):
    if len(face.verts) < 3:
        return 0.0
    origin = matrix_world @ face.verts[0].co
    area = 0.0
    for i in range(1, len(face.verts) - 1):
        a = matrix_world @ face.verts[i].co - origin
        b = matrix_world @ face.verts[i + 1].co - origin
        area += a.cross(b).length * 0.5
    return area


def analyze_object(obj, texture_size=4096, low_density_ratio=0.5,
                   minimum_island_span_px=8.0, epsilon=1e-12):
    """Analyze the active UV map without modifying the mesh."""
    global _last_result
    if obj is None or obj.type != 'MESH':
        raise ValueError("Select a mesh object")
    if obj.mode == 'EDIT':
        obj.update_from_editmode()
    mesh = obj.data
    if not mesh.uv_layers.active:
        raise ValueError("The active mesh has no UV map")

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            raise ValueError("The active mesh has no UV map")
        island_faces = island_math.compute_islands(bm, uv_layer)
        face_to_island = island_math.face_index_to_island(island_faces)
        uv_areas = [0.0] * len(island_faces)
        world_areas = [0.0] * len(island_faces)
        bounds = [[math.inf, math.inf, -math.inf, -math.inf]
                  for _ in island_faces]
        zero_area = set()
        outside = set()
        for face in bm.faces:
            island = face_to_island[face.index]
            uv_area = _polygon_uv_area(face, uv_layer)
            world_area = _world_face_area(face, obj.matrix_world)
            if uv_area <= epsilon or world_area <= epsilon:
                zero_area.add(face.index)
            else:
                uv_areas[island] += uv_area
                world_areas[island] += world_area
            for loop in face.loops:
                uv = loop[uv_layer].uv
                bounds[island][0] = min(bounds[island][0], uv.x)
                bounds[island][1] = min(bounds[island][1], uv.y)
                bounds[island][2] = max(bounds[island][2], uv.x)
                bounds[island][3] = max(bounds[island][3], uv.y)
                if uv.x < 0.0 or uv.x > 1.0 or uv.y < 0.0 or uv.y > 1.0:
                    outside.add(face.index)
        densities = [
            math.sqrt(uv_areas[i] / world_areas[i])
            if uv_areas[i] > epsilon and world_areas[i] > epsilon else None
            for i in range(len(island_faces))]
        defined = sorted(value for value in densities if value is not None)
        median = (defined[len(defined) // 2] if len(defined) % 2 else
                  (defined[len(defined) // 2 - 1] + defined[len(defined) // 2])
                  * 0.5) if defined else None
        low_ids = tuple(i for i, value in enumerate(densities)
                        if value is not None and median is not None
                        and value < median * float(low_density_ratio))
        tiny_ids = tuple(i for i, bound in enumerate(bounds)
                         if min(bound[2] - bound[0], bound[3] - bound[1])
                         * texture_size < minimum_island_span_px)
        low_faces = frozenset(face for island in low_ids
                              for face in island_faces[island])
        tiny_faces = frozenset(face for island in tiny_ids
                               for face in island_faces[island])
    finally:
        bm.free()

    mesh.calc_loop_triangles()
    uv_data = mesh.uv_layers.active.data
    groups = collections.defaultdict(list)
    for tri in mesh.loop_triangles:
        key = tuple(sorted((round(float(uv_data[loop].uv.x), 7),
                            round(float(uv_data[loop].uv.y), 7))
                           for loop in tri.loops))
        groups[key].append(tri.polygon_index)
    def key_area(key):
        (ax, ay), (bx, by), (cx, cy) = key
        return abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) * 0.5

    # Collapsed triangles are reported separately. Do not inflate the
    # duplicate-mapping count when hundreds of collapsed triangles share the
    # same point or line in UV space.
    duplicate_groups = [faces for key, faces in groups.items()
                        if len(faces) > 1 and key_area(key) > epsilon]
    duplicate_faces = frozenset(face for faces in duplicate_groups
                                for face in faces)
    result = UVHealthResult(
        object_name=obj.name, uv_map=mesh.uv_layers.active.name,
        face_count=len(mesh.polygons), triangle_count=len(mesh.loop_triangles),
        island_count=len(island_faces),
        duplicate_triangles=sum(len(group) - 1 for group in duplicate_groups),
        duplicate_groups=len(duplicate_groups),
        zero_area_faces=frozenset(zero_area),
        duplicate_faces=duplicate_faces,
        out_of_bounds_faces=frozenset(outside),
        low_density_faces=low_faces, tiny_island_faces=tiny_faces,
        low_density_islands=low_ids, tiny_islands=tiny_ids,
        median_density=median,
        minimum_density=min(defined) if defined else None)
    _last_result = result
    return result


def select_faces(obj, face_indices):
    """Select only the supplied polygon indices and enter face Edit Mode."""
    if obj.mode != 'EDIT':
        import bpy
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for face in bm.faces:
        face.select = face.index in face_indices
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
