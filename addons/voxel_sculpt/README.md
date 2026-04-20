# Voxel Sculpt (skeleton)

A prototype Blender 4.x add-on exploring 3DCoat-style voxel sculpting on
top of OpenVDB. **This is a skeleton** -- the UI, operators and property
group exist and the add-on registers cleanly, but no real voxel editing
is implemented. Every backend function is a ``# TODO`` stub.

See the full design discussion in
`../../docs/VOXEL_SCULPT_DESIGN.md` and the top-level
`../../PROPOSAL.md`.

## Install

1. Zip the `voxel_sculpt` folder:
   ```
   cd addons
   zip -r voxel_sculpt.zip voxel_sculpt
   ```
2. In Blender 4.x: `Edit > Preferences > Add-ons > Install...` and pick
   the zip.
3. Enable *Voxel Sculpt* under the Sculpt category.
4. Open the 3D Viewport, press `N` to open the sidebar, and switch to
   the **Voxel Sculpt** tab.

## Current status

| Area              | State                                                  |
|-------------------|--------------------------------------------------------|
| Add-on registers  | Yes (UI panel, property group, operators registered)   |
| New Voxel Object  | Stub -- reports what it would do, creates no data       |
| Brush modal       | Stub -- captures mouse events and prints them to stdout |
| Remesh to Mesh    | Stub -- reports what it would do, creates no mesh       |
| VDB backend       | Stub module; neither pyopenvdb nor native ext wired up |

## Known limitations

- No real grid is ever allocated. Buttons are wired but do nothing.
- `vdb_backend.py` does not import `pyopenvdb` at load time; the backend
  probe is lazy and cached. If no backend is found, stubs print the
  active backend as `None` and return placeholders.
- No raycast, no brush dab rasterisation, no re-meshing, no undo integration.
- The brush modal only handles left-mouse press / move / release and
  `Esc` / right-click to cancel; no pressure sensitivity, no stroke
  smoothing.
- Property group is attached to `Scene`, which is the wrong long-term
  home (settings should probably be per voxel object). See the design
  doc for the data-model discussion.

## Files

- `__init__.py` -- `bl_info`, register / unregister
- `properties.py` -- `VoxelSculptSettings` PropertyGroup
- `operators.py` -- `voxel.new_voxel_object`, `voxel.brush_modal`,
  `voxel.remesh_to_mesh` stubs
- `panel.py` -- sidebar UI panel
- `vdb_backend.py` -- backend stubs with TODOs for both the pyopenvdb
  and native-extension paths
