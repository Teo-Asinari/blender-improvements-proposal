# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto two-tier debounce — pure, fake-clock-testable (design §4.6).

No bpy imports (enforced by tests). The bpy.app.timers driver in
engine.py is a thin shell around :meth:`DebounceState.due`; every test
drives this class with an explicit clock instead.

Two tiers:

- STRUCTURAL (default 0.1 s): coalesces compile+reconcile after model
  edits; repeated marks push the deadline (trailing debounce), so a
  burst of edits converges on one reconcile.
- PRUNE (default 3 s idle): recompiles with the grace set cleared so
  factor-0 participants left behind by visibility/enable toggles are
  slimmed out of the shader once the user pauses.
"""

STRUCTURAL_DELAY = 0.1
PRUNE_DELAY = 3.0


class DebounceState:
    def __init__(self, structural_delay=STRUCTURAL_DELAY,
                 prune_delay=PRUNE_DELAY):
        self.structural_delay = structural_delay
        self.prune_delay = prune_delay
        self.structural_at = None    # absolute deadline or None
        self.prune_at = None

    def mark_structural(self, now):
        self.structural_at = now + self.structural_delay

    def mark_prune(self, now):
        self.prune_at = now + self.prune_delay

    @property
    def pending(self):
        return self.structural_at is not None or self.prune_at is not None

    def due(self, now):
        """Consume and return the actions due at ``now``, in the order
        they must run ('structural' before 'prune')."""
        actions = []
        if self.structural_at is not None and now >= self.structural_at:
            self.structural_at = None
            actions.append("structural")
        if self.prune_at is not None and now >= self.prune_at:
            self.prune_at = None
            actions.append("prune")
        return actions

    def next_delay(self, now):
        """Seconds until the earliest pending deadline, or None when
        idle (the timer driver unregisters on None)."""
        deadlines = [d for d in (self.structural_at, self.prune_at)
                     if d is not None]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def reset(self):
        self.structural_at = None
        self.prune_at = None
