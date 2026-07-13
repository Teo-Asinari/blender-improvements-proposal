# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure/headless checks for the GPU brush adapter and tile undo seam."""

import sys
import traceback
from pathlib import Path
from types import SimpleNamespace as NS

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import brush_adapter, tile_undo


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


class MemoryBackend:
    """CPU stand-in for GPU tile copies; payloads remain opaque to history."""

    def __init__(self, bytes_per_tile=16):
        self.tiles = {}
        self.bytes_per_tile = bytes_per_tile
        self.released = []
        self.restores = []
        self.fail_key = None

    def capture_tile(self, key):
        value = self.tiles.get(key, 0)
        return tile_undo.TileSnapshot((key, value), self.bytes_per_tile)

    def restore_tile(self, key, snapshot):
        if key == self.fail_key:
            raise RuntimeError("simulated GPU copy failure")
        self.tiles[key] = snapshot.payload[1]
        self.restores.append((key, snapshot.payload[1]))

    def release_tile(self, snapshot):
        self.released.append(snapshot)


try:
    # ---- brush conversion --------------------------------------------
    curve = NS(points=[NS(location=(0.0, 1.0)), NS(location=(1.0, 0.0))])
    image = NS(name="Brush Alpha")
    texture = NS(name="Cloud Stamp", type="IMAGE", image=image)
    slot = NS(texture=texture, map_mode="VIEW_PLANE", angle=0.25,
              offset=(0.1, 0.2, 0.0), scale=(2.0, 2.0, 1.0))
    brush = NS(image_tool="DRAW", size=80, strength=0.75, alpha=0.5,
               spacing=25, color=(0.2, 0.4, 0.6),
               secondary_color=(0.8, 0.6, 0.4), blend="MIX",
               use_pressure_size=True, use_pressure_strength=True,
               curve_distance_falloff=curve, texture_slot=slot)
    unified = NS(use_unified_size=True, size=120,
                 use_unified_strength=False,
                 use_unified_color=False)
    stamp = brush_adapter.brush_to_gpu_stamp(brush, unified)
    check("draw brush maps to supported GPU stamp", stamp.supported)
    check("unified diameter maps to GPU radius", stamp.radius_px == 60.0)
    check("spacing percentage maps to diameter ratio",
          stamp.spacing_ratio == 0.25 and stamp.spacing_px == 30.0)
    check("alpha and strength both contribute to opacity",
          stamp.values_at_pressure(0.5) == (30.0, 0.1875))
    check("color, blend and falloff survive conversion",
          stamp.color == (0.2, 0.4, 0.6) and stamp.blend == "MIX"
          and stamp.falloff_curve == ((0.0, 1.0), (1.0, 0.0)))
    check("texture metadata is stable and GPU-upload neutral",
          stamp.texture.texture_name == "Cloud Stamp"
          and stamp.texture.image_name == "Brush Alpha"
          and stamp.texture.mapping == "VIEW_PLANE")
    clone = brush_adapter.brush_to_gpu_stamp(NS(image_tool="CLONE"))
    unknown = brush_adapter.brush_to_gpu_stamp(NS(), tool_id="builtin.smear")
    check("specialized tools are explicitly unsupported",
          not clone.supported and "clone" in clone.unsupported_reason
          and not unknown.supported and unknown.tool == "SMEAR"
          and "canvas" in unknown.unsupported_reason)

    # ---- dirty tile selection ---------------------------------------
    tiles = tile_undo.tiles_for_rect(
        "roughness", (120, 120, 40, 40), (250, 250), tile_size=128)
    check("dirty rect expands to deterministic clipped tiles",
          [(t.x, t.y, t.width, t.height) for t in tiles]
          == [(0, 0, 128, 128), (128, 0, 122, 128),
              (0, 128, 128, 122), (128, 128, 122, 122)])
    check("empty/outside rectangles produce no tiles",
          tile_undo.tiles_for_rect("x", (-10, -10, 5, 5), (64, 64)) == ())

    # ---- atomic multichannel stroke ---------------------------------
    backend = MemoryBackend()
    history = tile_undo.TileHistory(memory_budget_bytes=256)
    base = tile_undo.TileKey("base_color", 0, 0, 128, 128)
    metal = tile_undo.TileKey("metallic", 0, 0, 128, 128)
    backend.tiles.update({base: "base-before", metal: "metal-before"})
    stroke = history.begin_stroke(backend, "multichannel stroke")
    stroke.touch(base)
    stroke.touch(metal)
    stroke.touch(base)  # lazy capture is exactly once
    backend.tiles.update({base: "base-after", metal: "metal-after"})
    record = stroke.commit()
    check("one stroke creates one record across every channel",
          record is not None and len(record.deltas) == 2
          and history.undo_count == 1 and history.byte_size == 64)
    history.undo(backend)
    check("one undo restores all channels",
          backend.tiles[base] == "base-before"
          and backend.tiles[metal] == "metal-before"
          and history.undo_count == 0 and history.redo_count == 1)
    history.redo(backend)
    check("one redo restores all channels",
          backend.tiles[base] == "base-after"
          and backend.tiles[metal] == "metal-after"
          and history.undo_count == 1 and history.redo_count == 0)

    # ---- failure rollback and memory policy --------------------------
    failing = MemoryBackend()
    fail_history = tile_undo.TileHistory(memory_budget_bytes=256)
    failing.tiles.update({base: 1, metal: 1})
    tx = fail_history.begin_stroke(failing)
    tx.touch(base)
    tx.touch(metal)
    failing.tiles.update({base: 2, metal: 2})
    tx.commit()
    failing.fail_key = metal
    try:
        fail_history.undo(failing)
        check("failed atomic restore raises", False)
    except tile_undo.TileHistoryError:
        check("failed atomic restore leaves history position unchanged",
              fail_history.undo_count == 1 and fail_history.redo_count == 0)
        check("already restored channels roll forward after failure",
              failing.tiles[base] == 2 and failing.tiles[metal] == 2)

    bounded = MemoryBackend(bytes_per_tile=10)
    bounded_history = tile_undo.TileHistory(memory_budget_bytes=40)
    keys = [tile_undo.TileKey("base_color", i, 0, 1, 1)
            for i in range(3)]
    sequences = []
    for i, key in enumerate(keys):
        bounded.tiles[key] = i
        tx = bounded_history.begin_stroke(bounded, str(i))
        tx.touch(key)
        bounded.tiles[key] = i + 10
        sequences.append(tx.commit().sequence)
    check("budget evicts oldest records deterministically",
          bounded_history.undo_count == 2
          and bounded_history.byte_size == 40
          and len(bounded.released) == 2)
    oversized_backend = MemoryBackend(bytes_per_tile=30)
    oversized = tile_undo.TileHistory(memory_budget_bytes=50)
    oversized_backend.tiles[base] = 1
    tx = oversized.begin_stroke(oversized_backend)
    tx.touch(base)
    oversized_backend.tiles[base] = 2
    check("oversized atomic record is rejected, never truncated",
          tx.commit() is None and oversized.byte_size == 0
          and oversized.undo_count == 0
          and len(oversized_backend.released) == 2)

    print("IMPASTO_BRUSH_UNDO_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_BRUSH_UNDO_FAILED")
