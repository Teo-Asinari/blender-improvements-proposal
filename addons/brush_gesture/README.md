# Brush Gesture

3DCoat-style interactive brush size and strength adjustment for Blender 4.x sculpt and paint modes.

## Install

1. Zip the `brush_gesture/` folder.
2. `Edit > Preferences > Add-ons > Install...` and pick the zip.
3. Enable **Paint: Brush Gesture**.

## Usage

Hold **D** and drag **Right Mouse Button** inside the 3D viewport while in one of:

- Sculpt
- Texture Paint
- Weight Paint
- Vertex Paint

Horizontal drag changes **brush size**. Vertical drag changes **brush strength**. Release the mouse button (or the hold key, or hit `Esc`) to commit.

### Modifiers during drag

- `Shift` — hard snap to the nearest preset value
- `Ctrl` — pure continuous motion, no detents
- default — soft detent ("magnet" feel near preset values)

### HUD

An on-screen overlay shows the current brush circle, a strength bar, a preset tick scale along the bottom of the viewport, and a text readout indicating current values and the active mode (`SNAP` / `FREE` / `DETENT`). The HUD can be disabled in the add-on preferences.

## Preferences

`Edit > Preferences > Add-ons > Brush Gesture` exposes:

- **Hold Key** and **Gesture Mouse Button** for rebinding the gesture
- **Size Presets** and **Strength Presets** (editable lists; defaults: sizes `5, 15, 40, 100, 250`, strengths `0.25, 0.5, 0.75, 1.0`)
- **Detent Radius** and **Detent Damping** for tuning how sticky presets feel
- **Size Sensitivity** / **Strength Sensitivity** for drag scaling
- **Invert Strength Axis** to flip the vertical direction
- **HUD** toggles and text size

## Brush memory

Size and strength are remembered per brush on the current scene (serialized to a Scene string property, so it travels with the `.blend` file). Switching to a brush that has been used before restores its last values at the start of the next gesture.

## Known limitations / status

- v1, viewport-only.
- No tablet pressure input yet — the gesture is mouse-delta driven.
- No per-brush preset overrides; the preset lists are global.
- Brush memory is scene-scoped; it does not transfer between `.blend` files.
- Some Blender API specifics (exact builtin shader name, `blf.size` signature across minor versions, paint-mode keymap names) are marked `# VERIFY:` in source and may need a small tweak against a specific Blender 4.x release.
