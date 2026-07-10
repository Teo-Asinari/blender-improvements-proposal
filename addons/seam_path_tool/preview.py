# SPDX-License-Identifier: GPL-2.0-or-later
"""Viewport overlays for the interactive seam-path tool.

This module owns ALL overlay drawing for the modal session:

- 3D (POST_VIEW handler):
    * committed seam segments of this session (red polylines;
      erase-commits in muted grey)
    * the live candidate path (orange polyline)
    * anchor vertices (large green points with a dark outline)
    * the snap-target vertex core (white point)
- 2D (POST_PIXEL handler):
    * a cyan ring around the projected snap-target vertex (constant
      pixel size, so it reads clearly at any zoom)
    * a help/status panel anchored at the bottom-left of the viewport
      (semi-transparent dark quad + blf text)

Kept deliberately thin and isolated: the modal operator hands this module
plain lists of world-space coordinates and status scalars; nothing here
touches bmesh or operator state. All gpu/blf work is deferred to draw time
and exception-guarded, since GPU drawing is unavailable in `--background`
mode (gpu.shader.from_builtin raises SystemError there — verified on
5.1.2). The pure helpers (`compose_help_lines`, `circle_points_2d`) have
no gpu dependency and are covered by the headless tests.
"""

import math

import bpy
import gpu

# --- Colors (RGBA) ---------------------------------------------------------
COLOR_PREVIEW = (1.0, 0.55, 0.05, 1.0)     # live candidate path: orange
COLOR_COMMITTED = (0.92, 0.12, 0.18, 1.0)  # committed seam segments: red
COLOR_ERASED = (0.45, 0.48, 0.55, 0.9)     # committed erase segments: grey
COLOR_ANCHOR = (0.25, 1.0, 0.35, 1.0)      # anchors: green
COLOR_ANCHOR_OUTLINE = (0.0, 0.0, 0.0, 0.9)
COLOR_SNAP_CORE = (1.0, 1.0, 1.0, 1.0)     # snap-target vertex: white core
COLOR_SNAP_RING = (0.1, 0.85, 1.0, 1.0)    # ... with a cyan ring
COLOR_PANEL_BG = (0.05, 0.05, 0.05, 0.65)  # help panel background
COLOR_TEXT_HELP = (0.9, 0.9, 0.9, 1.0)
COLOR_TEXT_STATUS = (1.0, 0.75, 0.35, 1.0)

# --- Sizes (pixels at ui_scale == 1.0) --------------------------------------
LINE_WIDTH = 3.0
ANCHOR_POINT_SIZE = 12.0
ANCHOR_OUTLINE_EXTRA = 4.0   # outline point is this much larger
SNAP_POINT_SIZE = 7.0
SNAP_RING_RADIUS = 9.0
SNAP_RING_WIDTH = 2.0
SNAP_RING_SEGMENTS = 24
PANEL_FONT_SIZE = 12.0
PANEL_PADDING = 10.0
PANEL_MARGIN = 16.0
PANEL_LINE_SPACING = 1.55


# ---------------------------------------------------------------------------
# Pure helpers (headless-testable, no gpu)
# ---------------------------------------------------------------------------

def compose_help_lines(mode, anchors, segments, erase_active):
    """Text lines for the viewport help panel.

    Returns [controls_line, status_line]. Pure function so headless tests
    can verify the panel content without a GPU.
    """
    controls = ("Seam Path  —  LMB: add point   Ctrl+LMB: erase mode   "
                "Backspace: undo segment   Enter/RMB/Esc: finish")
    status = ("Anchors: %d   Segments: %d   Mode: %s"
              % (anchors, segments, mode))
    if erase_active:
        status += "   [ERASE]"
    return [controls, status]


def circle_points_2d(center, radius, segments=SNAP_RING_SEGMENTS):
    """Closed 2D circle polyline (segments+1 points, first == last)."""
    cx, cy = center
    pts = []
    for i in range(segments + 1):
        t = 2.0 * math.pi * i / segments
        pts.append((cx + radius * math.cos(t), cy + radius * math.sin(t)))
    return pts


def _ui_scale():
    """UI scale factor, robust to background mode (where it reads 0.0)."""
    try:
        scale = bpy.context.preferences.system.ui_scale
    except Exception:
        scale = 1.0
    return scale if scale and scale > 0.0 else 1.0


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

class PathPreview:
    """Owns the POST_VIEW (3D) and POST_PIXEL (2D) draw handlers for one
    modal session. The operator writes plain data into the public fields;
    the draw callbacks only read them.
    """

    def __init__(self):
        self._handle_3d = None
        self._handle_2d = None
        self._shader = None
        self._polyline = False
        # World-space coordinates, set by the modal operator:
        self.path_coords = []          # live candidate path polyline
        self.anchor_coords = []        # committed anchor positions
        self.committed_segments = []   # [(coords_list, is_erase), ...]
        self.snap_coord = None         # vertex the cursor snaps to (or None)
        # Status for the help panel:
        self.mode = 'LENGTH'
        self.anchor_count = 0
        self.segment_count = 0
        self.erase_active = False

    # -- state -------------------------------------------------------------

    def set_status(self, mode, anchors, segments, erase_active):
        self.mode = mode
        self.anchor_count = anchors
        self.segment_count = segments
        self.erase_active = erase_active

    def help_lines(self):
        return compose_help_lines(self.mode, self.anchor_count,
                                  self.segment_count, self.erase_active)

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        if self._handle_3d is None:
            self._handle_3d = bpy.types.SpaceView3D.draw_handler_add(
                self._draw_3d, (), 'WINDOW', 'POST_VIEW')
        if self._handle_2d is None:
            self._handle_2d = bpy.types.SpaceView3D.draw_handler_add(
                self._draw_2d, (), 'WINDOW', 'POST_PIXEL')

    def stop(self):
        """Remove both handlers. Idempotent and never raises, so it is safe
        on every exit path (finish, cancel, exception)."""
        if self._handle_3d is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    self._handle_3d, 'WINDOW')
            except Exception:
                pass
            self._handle_3d = None
        if self._handle_2d is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    self._handle_2d, 'WINDOW')
            except Exception:
                pass
            self._handle_2d = None
        self.path_coords = []
        self.anchor_coords = []
        self.committed_segments = []
        self.snap_coord = None

    # -- drawing: 3D (POST_VIEW) ----------------------------------------------

    def _get_line_shader(self):
        if self._shader is None:
            # POLYLINE_UNIFORM_COLOR gives proper line width; fall back to
            # plain UNIFORM_COLOR if a future Blender drops it.
            try:
                self._shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
                self._polyline = True
            except Exception:
                self._shader = gpu.shader.from_builtin('UNIFORM_COLOR')
                self._polyline = False
        return self._shader

    def _draw_polyline(self, shader, coords, color, width):
        from gpu_extras.batch import batch_for_shader
        batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": coords})
        shader.bind()
        if self._polyline:
            region = bpy.context.region
            if region is not None:
                shader.uniform_float(
                    "viewportSize", (region.width, region.height))
            shader.uniform_float("lineWidth", width)
        else:
            gpu.state.line_width_set(width)
        shader.uniform_float("color", color)
        batch.draw(shader)
        if not self._polyline:
            gpu.state.line_width_set(1.0)

    def _draw_points(self, coords, color, size):
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'POINTS', {"pos": coords})
        gpu.state.point_size_set(size)
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        gpu.state.point_size_set(1.0)

    def _draw_3d(self):
        if (not self.path_coords and not self.anchor_coords
                and not self.committed_segments and self.snap_coord is None):
            return
        try:
            scale = _ui_scale()
            gpu.state.blend_set('ALPHA')
            # Draw on top of the mesh so the overlays are always visible.
            gpu.state.depth_test_set('NONE')

            line_shader = self._get_line_shader()

            # Committed segments of this session (under the live preview).
            for coords, is_erase in self.committed_segments:
                if len(coords) >= 2:
                    color = COLOR_ERASED if is_erase else COLOR_COMMITTED
                    self._draw_polyline(line_shader, coords, color,
                                        LINE_WIDTH * scale)

            # Live candidate path.
            if len(self.path_coords) >= 2:
                self._draw_polyline(line_shader, self.path_coords,
                                    COLOR_PREVIEW, LINE_WIDTH * scale)

            # Anchors: dark outline point under a green point.
            if self.anchor_coords:
                self._draw_points(self.anchor_coords, COLOR_ANCHOR_OUTLINE,
                                  (ANCHOR_POINT_SIZE
                                   + ANCHOR_OUTLINE_EXTRA) * scale)
                self._draw_points(self.anchor_coords, COLOR_ANCHOR,
                                  ANCHOR_POINT_SIZE * scale)

            # Snap-target core dot (the cyan ring is drawn in POST_PIXEL).
            if self.snap_coord is not None:
                self._draw_points([self.snap_coord], COLOR_SNAP_CORE,
                                  SNAP_POINT_SIZE * scale)

            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.blend_set('NONE')
        except Exception:
            # Never let a draw-time error take down the viewport callback
            # (and keep headless/background runs harmless).
            pass

    # -- drawing: 2D (POST_PIXEL) -----------------------------------------------

    def _draw_2d(self):
        try:
            from gpu_extras.batch import batch_for_shader

            region = bpy.context.region
            rv3d = bpy.context.region_data
            if region is None:
                return
            scale = _ui_scale()
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')

            gpu.state.blend_set('ALPHA')

            # Snap ring: constant pixel size around the projected snap vert.
            if self.snap_coord is not None and rv3d is not None:
                from bpy_extras import view3d_utils
                co2d = view3d_utils.location_3d_to_region_2d(
                    region, rv3d, self.snap_coord)
                if co2d is not None:
                    ring = circle_points_2d(
                        (co2d.x, co2d.y), SNAP_RING_RADIUS * scale)
                    batch = batch_for_shader(
                        shader, 'LINE_STRIP', {"pos": ring})
                    gpu.state.line_width_set(SNAP_RING_WIDTH * scale)
                    shader.bind()
                    shader.uniform_float("color", COLOR_SNAP_RING)
                    batch.draw(shader)
                    gpu.state.line_width_set(1.0)

            self._draw_help_panel(region, shader, batch_for_shader, scale)

            gpu.state.blend_set('NONE')
        except Exception:
            pass

    def _draw_help_panel(self, region, shader, batch_for_shader, scale):
        import blf

        font_id = 0
        font_size = PANEL_FONT_SIZE * scale
        blf.size(font_id, font_size)  # 5.1: strictly (fontid, size)

        lines = self.help_lines()
        line_h = font_size * PANEL_LINE_SPACING
        pad = PANEL_PADDING * scale
        margin = PANEL_MARGIN * scale

        width = max(blf.dimensions(font_id, ln)[0] for ln in lines)
        panel_w = width + 2.0 * pad
        panel_h = line_h * len(lines) + 2.0 * pad

        x0, y0 = margin, margin
        x1, y1 = x0 + panel_w, y0 + panel_h

        # Background quad.
        batch = batch_for_shader(
            shader, 'TRIS',
            {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]},
            indices=[(0, 1, 2), (2, 3, 0)])
        shader.bind()
        shader.uniform_float("color", COLOR_PANEL_BG)
        batch.draw(shader)

        # Text: controls line on top, status line below it.
        for i, line in enumerate(lines):
            color = COLOR_TEXT_HELP if i == 0 else COLOR_TEXT_STATUS
            # Line 0 is drawn highest; text baseline sits pad above the
            # bottom of its slot.
            y = y0 + pad + line_h * (len(lines) - 1 - i) \
                + (line_h - font_size) * 0.5
            blf.position(font_id, x0 + pad, y, 0.0)
            blf.color(font_id, *color)
            blf.draw(font_id, line)
