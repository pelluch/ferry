"""Execute a SyncPlan: download → transform → land → record state.

Executes serially (DESIGN.md §9 keeps parallel downloads as a v2+ deferral).
State is persisted after every successful ROM so a crash partway through a
big sync doesn't lose accounting for the ROMs that already landed.

Failure isolation: a ROM that fails to download or transform doesn't take
down the rest of the sync. The executor records the failure, leaves the
scratch dir for that ROM in place for debugging, and continues. Per-ROM
final state is summarized at the end.

Deletes are NOT executed yet — `to_delete` actions are surfaced in the
output but no files are touched. Soft-delete to a trash directory plus
retention purge lands in the delete-on-remove checkpoint.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ferry.adapters.romm import RommApi, RommApiError
from ferry.adapters.sidecar import (
    SIDECAR_PREFIX,
    SIDECAR_SUFFIX,
    sidecar_path_for,
    write_sidecar,
)
from ferry.adapters.state_store import save_state
from ferry.config import TransformsConfig
from ferry.config.schema import Config
from ferry.domain.destination import Destination
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.rom_files import resolve_local_filename
from ferry.domain.state import LibraryState, RomState, TransformedOutput
from ferry.domain.sync_plan import AddAction, DeleteAction, SyncPlan, UpdateAction
from ferry.services.pipeline import run_pipeline
from ferry.services.trash import trash_paths

logger = logging.getLogger(__name__)

# Priority order for picking which output is the launchable when a transform
# produces multiple files (multi-disc zips, etc.). DESIGN.md §5.1 wants this
# user-configurable; v1 hardcodes since `unzip` is the only multi-output
# transform we ship.
_PRIMARY_PRIORITY = (".m3u", ".cue", ".chd", ".rvz", ".iso", ".bin", ".zip")


@dataclass(frozen=True, slots=True)
class RomSuccess:
    rom_id: int
    name: str
    platform_slug: str
    bytes_downloaded: int
    output_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class RomFailure:
    rom_id: int
    name: str
    platform_slug: str
    error: str


@dataclass(frozen=True, slots=True)
class RomDeletion:
    rom_id: int
    name: str
    platform_slug: str
    trash_dir: Path


@dataclass(slots=True)
class ExecutionResult:
    succeeded: list[RomSuccess] = field(default_factory=list)
    failed: list[RomFailure] = field(default_factory=list)
    deleted: list[RomDeletion] = field(default_factory=list)


# Progress reporter contract: called once per ROM with the action index/total
# at the start, and again with the result at the end.
ProgressFn = Callable[[str], None]


def execute_plan(
    *,
    plan: SyncPlan,
    config: Config,
    api: RommApi,
    state: LibraryState,
    state_path: Path,
    scratch_root: Path,
    trash_root: Path,
    delete_on_remove: bool = False,
    progress: ProgressFn = lambda _msg: None,
    on_rom_delete: Callable[[RomState, Path], None] | None = None,
) -> ExecutionResult:
    """Execute *plan* against the live RomM and the local filesystem.

    Order: deletes first (free disk + clear stale state), then adds and
    updates. Mutates `state` (the in-memory copy) and writes `state_path`
    after each successful ROM. Returns aggregated counts.

    `delete_on_remove` controls whether `plan.to_delete` actually executes.
    When False (the default, mirroring the config default), the planner's
    delete entries are surfaced informationally but no files move.

    `on_rom_delete` is invoked once per successfully-trashed ROM with
    (rom_state, trash_dir) — the SaveBackend uses this to trash any saves
    associated with the removed ROM into the same trash dir. Failures in
    the callback are logged but don't fail the delete.
    """
    if config.destination is None:
        raise ValueError("execute_plan requires config.destination to be set")

    destination = config.destination
    transforms_cfg = config.transforms
    result = ExecutionResult()

    # Deletes first (only when opted into).
    pending_deletes = plan.to_delete if delete_on_remove else []
    delete_total = len(pending_deletes)
    for index, delete in enumerate(pending_deletes, start=1):
        prefix = f"[del {index}/{delete_total}]"
        progress(
            f"{prefix} trashing {delete.name} ({delete.platform_slug}, rom_id={delete.rom_id})"
        )
        try:
            trash_dir = _execute_delete(
                action=delete,
                destination=destination,
                trash_root=trash_root,
            )
        except Exception as e:
            logger.exception("delete failed for rom %d", delete.rom_id)
            progress(f"{prefix}   ✗ {type(e).__name__}: {e}")
            result.failed.append(
                RomFailure(
                    rom_id=delete.rom_id,
                    name=delete.name,
                    platform_slug=delete.platform_slug,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            continue
        if on_rom_delete is not None:
            try:
                on_rom_delete(delete.previous, trash_dir)
            except Exception as e:
                # Save trashing isn't critical to the rom delete; log and continue.
                logger.exception("on_rom_delete callback failed for rom %d", delete.rom_id)
                progress(f"{prefix}   (save trash callback warning: {type(e).__name__}: {e})")
        state.roms.pop(delete.rom_id, None)
        save_state(state, state_path)
        result.deleted.append(
            RomDeletion(
                rom_id=delete.rom_id,
                name=delete.name,
                platform_slug=delete.platform_slug,
                trash_dir=trash_dir,
            )
        )
        progress(f"{prefix}   ✓ moved to {trash_dir}")

    actions: list[AddAction | UpdateAction] = [*plan.to_add, *plan.to_update]
    total = len(actions)

    for index, action in enumerate(actions, start=1):
        prefix = f"[{index}/{total}]"
        verb = "downloading" if isinstance(action, AddAction) else "updating"
        progress(f"{prefix} {verb} {action.name} ({action.platform_slug}, rom_id={action.rom_id})")

        try:
            new_state = _execute_one(
                action=action,
                destination=destination,
                transforms_cfg=transforms_cfg,
                api=api,
                scratch_root=scratch_root,
                trash_root=trash_root,
            )
        except RommApiError as e:
            logger.warning("rom %d failed: %s", action.rom_id, e)
            progress(f"{prefix}   ✗ {e}")
            result.failed.append(
                RomFailure(
                    rom_id=action.rom_id,
                    name=action.name,
                    platform_slug=action.platform_slug,
                    error=str(e),
                )
            )
            continue
        except Exception as e:
            logger.exception("rom %d failed", action.rom_id)
            progress(f"{prefix}   ✗ {type(e).__name__}: {e}")
            result.failed.append(
                RomFailure(
                    rom_id=action.rom_id,
                    name=action.name,
                    platform_slug=action.platform_slug,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            continue

        # Update state after each successful ROM, then persist.
        state.roms[new_state.rom_id] = new_state
        save_state(state, state_path)

        absolute_outputs = tuple(destination.roms_base / o.path for o in new_state.outputs)
        result.succeeded.append(
            RomSuccess(
                rom_id=new_state.rom_id,
                name=new_state.name,
                platform_slug=new_state.platform_slug,
                bytes_downloaded=new_state.source_size,
                output_paths=absolute_outputs,
            )
        )
        progress(
            f"{prefix}   ✓ {_format_bytes(new_state.source_size)} → "
            f"{absolute_outputs[0]}"
            + (f" (+ {len(absolute_outputs) - 1} more)" if len(absolute_outputs) > 1 else "")
        )

    return result


def _execute_delete(
    *,
    action: DeleteAction,
    destination: Destination,
    trash_root: Path,
) -> Path:
    """Move all of *action.previous*'s outputs + sidecar to the trash dir."""
    rom = action.previous
    primary_abs = destination.roms_base / rom.primary_output.path
    paths: list[Path | tuple[Path, Path]] = [destination.roms_base / o.path for o in rom.outputs]
    sidecar = sidecar_path_for(primary_abs, roms_base=destination.roms_base)
    if sidecar.exists():
        paths.append((sidecar, _trash_rel_for_sidecar(rom.primary_output.path)))
    return trash_paths(
        paths,
        rom.rom_id,
        trash_root=trash_root,
        roms_base=destination.roms_base,
    )


def _trash_rel_for_sidecar(primary_rel: str) -> Path:
    """Trash-dir-relative path for a sidecar.

    Mirrors the v2 dot-prefixed legacy layout (next-to-rom). On manual
    restore (`mv <trash>/* ~/ROMs/`), the sidecar lands at the legacy
    fallback path and `read_sidecar`'s legacy-fallback handles it.
    Re-syncing then re-promotes it to the canonical location.
    """
    primary = Path(primary_rel)
    return primary.parent / (SIDECAR_PREFIX + primary.name + SIDECAR_SUFFIX)


def _execute_one(
    *,
    action: AddAction | UpdateAction,
    destination: Destination,
    transforms_cfg: TransformsConfig,
    api: RommApi,
    scratch_root: Path,
    trash_root: Path,
) -> RomState:
    rom_data = action.rom_data
    rom_id = action.rom_id
    platform = action.platform_slug
    # `fs_name` is RomM's on-server name — for nested-single-file ROMs it's the
    # parent folder, not the file. Use it for the URL (RomM identifies the rom
    # by `rom_id`; the URL filename is just for content-disposition) but
    # resolve a separate `local_filename` (with extension) for the on-disk
    # scratch path and the `source_filename` we record in state.
    fs_name = rom_data.get("fs_name") or f"rom-{rom_id}"
    local_filename = resolve_local_filename(rom_data, logger=logger)

    rom_scratch = scratch_root / str(rom_id)
    if rom_scratch.exists():
        # Stale scratch from a previous failed run — clear it before we start.
        shutil.rmtree(rom_scratch, ignore_errors=True)
    rom_scratch.mkdir(parents=True)

    succeeded = False
    try:
        source_path = rom_scratch / local_filename
        download_result = api.download_rom(rom_id, fs_name, source_path)
        # Note: we don't cross-check `download_result.md5` against
        # rom_data.get("md5_hash") even when RomM provides one. RomM
        # decompresses archives before hashing (its scanner reads zip/tar/
        # gz/7z contents), so RomM's md5 is over the *underlying ROM bytes*
        # while ours is over the *as-served bytes* (often a zip wrapper).
        # They're different by design. Real integrity verification would need
        # to hash post-unzip outputs and compare to RomM's hash — only
        # meaningful when unzip is configured. Future work; not load-bearing
        # for v1.

        platform_dir = destination.roms_base / resolve_platform_dir(platform)
        pipeline_scratch = rom_scratch / "pipeline"
        pipeline_outputs = run_pipeline(
            source_path=source_path,
            transforms=transforms_cfg.for_platform(platform),
            final_dir=platform_dir,
            scratch_dir=pipeline_scratch,
        )

        previous = action.previous if isinstance(action, UpdateAction) else None
        if previous is not None:
            _cleanup_orphans(
                previous=previous,
                roms_base=destination.roms_base,
                new_outputs=pipeline_outputs,
                trash_root=trash_root,
                rom_id=rom_id,
            )

        outputs = tuple(
            TransformedOutput(
                path=str(p.relative_to(destination.roms_base)),
                md5=_hash_file(p),
                size=p.stat().st_size,
            )
            for p in pipeline_outputs
        )
        primary_index = _pick_primary_index(pipeline_outputs)

        new_state = RomState(
            rom_id=rom_id,
            platform_slug=platform,
            name=action.name,
            source_filename=local_filename,
            source_md5=download_result.md5,
            source_size=download_result.size,
            source_updated_at=str(rom_data.get("updated_at", "")),
            transforms=tuple(transforms_cfg.for_platform(platform)),
            outputs=outputs,
            primary_output_index=primary_index,
            synced_at=_now_iso(),
        )
        write_sidecar(
            pipeline_outputs[primary_index],
            new_state,
            roms_base=destination.roms_base,
        )
        succeeded = True
        return new_state
    finally:
        if succeeded:
            shutil.rmtree(rom_scratch, ignore_errors=True)


def _cleanup_orphans(
    *,
    previous: RomState,
    roms_base: Path,
    new_outputs: list[Path],
    trash_root: Path,
    rom_id: int,
) -> None:
    """Trash files from the previous state that aren't in the new output set.

    Uses the same trash dir as DeleteAction so a single timestamped entry
    holds everything from one update event. Stale sidecar always trashed
    too — we'll write a fresh one at the new primary.
    """
    new_set = set(new_outputs)
    to_trash: list[Path | tuple[Path, Path]] = []
    old_primary = roms_base / previous.outputs[previous.primary_output_index].path
    old_sidecar = sidecar_path_for(old_primary, roms_base=roms_base)
    if old_sidecar.exists():
        to_trash.append((old_sidecar, _trash_rel_for_sidecar(previous.primary_output.path)))
    for old in previous.outputs:
        old_abs = roms_base / old.path
        if old_abs not in new_set and old_abs.exists():
            to_trash.append(old_abs)
    if to_trash:
        trash_paths(to_trash, rom_id, trash_root=trash_root, roms_base=roms_base)


def _hash_file(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def _pick_primary_index(outputs: list[Path]) -> int:
    if len(outputs) == 1:
        return 0
    for ext in _PRIMARY_PRIORITY:
        for i, p in enumerate(outputs):
            if p.suffix.lower() == ext:
                return i
    return 0


def _format_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{int(n)} B"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_scratch_root(env: os._Environ | dict[str, str] | None = None) -> Path:
    """Resolve the canonical scratch directory under XDG_CACHE_HOME."""
    env = env if env is not None else os.environ
    base = env.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "ferry" / "scratch"
