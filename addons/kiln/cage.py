# SPDX-License-Identifier: GPL-2.0-or-later
"""Explicit bake-cage construction and native-speed shell previews.

The preview is made from ordinary wire-display mesh objects rather than a
per-frame Python draw callback.  Rebuilding is explicit (and automatic just
before a cage bake), while orbiting the viewport stays entirely in Blender's
native drawing path.

The outer object has exactly the low-poly's topology, as required by
``bpy.ops.object.bake(use_cage=True, cage_object=...)``.  Its vertices are
offset along the low-poly vertex normals.  An optional vertex group supplies
a painted local multiplier: 0.5 is neutral (1x), 0 is a non-degenerate
0.05x minimum, and 1 is 2x.
"""

import bpy


OUTER_ROLE = "OUTER"
INNER_ROLE = "INNER"
ROLE_KEY = "kiln_cage_role"
SOURCE_KEY = "kiln_cage_source"
PAINT_GROUP = "Kiln Cage Scale"


class CageError(Exception):
    pass


def painted_factors(low, enabled):
    """Per-vertex extrusion multipliers.  Neutral when painting is off or
    the group does not exist. Weight 0.5 -> 1x and 1 -> 2x. A small floor
    keeps painted-blue vertices separated from the low mesh; a coincident
    explicit cage has a degenerate inward-ray direction."""
    n = len(low.data.vertices)
    if not enabled:
        return [1.0] * n
    group = low.vertex_groups.get(PAINT_GROUP)
    if group is None:
        return [1.0] * n
    factors = []
    for vertex in low.data.vertices:
        try:
            factors.append(max(0.05, 2.0 * group.weight(vertex.index)))
        except RuntimeError:
            factors.append(1.0)
    return factors


def offset_coordinates(low, distance, factors=None, direction=1.0):
    """Pure-ish geometry extraction: local-space coordinate tuples offset
    along the source vertex normals.  The source mesh is updated first so
    normals reflect its current smooth/flat configuration."""
    low.data.update()
    if factors is None:
        factors = [1.0] * len(low.data.vertices)
    if len(factors) != len(low.data.vertices):
        raise CageError("Painted cage weights do not match low-poly vertices")
    d = float(distance) * float(direction)
    return [tuple(v.co + v.normal * (d * factors[v.index]))
            for v in low.data.vertices]


def _find(low, role):
    for obj in bpy.data.objects:
        if (obj.type == 'MESH' and obj.get(ROLE_KEY) == role
                and obj.get(SOURCE_KEY) == low.name):
            return obj
    return None


def _guide_collection(scene):
    name = "Kiln Guides"
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        scene.collection.children.link(collection)
    return collection


def _remove_object(obj):
    if obj is None:
        return
    mesh = obj.data if obj.type == 'MESH' else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _ensure_shell(context, low, role, distance, factors, direction):
    obj = _find(low, role)
    # A cage must match the low topology exactly. Recreate after retopo.
    if (obj is not None
            and (len(obj.data.vertices) != len(low.data.vertices)
                 or len(obj.data.polygons) != len(low.data.polygons))):
        _remove_object(obj)
        obj = None
    if obj is None:
        mesh = low.data.copy()
        mesh.name = "%s.Kiln%s" % (low.data.name, role.title())
        obj = bpy.data.objects.new("%s.Kiln%s" % (low.name, role.title()),
                                   mesh)
        _guide_collection(context.scene).objects.link(obj)
        obj[ROLE_KEY] = role
        obj[SOURCE_KEY] = low.name
        obj.display_type = 'WIRE'
        obj.show_in_front = True
        obj.hide_render = True
        obj.hide_select = True
        obj.color = ((1.0, 0.18, 0.05, 1.0) if role == OUTER_ROLE
                     else (0.05, 0.45, 1.0, 1.0))
    coords = offset_coordinates(low, distance, factors, direction)
    flat = [component for co in coords for component in co]
    obj.data.vertices.foreach_set("co", flat)
    obj.data.update()
    obj.matrix_world = low.matrix_world.copy()
    obj.hide_set(False)
    return obj


def build_guides(context, low, extrusion, max_ray_distance,
                 use_painted=False):
    if low is None or low.type != 'MESH' or not len(low.data.vertices):
        raise CageError("Pick a non-empty low-poly mesh first")
    if float(extrusion) <= 1e-9:
        raise CageError(
            "Explicit cage extrusion must be greater than zero; at zero "
            "the cage coincides with the low-poly and its inward ray "
            "direction is degenerate")
    factors = painted_factors(low, use_painted)
    outer = _ensure_shell(context, low, OUTER_ROLE, extrusion,
                          factors, 1.0)
    # Older Kiln builds created a second "inner" guide by subtracting max
    # ray distance. Blender does not expose Max Ray Distance while using an
    # explicit cage, so that shell implied control the baker does not have.
    _remove_object(_find(low, INNER_ROLE))
    return outer, None


def hide_guides(low):
    for role in (OUTER_ROLE, INNER_ROLE):
        obj = _find(low, role)
        if obj is not None:
            obj.hide_set(True)


def guides_visible(low):
    outer = _find(low, OUTER_ROLE)
    return outer is not None and not outer.hide_get()


def ensure_paint_group(low):
    group = low.vertex_groups.get(PAINT_GROUP)
    if group is None:
        group = low.vertex_groups.new(name=PAINT_GROUP)
        if len(low.data.vertices):
            group.add(range(len(low.data.vertices)), 0.5, 'REPLACE')
    low.vertex_groups.active_index = group.index
    return group


def remove_all_guides():
    for obj in list(bpy.data.objects):
        if obj.get(ROLE_KEY) in {OUTER_ROLE, INNER_ROLE}:
            _remove_object(obj)
