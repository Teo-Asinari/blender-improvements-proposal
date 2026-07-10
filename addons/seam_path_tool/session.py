# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure session-state bookkeeping for the interactive seam-path tool.

This module owns everything the modal operator previously tracked ad hoc:
the anchor click history, the committed segments (edge lists + the seam
state each edge had *before* the commit + the mark/erase flag), in-tool
undo, and the derived overlay model (which polylines and anchor dots the
viewport should show). No bpy / UI imports — only bmesh-level data is
touched — so the whole commit/erase/undo lifecycle is testable in
`blender --background` (see tests/test_session.py).

The modal operator in __init__.py is a thin event shell over this class:
it resolves clicks to BMVerts and shortest paths, then delegates all state
changes to `commit_segment` / `undo_last`, and feeds
`overlay_polylines` / `overlay_anchor_indices` straight to the preview.

Performance note (1.3.0): everything here is O(session size + path
length) — `commit_segment` only flips seam flags along the given edge
list and records bookkeeping. The expensive part of a click, the next
anchor's shortest-path tree, is deliberately NOT this module's problem:
the modal hands `commit_segment` the edge list the preview already
computed, and rebuilds the tree incrementally afterwards (see
core.DijkstraSlicer and the __init__.py module docstring), so committing
stays effectively instant even on very large meshes.

Elements are stored by INDEX (vert/edge indices), not as BMVert/BMEdge
references: the session only ever changes seam flags, never topology, so
indices stay stable for its whole lifetime and survive edit-mesh wrapper
swaps (undo pushes) that would invalidate element references.

Overlay semantics
-----------------
- A MARK segment is shown as a polyline over its edges. When a later
  erase commit covers some of its edges, those portions of its overlay
  disappear (the polyline may split); a fully covered mark segment's
  overlay disappears entirely.
- An ERASE segment is shown (grey in the preview) only over edges that
  were seams *before this session's segments* put them there — i.e. it
  visualises "pre-existing seam erased here". Erasing a just-committed
  mark segment therefore removes the red line instead of stacking a grey
  one on top of it; erasing where there was never a seam draws nothing.
- Anchor dots are shown only for endpoints of segments that still have a
  visible overlay, plus the current anchor (the live path source). Green
  dots from fully erased segments do not linger.
- `undo_last` restores everything exactly: the seam flags each edge had
  before the segment, any overlay portions an erase had removed from
  earlier segments, and the anchor history.
"""

__all__ = ("SeamSession",)


class _Segment:
    """One committed click-to-click path.

    :ivar vert_indices: ordered vert indices along the path (len(edges)+1).
    :ivar edge_indices: ordered edge indices along the path.
    :ivar prior_seam: per-edge seam state before this commit (parallel to
        edge_indices), for exact undo.
    :ivar clearing: True for an erase commit, False for a mark commit.
    :ivar live_edges: subset of edge_indices this segment still shows an
        overlay for (see module docstring).
    :ivar suppressed: [(segment_position, edge_index)] — overlay edges this
        erase commit removed from EARLIER segments, so undo can restore
        them.
    """

    __slots__ = ("vert_indices", "edge_indices", "prior_seam", "clearing",
                 "live_edges", "suppressed")

    def __init__(self, vert_indices, edge_indices, prior_seam, clearing):
        self.vert_indices = vert_indices
        self.edge_indices = edge_indices
        self.prior_seam = prior_seam
        self.clearing = clearing
        self.live_edges = set(edge_indices)
        self.suppressed = []


class SeamSession:
    """Commit/erase/undo state of one interactive seam-path session."""

    def __init__(self):
        self.anchors = []    # committed click vert indices, in order
        self.segments = []   # committed _Segment records, in order

    # -- anchors -----------------------------------------------------------

    @property
    def current_anchor(self):
        """Vert index the next path starts from, or None before the first
        click."""
        return self.anchors[-1] if self.anchors else None

    def add_anchor(self, v_index):
        """Place the first anchor. Later anchors are appended by
        `commit_segment`."""
        if self.anchors:
            raise ValueError("add_anchor is only valid before the first "
                             "segment; commit_segment advances the anchor")
        self.anchors.append(v_index)

    # -- commit / undo -----------------------------------------------------

    def commit_segment(self, v_from, edges, clearing):
        """Commit one path segment: set/clear seam flags on *edges* and
        record everything needed for exact undo and for the overlay model.

        :arg v_from: BMVert the path starts at; must be the current anchor.
        :arg edges: ordered, non-empty list of BMEdge from v_from to the
            new anchor (as returned by core.path_from_tree).
        :arg clearing: True to erase seams along the path, False to mark.
        :return: the internal segment record (mainly for tests).
        """
        if not edges:
            raise ValueError("commit_segment needs a non-empty edge list")
        if v_from.index != self.current_anchor:
            raise ValueError("segment must start at the current anchor "
                             "(%r), got vert %d"
                             % (self.current_anchor, v_from.index))

        vert_indices = [v_from.index]
        v = v_from
        for e in edges:
            v = e.other_vert(v)
            vert_indices.append(v.index)
        edge_indices = [e.index for e in edges]
        prior_seam = [e.seam for e in edges]

        # The actual mesh mutation.
        state = not clearing
        for e in edges:
            e.seam = state

        seg = _Segment(vert_indices, edge_indices, prior_seam, clearing)
        if clearing:
            # Remove covered portions from earlier mark segments' overlays
            # (recording them for undo) ...
            erased = set(edge_indices)
            for pos, prev in enumerate(self.segments):
                if prev.clearing:
                    continue
                dead = prev.live_edges & erased
                if dead:
                    seg.suppressed.extend((pos, ei) for ei in dead)
                    prev.live_edges -= dead
            # ... and only show this erase over PRE-EXISTING seams: edges
            # that were seams but not because a still-live session mark
            # put them there.
            session_marked = {ei for _, ei in seg.suppressed}
            seg.live_edges = {ei for ei, was in zip(edge_indices, prior_seam)
                              if was and ei not in session_marked}

        self.segments.append(seg)
        self.anchors.append(vert_indices[-1])
        return seg

    def undo_last(self, bm):
        """Undo the last commit (or, before any commit, remove the first
        anchor): restore each edge's prior seam flag, re-expose overlay
        portions an undone erase had removed, retreat the anchor.

        :return: True if something was undone, False if there was nothing
            to undo.
        """
        if not self.segments:
            if self.anchors:
                self.anchors.clear()
                return True
            return False
        seg = self.segments.pop()
        bm.edges.ensure_lookup_table()
        for ei, was in zip(seg.edge_indices, seg.prior_seam):
            bm.edges[ei].seam = was
        for pos, ei in seg.suppressed:
            self.segments[pos].live_edges.add(ei)
        if len(self.anchors) > 1:
            self.anchors.pop()
        return True

    # -- overlay model -------------------------------------------------------

    def overlay_polylines(self):
        """What the committed-segment overlay should show right now.

        :return: list of (vert_index_chain, is_erase) — each chain is an
            ordered list of vert indices forming one polyline. A segment
            whose live edges are no longer contiguous yields multiple
            chains; a segment with no live edges yields none.
        """
        out = []
        for seg in self.segments:
            run = None
            for k, ei in enumerate(seg.edge_indices):
                if ei in seg.live_edges:
                    if run is None:
                        run = [seg.vert_indices[k]]
                    run.append(seg.vert_indices[k + 1])
                elif run is not None:
                    out.append((run, seg.clearing))
                    run = None
            if run is not None:
                out.append((run, seg.clearing))
        return out

    def overlay_anchor_indices(self):
        """Vert indices that should be drawn as anchor dots: endpoints of
        segments that still show an overlay, plus the current anchor."""
        out = []
        seen = set()
        for seg in self.segments:
            if not seg.live_edges:
                continue
            for vi in (seg.vert_indices[0], seg.vert_indices[-1]):
                if vi not in seen:
                    seen.add(vi)
                    out.append(vi)
        cur = self.current_anchor
        if cur is not None and cur not in seen:
            out.append(cur)
        return out
