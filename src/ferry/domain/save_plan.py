"""Save-sync plan — what one backend's `.sync()` *would* do.

Computed by `<backend>.plan(state)`, rendered by the CLI's dry-run path,
and used internally by `.sync(state)` to drive the execution loop.
Parallel to `domain.sync_plan.SyncPlan`, which describes library-level
add/update/delete actions.

Read-only modeling of intended actions: each `PlannedSaveAction`
captures the rom, emulator/slot key, save filename, direction, and a
short human-readable reason for display in dry-run output. Counters
cover skip-equivalent outcomes (already in sync, ambiguous within
tolerance, prior records that would be dropped) so a dry-run summary
can show every disposition without listing them all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PlannedDirection = Literal["upload", "download"]


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedSaveAction:
    """One save's intended action.

    `reason` is a short human-readable phrase suitable for display in
    dry-run output (e.g., "new local save", "server has newer", "first
    sync — local newer", "conflict resolved — local newer").
    """

    rom_id: int
    rom_name: str
    emulator: str
    slot: str
    save_filename: str
    direction: PlannedDirection
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SavePlan:
    """What one save backend's next `.sync()` call would do.

    `backend_label` is used by the CLI to disambiguate output blocks
    when both RetroArch and Dolphin plans are printed back-to-back
    (e.g. `"RetroArch"` / `"Dolphin"`).

    `failed` here covers planning-time failures — couldn't list server
    saves, couldn't compute a plan for some key. Per-action execution
    failures only show up at sync time and aren't predictable in dry-run.
    """

    backend_label: str
    to_upload: tuple[PlannedSaveAction, ...] = ()
    to_download: tuple[PlannedSaveAction, ...] = ()
    skipped: int = 0
    conflicts_resolved: int = 0
    drop_prior_count: int = 0
    ambiguous: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.to_upload or self.to_download or self.ambiguous or self.failed)

    @property
    def action_count(self) -> int:
        return len(self.to_upload) + len(self.to_download)
