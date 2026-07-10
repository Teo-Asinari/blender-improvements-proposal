# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure debounce logic for the live seam-island refresh.

No bpy imports — the state machine takes explicit timestamps, so tests
drive it with a fake clock. ``bpy.app.timers`` is only the thin driver
(overlay._live_timer_cb), the same pattern as seam_path_tool's slice
runner. This matters because app timers never fire in
``blender --background`` (probed on 5.1.2), so anything that needs the
timer to be correct would be untestable headlessly.
"""


class Debounce:
    """Collapse a burst of change notifications into a single fire once
    ``quiet_s`` seconds have passed since the LAST notification.

    Usage: call note_change(now) on every suspected change; poll
    try_fire(now) periodically. try_fire returns True exactly once per
    burst — after that the debounce is idle until the next note_change.
    """

    __slots__ = ("quiet_s", "_last_change")

    def __init__(self, quiet_s):
        self.quiet_s = float(quiet_s)
        self._last_change = None

    @property
    def pending(self):
        """True while a burst is waiting for its quiet period."""
        return self._last_change is not None

    def note_change(self, now):
        """Record activity; (re)starts the quiet countdown."""
        self._last_change = now

    def reset(self):
        self._last_change = None

    def try_fire(self, now):
        """True once when the quiet period since the last note_change has
        elapsed; consumes the pending state."""
        if self._last_change is None:
            return False
        if now - self._last_change >= self.quiet_s:
            self._last_change = None
            return True
        return False
