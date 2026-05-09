"""Lazy hydration of `RomState.source_romm_md5` for legacy state files.

`source_romm_md5` was added to `RomState` after ferry had already
shipped — existing state files load with the field defaulted to None.
Without backfilling, every rom in legacy state would hit
`compute_plan`'s "no stored RomM-style md5" branch and get flagged for
re-download on the next sync, even though their on-disk files are
intact.

`hydrate_romm_md5` walks state, hashes each rom's primary output via
`hash_orphan_file` (RomM's algorithm — largest-inner-file for
archives, direct md5 for non-archives), and returns an updated state
with the field populated.

**Resumable.** A library of hundreds of ROMs can take many minutes
(storage-read-bound; SD-card libraries are the worst case). Hydration
periodically calls an `on_checkpoint(partial_state, hydrated_so_far)`
callback so the caller can persist progress; on next run the
already-populated entries are skipped. `save_state` writes are atomic
(rename pattern), so worst-case after a crash or SIGKILL is losing the
entries since the last checkpoint.

Idempotent: roms that already have `source_romm_md5` populated are
left alone. Files missing on disk are silently skipped — those will
hit `_primary_missing` in `compute_plan` and re-download, populating
the field naturally.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ferry.adapters.orphan_hash import hash_orphan_file
from ferry.domain.state import LibraryState, RomState

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[LibraryState, int, RomState], None]
CheckpointCallback = Callable[[LibraryState, int], None]

# Checkpoint cadence: small enough that an interrupted run loses
# bounded work, large enough that state.json writes don't dominate.
# Each write is atomic and ~hundreds of KB; 20 checkpoints per minute
# is comfortably below disk I/O concerns even on slow SD cards.
DEFAULT_CHECKPOINT_EVERY = 25


def hydrate_romm_md5(
    state: LibraryState,
    roms_base: Path,
    *,
    on_progress: ProgressCallback | None = None,
    on_checkpoint: CheckpointCallback | None = None,
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
) -> tuple[LibraryState, int]:
    """Backfill `RomState.source_romm_md5` for roms missing it.

    Returns `(new_state, hydrated_count)`. `hydrated_count` is how many
    roms had their hash computed this pass — useful for caller logging.
    Returns the original state unchanged when there's nothing to do
    (zero-cost steady-state behavior after the first sync).

    Two optional callbacks for distinct concerns:

    - `on_progress(partial_state, hydrated_so_far, rom)` fires after
      every successfully-hydrated rom. UI-facing — typically used by
      the CLI to print "N/total hashed: <rom name>" lines so the user
      sees progress on long runs.
    - `on_checkpoint(partial_state, hydrated_so_far)` fires every
      `checkpoint_every` successfully-hydrated entries. Persistence-
      facing — the caller typically saves `partial_state` so an
      interrupted run can resume from there.

    Files that are skipped (missing on disk, unhashable) don't fire
    either callback and don't count toward the checkpoint cadence.
    """
    needs = [r for r in state.roms.values() if not r.source_romm_md5]
    if not needs:
        return state, 0

    new_roms = dict(state.roms)
    hydrated = 0
    since_checkpoint = 0
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
        new_roms[rom.rom_id] = replace(rom, source_romm_md5=romm_md5)
        hydrated += 1
        since_checkpoint += 1
        partial = replace(state, roms=new_roms)
        if on_progress is not None:
            on_progress(partial, hydrated, rom)
        if on_checkpoint is not None and since_checkpoint >= checkpoint_every:
            on_checkpoint(partial, hydrated)
            since_checkpoint = 0

    if hydrated == 0:
        return state, 0

    return replace(state, roms=new_roms), hydrated
