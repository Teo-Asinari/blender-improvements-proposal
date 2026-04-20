"""Voxel / OpenVDB backend for the Voxel Sculpt skeleton.

Two backends are planned. Exactly one will be selected at runtime based on
what is available in the current Blender install:

1. **Direct pyopenvdb**. Blender historically ships ``pyopenvdb`` with its
   bundled Python, which would let the add-on manipulate ``FloatGrid``
   level sets directly from Python. This is the cheapest path *if* it is
   actually exposed in Blender 4.x -- that is an open question that the
   parallel research agent is tracking.

2. **Native C++ extension**. A small pybind11 module that wraps the minimal
   OpenVDB surface we need (sphere SDF CSG, level-set filters,
   ``volumeToMesh``). This ships as pre-built wheels per platform inside
   the add-on zip. See ``docs/VOXEL_SCULPT_DESIGN.md`` for the full plan.

Until one of those backends exists, every function in this module is a
TODO stub that logs what it would do and returns a neutral value. The
add-on is designed so that importing this module must never fail, even
if neither backend is installed -- the import is guarded below.

Do NOT import pyopenvdb at module load time in order to keep the add-on
registerable without the dependency.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------
#
# We do a cheap, import-free check first so that merely loading this module
# never raises. The real probe happens inside ``_ensure_backend`` on first
# use.

_BACKEND: Optional[str] = None  # one of {"pyopenvdb", "native", None}
_pyopenvdb: Any = None
_native: Any = None


def _ensure_backend() -> Optional[str]:
    """Lazily detect which backend (if any) is available.

    Returns the backend name or ``None`` if neither is present. Safe to call
    repeatedly -- the detection result is cached in module globals.
    """
    global _BACKEND, _pyopenvdb, _native
    if _BACKEND is not None:
        return _BACKEND

    # Try pyopenvdb first. It is the least-friction path if Blender exposes it.
    try:
        import pyopenvdb as _pyopenvdb_mod  # type: ignore

        _pyopenvdb = _pyopenvdb_mod
        _BACKEND = "pyopenvdb"
        return _BACKEND
    except Exception:
        _pyopenvdb = None

    # Fall back to a native extension we would ship with the add-on.
    try:
        # The real name is still TBD; "_voxel_sculpt_native" is a placeholder.
        from . import _voxel_sculpt_native as _native_mod  # type: ignore

        _native = _native_mod
        _BACKEND = "native"
        return _BACKEND
    except Exception:
        _native = None

    _BACKEND = None
    return None


def backend_name() -> Optional[str]:
    """Public helper for the UI -- returns the active backend or None."""
    return _ensure_backend()


# ---------------------------------------------------------------------------
# Grid lifecycle
# ---------------------------------------------------------------------------


def create_grid(voxel_size: float = 0.02, background: float = 1.0) -> Any:
    """Create an empty narrow-band level-set grid.

    Parameters
    ----------
    voxel_size:
        Edge length of a voxel in world units.
    background:
        Background value of the level set -- typically the narrow-band
        half-width in world units so that the outside of the band reads as
        "deeply outside". OpenVDB convention: positive = outside.

    Returns
    -------
    An opaque grid handle. With pyopenvdb this is a ``FloatGrid``; with the
    native extension it is a ``PyCapsule`` wrapping a shared_ptr.
    """
    backend = _ensure_backend()
    # TODO(pyopenvdb): grid = pyopenvdb.FloatGrid(background)
    #                   grid.transform = pyopenvdb.createLinearTransform(voxel_size)
    #                   grid.gridClass = pyopenvdb.GridClass.LEVEL_SET
    # TODO(native):    return _native.create_level_set(voxel_size, background)
    print(
        f"[vdb_backend] create_grid(voxel_size={voxel_size}, background={background}) "
        f"-- backend={backend!r} (stub)"
    )
    return None


# ---------------------------------------------------------------------------
# CSG brush primitives
# ---------------------------------------------------------------------------


def add_sphere(
    grid: Any,
    center: Tuple[float, float, float],
    radius: float,
    strength: float = 1.0,
) -> None:
    """Union a sphere SDF into ``grid`` (additive brush).

    Implementation notes:
      * pyopenvdb: build a sphere level set with ``tools.createLevelSetSphere``
        then ``tools.csgUnion(grid, sphere)``.
      * native: single call into a C++ routine that rasterises the sphere
        directly into the narrow band without an intermediate grid, which is
        significantly faster per dab.
    """
    backend = _ensure_backend()
    # TODO: implement per backend; see docstring.
    print(
        f"[vdb_backend] add_sphere(center={center}, radius={radius}, "
        f"strength={strength}) -- backend={backend!r} (stub)"
    )


def subtract_sphere(
    grid: Any,
    center: Tuple[float, float, float],
    radius: float,
    strength: float = 1.0,
) -> None:
    """Subtract a sphere SDF from ``grid`` (carve brush)."""
    backend = _ensure_backend()
    # TODO: pyopenvdb path uses tools.csgDifference; native path uses a direct
    #       narrow-band subtract.
    print(
        f"[vdb_backend] subtract_sphere(center={center}, radius={radius}, "
        f"strength={strength}) -- backend={backend!r} (stub)"
    )


# ---------------------------------------------------------------------------
# Meshing
# ---------------------------------------------------------------------------


def grid_to_mesh(
    grid: Any,
    iso: float = 0.0,
    adaptivity: float = 0.0,
) -> Tuple[Any, Any, Any]:
    """Convert ``grid`` to triangle / quad arrays.

    Returns ``(verts, tris, quads)`` with the same shape as OpenVDB's
    ``volumeToMesh`` output. In the skeleton this returns three ``None``
    placeholders.
    """
    backend = _ensure_backend()
    # TODO(pyopenvdb): pyopenvdb.tools.volumeToMesh(grid, iso, adaptivity)
    # TODO(native):    _native.volume_to_mesh(grid, iso, adaptivity)
    print(
        f"[vdb_backend] grid_to_mesh(iso={iso}, adaptivity={adaptivity}) "
        f"-- backend={backend!r} (stub)"
    )
    return (None, None, None)
