# SPDX-License-Identifier: GPL-2.0-or-later
"""Atomic, memory-bounded tile history for GPU-resident paint sessions.

Snapshots are opaque backend-owned objects.  A backend may therefore return
GPU textures/buffers or CPU test data; Impasto never forces readback.  One
record contains every channel tile touched by a stroke and moves atomically
between the undo and redo stacks.
"""

from dataclasses import dataclass


class TileHistoryError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class TileKey:
    channel: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class TileSnapshot:
    """Opaque tile storage plus exact memory accounting."""

    payload: object
    byte_size: int

    def __post_init__(self):
        if self.byte_size < 0:
            raise ValueError("snapshot byte_size must be non-negative")


@dataclass(frozen=True)
class TileDelta:
    key: TileKey
    before: TileSnapshot
    after: TileSnapshot

    @property
    def byte_size(self):
        return self.before.byte_size + self.after.byte_size


@dataclass(frozen=True)
class StrokeRecord:
    sequence: int
    label: str
    deltas: tuple

    @property
    def byte_size(self):
        return sum(delta.byte_size for delta in self.deltas)


def tiles_for_rect(channel, rect, image_size, tile_size=128):
    """Return deterministic clipped tile keys intersecting an XYWH rect."""
    x, y, width, height = (int(v) for v in rect)
    image_width, image_height = (int(v) for v in image_size)
    tile_size = int(tile_size)
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    x0, y0 = max(0, x), max(0, y)
    x1 = min(image_width, x + max(0, width))
    y1 = min(image_height, y + max(0, height))
    if x1 <= x0 or y1 <= y0:
        return ()
    result = []
    first_x, first_y = x0 // tile_size, y0 // tile_size
    last_x, last_y = (x1 - 1) // tile_size, (y1 - 1) // tile_size
    for tile_y in range(first_y, last_y + 1):
        for tile_x in range(first_x, last_x + 1):
            px, py = tile_x * tile_size, tile_y * tile_size
            result.append(TileKey(
                str(channel), px, py,
                min(tile_size, image_width - px),
                min(tile_size, image_height - py)))
    return tuple(result)


class TileHistory:
    """Undo/redo records with a strict byte budget and FIFO eviction."""

    def __init__(self, memory_budget_bytes=256 * 1024 * 1024):
        memory_budget_bytes = int(memory_budget_bytes)
        if memory_budget_bytes < 0:
            raise ValueError("memory budget must be non-negative")
        self.memory_budget_bytes = memory_budget_bytes
        self._undo = []
        self._redo = []
        self._bytes = 0
        self._sequence = 0

    @property
    def byte_size(self):
        return self._bytes

    @property
    def undo_count(self):
        return len(self._undo)

    @property
    def redo_count(self):
        return len(self._redo)

    def begin_stroke(self, backend, label="Paint stroke"):
        return StrokeTransaction(self, backend, label)

    def _release_snapshot(self, backend, snapshot):
        release = getattr(backend, "release_tile", None)
        if release is not None:
            release(snapshot)

    def _release_record(self, backend, record):
        for delta in record.deltas:
            self._release_snapshot(backend, delta.before)
            self._release_snapshot(backend, delta.after)

    def _discard_records(self, backend, records):
        for record in records:
            self._bytes -= record.byte_size
            self._release_record(backend, record)
        records.clear()

    def _commit(self, backend, label, deltas):
        self._sequence += 1
        record = StrokeRecord(self._sequence, str(label), tuple(deltas))
        # A stroke that cannot fit is intentionally not partially retained.
        if not record.deltas or record.byte_size > self.memory_budget_bytes:
            self._release_record(backend, record)
            return None
        self._discard_records(backend, self._redo)
        self._undo.append(record)
        self._bytes += record.byte_size
        # Oldest committed strokes are evicted first, deterministically.
        while self._bytes > self.memory_budget_bytes and self._undo:
            evicted = self._undo.pop(0)
            self._bytes -= evicted.byte_size
            self._release_record(backend, evicted)
        return record

    def _apply(self, backend, record, before):
        applied = []
        try:
            for delta in record.deltas:
                snapshot = delta.before if before else delta.after
                backend.restore_tile(delta.key, snapshot)
                applied.append(delta)
        except Exception as exc:
            # Restore the state that existed before this failed operation.
            for delta in reversed(applied):
                rollback = delta.after if before else delta.before
                backend.restore_tile(delta.key, rollback)
            raise TileHistoryError("atomic tile restore failed") from exc

    def undo(self, backend):
        if not self._undo:
            return None
        record = self._undo[-1]
        self._apply(backend, record, before=True)
        self._undo.pop()
        self._redo.append(record)
        return record

    def redo(self, backend):
        if not self._redo:
            return None
        record = self._redo[-1]
        self._apply(backend, record, before=False)
        self._redo.pop()
        self._undo.append(record)
        return record

    def clear(self, backend):
        self._discard_records(backend, self._undo)
        self._discard_records(backend, self._redo)
        self._bytes = 0


class StrokeTransaction:
    """Lazy before/after capture boundary for one multichannel stroke."""

    def __init__(self, history, backend, label):
        self._history = history
        self._backend = backend
        self._label = label
        self._before = {}
        self._closed = False

    def touch(self, key):
        """Capture a tile once, immediately before its first modification."""
        if self._closed:
            raise TileHistoryError("stroke transaction is closed")
        if key not in self._before:
            self._before[key] = self._backend.capture_tile(key)

    def touch_rect(self, channel, rect, image_size, tile_size=128):
        keys = tiles_for_rect(channel, rect, image_size, tile_size)
        for key in keys:
            self.touch(key)
        return keys

    def commit(self):
        if self._closed:
            raise TileHistoryError("stroke transaction is closed")
        self._closed = True
        deltas = []
        try:
            for key in sorted(self._before):
                after = self._backend.capture_tile(key)
                deltas.append(TileDelta(key, self._before[key], after))
        except Exception:
            for snapshot in self._before.values():
                self._history._release_snapshot(self._backend, snapshot)
            for delta in deltas:
                self._history._release_snapshot(self._backend, delta.after)
            raise
        return self._history._commit(self._backend, self._label, deltas)

    def cancel(self):
        """Restore all touched tiles and retain no history record."""
        if self._closed:
            return
        self._closed = True
        for key, snapshot in reversed(tuple(self._before.items())):
            self._backend.restore_tile(key, snapshot)
            self._history._release_snapshot(self._backend, snapshot)

