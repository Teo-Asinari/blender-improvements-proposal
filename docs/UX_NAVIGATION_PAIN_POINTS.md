# Blender Sculpt/Paint UX Pain Points — Catalog

A focused catalog of widely-reported UX issues in Blender's sculpt and texture-paint workflows, with 3DCoat / ZBrush references and concrete fixes. Seven pain points in three categories, then quick-win and structural recommendations.

Feature #3 from the proposal (interactive brush size/intensity gesture) is excluded — it is already being addressed separately.

---

## Category A: Viewport Navigation

### A1. Middle-mouse dependency for orbit/pan/zoom

- **Pain point:** Default navigation requires MMB to orbit, Shift+MMB to pan, Ctrl+MMB to zoom. Sculptors on pen displays, trackpads, or entry-level tablets without an easily remappable MMB cannot navigate smoothly and must keep reaching for the keyboard.
- **Evidence:** [Sculpt Navigation extension](https://extensions.blender.org/add-ons/sculpt-navigation/) — a featured extension whose whole purpose is "ZBrush/3D-Coat style viewport navigation that allows clicking empty canvas space to navigate." See also [Is there a way to make sculpt navigation similar to Sculptris/Zbrush?](https://blenderartists.org/t/is-there-a-way-to-make-the-sculpt-room-navigation-similar-to-sculptris-zbrush/573746).
- **Reference tool behavior:** ZBrush and 3DCoat orbit by click-drag on empty canvas, pan with Alt+drag, zoom with scroll — no MMB. 3DCoat exposes a navigation-style dropdown (3DCoat / Maya / ZBrush / arrows) in preferences.
- **Proposed solution:** Ship a first-party "Sculpt Navigation" preset (empty-space click = orbit, Alt+click = pan, scroll = zoom-to-cursor). A Python add-on can approximate this by wrapping view operators in a modal that raycasts under the cursor; the existing Sculpt Navigation add-on proves feasibility.
- **Effort:** medium add-on (core patch preferable long-term).

### A2. No camera pivot-on-surface for sculpting

- **Pain point:** Blender orbits around scene origin or last selection. While sculpting, users expect the view to rotate around the point under the cursor so detail areas stay centered; pressing Numpad-period to reframe constantly interrupts stroke rhythm.
- **Evidence:** [3DCoat Camera and Navigation docs](https://3dcoat.com/documentation/manual/navigation/camera-and-navigation/); community references in the [Sculpt Mode thread Part 2](https://blenderartists.org/t/the-big-blender-sculpt-mode-thread-part-2/1395490?page=181). *Anecdotal — well-known from add-on ecosystem (Mouselook Navigation, Bbrush) rather than a single canonical bug.*
- **Reference tool behavior:** 3DCoat sets the pivot with one F press (pivot becomes point under cursor). ZBrush auto-pivots on the point clicked when orbit begins.
- **Proposed solution:** Add a "Pivot Under Cursor" toggle in the sculpt header that raycasts at orbit-start and uses the hit as the temporary view pivot. The [Mouselook Navigation add-on](https://github.com/dairin0d/mouselook-navigation) already does this; a trimmed sculpt-only variant could be upstreamed.
- **Effort:** small add-on (upstream later).

---

## Category B: Brush and Tool Switching

### B1. Brush size/strength not persistent per tool

- **Pain point:** Adjusting brush radius or strength mutates shared Unified Paint Settings by default, so switching from Clay Strips (radius 80) to Smooth (radius 20) and back loses the earlier value. ZBrush/3DCoat artists expect each brush to remember its own size and falloff.
- **Evidence:** [Sculpt Brush Management UI/UX Proposal](https://devtalk.blender.org/t/sculpt-brush-management-ui-ux-proposal/20071) — a multi-page devtalk thread whose core concern is brush-state and persistence. Related: [Lock Scale of Brush in Texture Paint](https://staging.blender.community/c/rightclickselect/H8cbbc/?category=painting).
- **Reference tool behavior:** ZBrush brushes carry per-brush Draw Size and ZIntensity; switching restores each one's last-used values. 3DCoat is similar with per-brush radius/depth persisted across sessions.
- **Proposed solution:** Give every brush asset its own `size`, `strength`, and `falloff` stored on the asset data-block. Keep "Unified size/strength" as an opt-in preference; default new installs to per-brush. Ensure brush asset save/load round-trips these values.
- **Effort:** requires C++ (schema + UI); a small add-on can partially emulate via save/restore handlers on brush change.

### B2. Brush switching is slow — no quick search/picker

- **Pain point:** Switching between Clay, Crease, Smooth, Grab during a pass requires numbered hotkeys (only a handful mapped) or the toolbar. The default Sculpt pie menu lists only a subset of brushes, and customizing it requires Python.
- **Evidence:** [T65611 Sculpt Pie Menu missing Brushes](https://developer.blender.org/T65611); [How do I add sculpt brushes to the pie menu?](https://blenderartists.org/t/how-do-i-add-sculpt-brushes-to-the-pie-menu/1101765); [Draw, Paint & Sculpting Keymap Proposal](https://devtalk.blender.org/t/draw-paint-sculpting-keymap-proposal-feedback-request/29253).
- **Reference tool behavior:** ZBrush's B-menu opens a two-letter filtered picker (B-C-S for Clay Buildup). 3DCoat uses a popup palette sorted by category with live filter.
- **Proposed solution:** Ship a "Brush Quick Picker" popover bound to a single hotkey (e.g. `B`) with a live-search text field over the active brush asset library and keyboard selection. Implementable today as an add-on using `template_search` plus a modal keymap.
- **Effort:** small add-on.

### B3. Texture paint stroke lag (soften/smear, large brushes, tablets)

- **Pain point:** Texture-paint strokes accumulate latency that becomes severe with soften/smear brushes, large radii, graphics tablets, and when a 2D UV view is open alongside the 3D view. Strokes can render as straight-line segments or only commit on pen-up.
- **Evidence:** [#58465 brush lag in texture painting](https://developer.blender.org/T58465); [#93796 Graphics Tablet Lag in Texture Paint](https://projects.blender.org/blender/blender/issues/93796); [#74753 Smear/soften lag](https://developer.blender.org/T74753); [#101545 lag with 2D UV window](https://projects.blender.org/blender/blender/issues/101545).
- **Reference tool behavior:** 3DCoat and Substance Painter run paint on GPU shaders with tile-based dirty updates, keeping near-constant latency regardless of resolution.
- **Proposed solution:** Move stroke accumulation and blending to the GPU — aligned with the [Paint Mode Design Discussion](https://devtalk.blender.org/t/paint-mode-design-discussion-feedback/24243). Short-term mitigation via add-on: enforce stroke-spacing minimums and disable live 2D UV-editor sync during strokes.
- **Effort:** requires C++ (add-on mitigation only).

---

## Category C: Feedback and HUD

### C1. No on-canvas HUD during F/Shift-F size/strength adjust

- **Pain point:** When resizing brush with F or adjusting strength with Shift-F, the numeric value appears only in the header — not near the cursor. On large pen displays this breaks focus, and the radius ring has no on-screen readout.
- **Evidence:** [Sculpt mode — Viewport HUD slider overlay (RCS)](https://blender.community/c/rightclickselect/w0N1/); [viewport HUD overlays (RCS)](https://blender.community/c/rightclickselect/99G4/).
- **Reference tool behavior:** ZBrush prints Draw Size and ZIntensity next to the cursor during adjustment. 3DCoat shows a size label on the brush ring.
- **Proposed solution:** Draw a compact text overlay (radius / strength) near the cursor during the F/Shift-F modal using `SpaceView3D.draw_handler_add`. Straightforward as an add-on.
- **Effort:** small add-on.

### C2. Symmetry state is invisible

- **Pain point:** Sculpt/paint symmetry (X/Y/Z, radial count) is toggled in a header popover with no persistent viewport indicator. Users accidentally sculpt asymmetrically and notice only later.
- **Evidence:** [Individual symmetry in sculpt mode (RCS)](https://blender.community/c/rightclickselect/61bbbc/); [Extended symmetry options (RCS)](https://blender.community/c/rightclickselect/9gJB/); [Better edit/sculpt mirror system (RCS)](https://blender.community/c/rightclickselect/g7GZ/).
- **Reference tool behavior:** ZBrush draws red symmetry cursors on mirrored side(s); the extra cursors are themselves the indicator. 3DCoat draws duplicate brush rings.
- **Proposed solution:** Ensure mirrored brush-ring cursors stay enabled by default and add a small always-on corner badge listing active axes (e.g. `SYM: X`). Feasible via a draw handler.
- **Effort:** small add-on.

---

## Quick Wins (small add-ons, deliverable now)

1. **Brush Quick Picker popover** (B2) — one hotkey, searchable brush list over the active asset library.
2. **Cursor HUD for size/strength** (C1) — overlay values near the cursor during F/Shift-F modal.
3. **Symmetry Badge overlay** (C2) — persistent corner indicator for active symmetry axes.

Each is a self-contained Python add-on (a few hundred lines) and directly targets the most-cited friction.

## Structural (needs upstream C++)

1. **Per-brush persistent size/strength/falloff** (B1) — data-block schema change; default unified-settings off.
2. **GPU texture-paint pipeline** (B3) — the long-standing lag bugs cannot be fixed by an add-on; needs the in-progress Paint Mode refactor.
3. **Pivot-under-cursor / empty-space navigation preset** (A1 + A2) — deliverable as add-on first but belongs in core as a preferences navigation style to match 3DCoat's dropdown and compose cleanly with gizmos, walk-nav, and VR.
