# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure occlusion geometry for viewport vertex picking.

The interactive tool must only snap to vertices on surfaces facing /
visible to the camera (unless the viewport is in X-ray shading, where
Blender's own convention is to select through the mesh). The geometric
core of that test lives here, with no bpy imports, so it is fully
testable headlessly against a constructed ``mathutils.bvhtree.BVHTree``.

Picking semantics (implemented by the shell in __init__.py on top of
these helpers, documented here because this is where the decisions live):

1. A ray is cast from the view through the mouse position into a BVH of
   the *visible* faces of the edit mesh. On a hit, the pick snaps to the
   hit face's vertex that is nearest to the mouse IN SCREEN SPACE (not
   nearest to the 3D hit point) — the cursor-nearest dot is what the eye
   expects to win, and it keeps the pick stable while sliding the cursor
   across a large face at a glancing angle.
2. On a miss (clicking just off the silhouette), the pick falls back to
   the screen-space nearest vertex within the pick radius, but each
   candidate must pass `is_vert_visible`: a candidate is rejected when
   the mesh blocks the view ray to it meaningfully before the vertex
   itself. "Meaningfully" is epsilon-tolerant (`occlusion_epsilon`) so a
   vertex's own faces — which the ray hits at exactly the vertex's
   distance — never self-occlude it.
"""

__all__ = (
    "occlusion_epsilon",
    "is_vert_visible",
    "first_visible_candidate",
)

# Occlusion tolerance: relative to the vertex distance, floored for very
# close range. A blocker must be closer than (dist - epsilon) to reject a
# candidate; hits within epsilon of the vertex are the vertex's own faces.
OCCLUSION_EPS_REL = 1e-3
OCCLUSION_EPS_ABS = 1e-5


def occlusion_epsilon(dist):
    """Tolerance band (in the same units as *dist*) inside which a BVH hit
    counts as the vertex's own surface rather than an occluder."""
    return max(OCCLUSION_EPS_ABS, dist * OCCLUSION_EPS_REL)


def is_vert_visible(bvh, origin, direction, dist):
    """True if nothing in *bvh* blocks the ray meaningfully before *dist*.

    :arg bvh: mathutils.bvhtree.BVHTree (same space as origin/direction).
    :arg origin: ray origin (the view position for this screen point).
    :arg direction: normalized ray direction toward the vertex.
    :arg dist: distance from origin to the vertex along direction.
    """
    if dist <= occlusion_epsilon(dist):
        return True  # vertex is at the ray origin; nothing can block it
    location, _normal, _index, hit_dist = bvh.ray_cast(origin, direction)
    if location is None:
        return True
    return hit_dist >= dist - occlusion_epsilon(dist)


def first_visible_candidate(bvh, candidates):
    """First candidate whose view ray is not blocked.

    :arg candidates: iterable of (key, origin, direction, dist) in
        priority order (e.g. screen-space nearest first); direction must
        be normalized and dist the origin→vertex distance.
    :return: the winning candidate's key, or None if all are occluded.
    """
    for key, origin, direction, dist in candidates:
        if is_vert_visible(bvh, origin, direction, dist):
            return key
    return None
