"""Lazy hydration of `RomState.source_romm_md5` for legacy state files.

`source_romm_md5` was added to `RomState` after ferry had already
shipped — existing state files load with the field defaulted to None.
Without backfilling, every rom in legacy state would hit
`compute_plan`'s "no stored RomM-style md5" branch and get flagged for
re-download on the next sync, even though their on-disk files are
intact.

`hydrate_romm_md5` walks state once, hashes each rom's primary output
via `hash_orphan_file` (RomM's algorithm — largest-inner-file for
archives, direct md5 for non-archives), and returns an updated state
with the field populated. Caller persists immediately so a crash
mid-run doesn't lose the work.

Idempotent: roms that already have `source_romm_md5` populated are
left alone. Files missing on disk are silently skipped — those will
hit `_primary_missing` in `compute_plan` and re-download, populating
the field naturally.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from ferry.adapters.orphan_hash import hash_orphan_file
from ferry.domain.state import LibraryState, RomState

logger = logging.getLogger(__name__)


def hydrate_romm_md5(state: LibraryState, roms_base: Path) -> tuple[LibraryState, int]:
    """Backfill `RomState.source_romm_md5` for roms missing it.

    Returns `(new_state, hydrated_count)`. `hydrated_count` is how many
    roms had their hash computed this pass — useful for caller logging.
    Returns the original state unchanged when there's nothing to do
    (zero-cost steady-state behavior after the first sync).
    """
    needs = [r for r in state.roms.values() if not r.source_romm_md5]
    if not needs:
        return state, 0

    updates: dict[int, RomState] = {}
    for rom in needs:
        primary_path = roms_base / rom.primary_output.path
        if not primary_path.is_file():
            # Missing on disk — `_primary_missing` will catch it in the
            # planner and trigger re-download, which populates the
            # field via the executor.
            logger.debug(
                "skipping hydrate for rom_id=%d (%s): primary output missing at %s",
                rom.rom_id,
                rom.name,
                primary_path,
            )
            continue
        try:
            romm_md5 = hash_orphan_file(primary_path)
        except OSError as exc:
            logger.warning(
                "could not hash %s for rom_id=%d (%s): %s",
                primary_path,
                rom.rom_id,
                rom.name,
                exc,
            )
            continue
        if not romm_md5:
            # `hash_orphan_file` returns None on unhashable input
            # (corrupt archive that can't be opened, etc.).
            continue
        updates[rom.rom_id] = replace(rom, source_romm_md5=romm_md5)

    if not updates:
        return state, 0

    new_roms = {rom_id: updates.get(rom_id, rom) for rom_id, rom in state.roms.items()}
    return replace(state, roms=new_roms), len(updates)
