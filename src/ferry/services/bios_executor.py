"""Execute a BiosPlan: download firmware → place under bios_base → record state.

The BIOS analogue of `sync_executor` (v5.5). Same shape and guarantees:

- serial execution; state persisted after every file so a crash mid-sync
  doesn't lose accounting for what already landed
- failure isolation — one firmware that fails to download doesn't abort
  the rest; the failure is recorded and the sync continues
- deletes run first (gated by `delete_on_remove`), then adds + updates

A placement-change update (content unchanged, but the subfolder map moved
the file) writes to the new path and removes the stale copy at the old
one. Unverified firmware — RomM's `is_verified` is false — is synced
anyway (the RomM library is canonical) but flagged in the progress
output so the user knows the file isn't a known-good BIOS.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ferry.adapters.romm import RommApi, RommApiError
from ferry.adapters.state_store import save_state
from ferry.domain.bios_plan import BiosAddAction, BiosPlan, BiosUpdateAction
from ferry.domain.destination import Destination
from ferry.domain.format import format_bytes
from ferry.domain.state import BiosRecord, LibraryState
from ferry.services.trash import trash_bios_files

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosSuccess:
    firmware_id: int
    file_name: str
    platform_slug: str
    path: Path
    size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosFailure:
    firmware_id: int
    file_name: str
    platform_slug: str
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosDeletion:
    firmware_id: int
    file_name: str
    platform_slug: str
    trash_dir: Path


@dataclass(slots=True, kw_only=True)
class BiosExecutionResult:
    succeeded: list[BiosSuccess] = field(default_factory=list)
    failed: list[BiosFailure] = field(default_factory=list)
    deleted: list[BiosDeletion] = field(default_factory=list)


def execute_bios_plan(
    *,
    plan: BiosPlan,
    api: RommApi,
    state: LibraryState,
    state_path: Path,
    destination: Destination,
    trash_root: Path,
    delete_on_remove: bool = False,
    progress: ProgressFn = lambda _msg: None,
) -> BiosExecutionResult:
    """Execute *plan* against the live RomM and the local BIOS tree.

    Requires `destination.bios_base` to be set — the caller skips BIOS
    sync entirely (with a hint) when it's None. Mutates `state.bios` and
    persists `state_path` after every file.
    """
    if destination.bios_base is None:
        raise ValueError("execute_bios_plan requires destination.bios_base to be set")
    bios_base = destination.bios_base
    result = BiosExecutionResult()

    pending_deletes = plan.to_delete if delete_on_remove else []
    delete_total = len(pending_deletes)
    for index, delete in enumerate(pending_deletes, start=1):
        prefix = f"[bios del {index}/{delete_total}]"
        progress(f"{prefix} trashing {delete.file_name} ({delete.platform_slug})")
        try:
            trash_dir = trash_bios_files(
                [bios_base / delete.previous.path],
                delete.firmware_id,
                trash_root=trash_root,
                bios_base=bios_base,
            )
        except Exception as e:
            logger.exception("bios delete failed for firmware %d", delete.firmware_id)
            progress(f"{prefix}   ✗ {type(e).__name__}: {e}")
            result.failed.append(
                BiosFailure(
                    firmware_id=delete.firmware_id,
                    file_name=delete.file_name,
                    platform_slug=delete.platform_slug,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            continue
        state.bios.pop(delete.firmware_id, None)
        save_state(state, state_path)
        result.deleted.append(
            BiosDeletion(
                firmware_id=delete.firmware_id,
                file_name=delete.file_name,
                platform_slug=delete.platform_slug,
                trash_dir=trash_dir,
            )
        )
        progress(f"{prefix}   ✓ moved to {trash_dir}")

    actions: list[BiosAddAction | BiosUpdateAction] = [*plan.to_add, *plan.to_update]
    total = len(actions)
    for index, action in enumerate(actions, start=1):
        prefix = f"[bios {index}/{total}]"
        verb = "downloading" if isinstance(action, BiosAddAction) else "updating"
        progress(f"{prefix} {verb} {action.file_name} ({action.platform_slug})")
        if action.unverified:
            progress(f"{prefix}   ⚠ unverified — not a known-good BIOS in RomM's database")

        try:
            record = _execute_one(action=action, bios_base=bios_base, api=api)
        except RommApiError as e:
            logger.warning("bios firmware %d failed: %s", action.firmware_id, e)
            progress(f"{prefix}   ✗ {e}")
            result.failed.append(
                BiosFailure(
                    firmware_id=action.firmware_id,
                    file_name=action.file_name,
                    platform_slug=action.platform_slug,
                    error=str(e),
                )
            )
            continue
        except Exception as e:
            logger.exception("bios firmware %d failed", action.firmware_id)
            progress(f"{prefix}   ✗ {type(e).__name__}: {e}")
            result.failed.append(
                BiosFailure(
                    firmware_id=action.firmware_id,
                    file_name=action.file_name,
                    platform_slug=action.platform_slug,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            continue

        state.bios[record.firmware_id] = record
        save_state(state, state_path)
        result.succeeded.append(
            BiosSuccess(
                firmware_id=record.firmware_id,
                file_name=record.file_name,
                platform_slug=record.platform_slug,
                path=bios_base / record.path,
                size=record.size,
            )
        )
        progress(f"{prefix}   ✓ {format_bytes(record.size)} → {bios_base / record.path}")

    return result


def _execute_one(
    *,
    action: BiosAddAction | BiosUpdateAction,
    bios_base: Path,
    api: RommApi,
) -> BiosRecord:
    """Download one firmware file to its target path; return its new state record.

    `RommHttpAdapter.download` writes atomically (`.part` → rename) and
    creates parent dirs, so a flat or subfoldered target both work. On a
    placement-change update the stale file at the previous path is removed
    once the new copy has landed.
    """
    dest = bios_base / action.target_path
    download = api.download_firmware(action.firmware_id, action.file_name, dest)

    if isinstance(action, BiosUpdateAction) and action.previous.path != action.target_path:
        stale = bios_base / action.previous.path
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            # Best-effort: a leftover stale copy is harmless, not worth failing on.
            logger.warning("could not remove stale BIOS file at %s", stale)

    return BiosRecord(
        firmware_id=action.firmware_id,
        platform_slug=action.platform_slug,
        file_name=action.file_name,
        path=action.target_path,
        md5=download.md5,
        size=download.size,
    )
