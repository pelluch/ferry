"""Sync plan computation — what would change if we synced now.

Pure functions over the inputs (current RomM listing, stored LibraryState).
Consumers of the resulting plan are:

- the download path → executes `to_add` and `to_update`
- the delete-on-remove path → executes `to_delete`
- `ferry sync --dry-run` → prints the plan and exits

Change detection compares `state.source_romm_md5` (a md5 computed via
RomM's `largest-inner-file-for-archives` algorithm, populated by the
download path or by lazy hydration over local files) against the
server's `rom.md5_hash`. RomM's row-level `updated_at` is unreliable —
its scan path bumps `updated_at` on every rescan when refreshing
cover/screenshot/manual asset paths even when the underlying file is
identical, which would otherwise trigger a full re-download of the
library on every metadata refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ferry.domain.destination import Destination
from ferry.domain.state import LibraryState, RomState


@dataclass(frozen=True, slots=True, kw_only=True)
class AddAction:
    """A ROM in RomM that ferry has never seen before."""

    rom_id: int
    name: str
    platform_slug: str
    rom_data: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateAction:
    """A ROM ferry has, but RomM's `updated_at` has moved since we last synced."""

    rom_id: int
    name: str
    platform_slug: str
    rom_data: dict[str, Any]
    previous: RomState
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteAction:
    """A ROM in stored state that's no longer in the configured collection."""

    rom_id: int
    name: str
    platform_slug: str
    previous: RomState
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncPlan:
    to_add: list[AddAction]
    to_update: list[UpdateAction]
    to_delete: list[DeleteAction]
    unchanged_count: int

    @property
    def is_empty(self) -> bool:
        return not (self.to_add or self.to_update or self.to_delete)

    @property
    def total_changes(self) -> int:
        return len(self.to_add) + len(self.to_update) + len(self.to_delete)


def compute_plan(
    *,
    current_roms: list[dict[str, Any]],
    state: LibraryState,
    destination: Destination | None = None,
) -> SyncPlan:
    """Diff *current_roms* (from RomM) against *state* (last sync's record).

    Returns a SyncPlan with per-rom decisions. Output is stable: actions
    within each list are sorted by `name`.

    The planner always populates `to_delete` for ROMs that exist in state
    but not in the current listing — this is informational. Whether those
    deletions actually execute is a *runtime* decision controlled by
    `[sync].delete_on_remove` and applied in the executor, not here. That
    keeps `--dry-run` honest about what's no longer in the collection
    regardless of the user's safety preference.

    When `destination` is provided, the planner additionally stats each
    "unchanged" ROM's primary output. Missing-on-disk primaries are
    promoted to `to_update` with a re-sync reason — this catches the case
    where the user manually deleted files from the ROM tree and expects
    `ferry sync` to put them back. When `destination` is None (e.g. unit
    tests), the check is skipped and `updated_at` matches mean unchanged.
    """
    to_add: list[AddAction] = []
    to_update: list[UpdateAction] = []
    to_delete: list[DeleteAction] = []
    unchanged = 0

    current_ids: set[int] = set()
    for rom in current_roms:
        rom_id = rom.get("id")
        if not isinstance(rom_id, int):
            # Defensive: skip rows we can't identify rather than raise.
            continue
        current_ids.add(rom_id)

        name = _display_name(rom)
        platform = rom.get("platform_slug") or "?"
        prev = state.roms.get(rom_id)

        if prev is None:
            to_add.append(
                AddAction(
                    rom_id=rom_id,
                    name=name,
                    platform_slug=platform,
                    rom_data=rom,
                    reason="new in RomM",
                )
            )
            continue

        if _content_changed(rom, prev):
            to_update.append(
                UpdateAction(
                    rom_id=rom_id,
                    name=name,
                    platform_slug=platform,
                    rom_data=rom,
                    previous=prev,
                    reason=_change_reason(rom, prev),
                )
            )
        elif destination is not None and _primary_missing(prev, destination):
            to_update.append(
                UpdateAction(
                    rom_id=rom_id,
                    name=name,
                    platform_slug=platform,
                    rom_data=rom,
                    previous=prev,
                    reason="primary output missing on disk — re-syncing",
                )
            )
        else:
            unchanged += 1

    for rom_id, prev in state.roms.items():
        if rom_id not in current_ids:
            to_delete.append(
                DeleteAction(
                    rom_id=rom_id,
                    name=prev.name,
                    platform_slug=prev.platform_slug,
                    previous=prev,
                    reason="no longer in collection",
                )
            )

    to_add.sort(key=lambda a: (a.name, a.rom_id))
    to_update.sort(key=lambda a: (a.name, a.rom_id))
    to_delete.sort(key=lambda a: (a.name, a.rom_id))

    return SyncPlan(
        to_add=to_add,
        to_update=to_update,
        to_delete=to_delete,
        unchanged_count=unchanged,
    )


def _display_name(rom: dict[str, Any]) -> str:
    """Best-effort human-readable name for a RomM rom row."""
    return rom.get("name") or rom.get("fs_name_no_ext") or rom.get("fs_name") or "?"


def _primary_missing(prev: RomState, destination: Destination) -> bool:
    """Return True when the previously-recorded primary output is gone from disk."""
    primary_path: Path = destination.roms_base / prev.primary_output.path
    return not primary_path.exists()


def _content_changed(rom: dict[str, Any], prev: RomState) -> bool:
    """True iff the API rom-row's RomM-style md5 differs from stored state.

    Both sides use RomM's `largest-inner-file-for-archives` md5 algorithm
    (see `adapters/orphan_hash.hash_orphan_file` for ferry's mirror).
    Equality ⇒ same file content; we skip re-download regardless of any
    `updated_at` drift.

    Conservative branches when we can't make a clean equality call:
      - state lacks `source_romm_md5` (legacy entries before lazy
        hydration ran, or hydration failed because the file was missing)
        → flag for update; the re-download populates the field for next
        sync.
      - server omits `md5_hash` (incomplete API response) → flag for
        update; spurious downloads beat silently missing a real change.

    Both fallbacks resolve themselves on the next sync after a real
    re-download, so persistent flapping isn't possible.
    """
    if not prev.source_romm_md5:
        return True
    server_md5 = rom.get("md5_hash")
    if not isinstance(server_md5, str) or not server_md5:
        return True
    return prev.source_romm_md5 != server_md5


def _change_reason(rom: dict[str, Any], prev: RomState) -> str:
    server_md5 = rom.get("md5_hash")
    if not prev.source_romm_md5:
        return "no stored RomM-style md5 — re-syncing to populate state"
    if not isinstance(server_md5, str) or not server_md5:
        return "RomM did not surface md5_hash — re-syncing conservatively"
    return f"md5 changed ({prev.source_romm_md5} → {server_md5})"
