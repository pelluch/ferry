"""Shared domain type for an on-disk save file matched to a known ROM.

Both v2's RetroArch walker and v3's Dolphin walker emit `LocalSave`
records; the field shape is identical (it always was — they were
deliberately mirrored in v3 against the eventual Protocol extraction).
This module is the single home; the adapter modules re-export for
backward compatibility with existing imports.

`emulator` and `slot` semantics are backend-specific:

- RetroArch: `emulator` is `"retroarch"` or `"retroarch-<core>"`
  depending on the install's sort layout; `slot` is always `"default"`
  (SRAM-style — single save per game; per-state-slot syncing is a
  future feature).
- Dolphin: `emulator` is always `"dolphin"`; `slot` is the in-game
  save name from the GCI's directory entry (e.g. `"MetroidPrime A"`,
  `"f_zero.dat"`, `"SuperSmashBros0110290334"`). One ROM can produce
  many slots when the game writes multiple .gci files (replays,
  per-character saves, system files, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalSave:
    """A save file present on disk, matched to a known ROM."""

    rom_id: int
    emulator: str
    slot: str
    save_filename: str
    local_path: Path
    local_mtime: float
    local_md5: str
    local_size: int
