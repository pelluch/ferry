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
from ferry.adapters.sidecar import sidecar_path_for, write_sidecar
from ferry.adapters.state_store import save_state
from ferry.config import TransformsConfig
from ferry.config.schema import Config
from ferry.domain.destination import Destination
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.state import LibraryState, RomState, TransformedOutput
from ferry.domain.sync_plan import AddAction, SyncPlan, UpdateAction
from ferry.services.pipeline import run_pipeline

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


@dataclass(slots=True)
class ExecutionResult:
    succeeded: list[RomSuccess] = field(default_factory=list)
    failed: list[RomFailure] = field(default_factory=list)
    skipped_deletes: int = 0


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
    progress: ProgressFn = lambda _msg: None,
) -> ExecutionResult:
    """Execute *plan* against the live RomM and the local filesystem.

    Mutates `state` (the in-memory copy) and writes `state_path` after each
    successful ROM. Returns aggregated success/failure counts.
    """
    if config.destination is None:
        raise ValueError("execute_plan requires config.destination to be set")

    destination = config.destination
    transforms_cfg = config.transforms

    actions: list[AddAction | UpdateAction] = [*plan.to_add, *plan.to_update]
    total = len(actions)
    result = ExecutionResult(skipped_deletes=len(plan.to_delete))

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


def _execute_one(
    *,
    action: AddAction | UpdateAction,
    destination: Destination,
    transforms_cfg: TransformsConfig,
    api: RommApi,
    scratch_root: Path,
) -> RomState:
    rom_data = action.rom_data
    rom_id = action.rom_id
    platform = action.platform_slug
    fs_name = rom_data.get("fs_name") or f"rom-{rom_id}"

    rom_scratch = scratch_root / str(rom_id)
    if rom_scratch.exists():
        # Stale scratch from a previous failed run — clear it before we start.
        shutil.rmtree(rom_scratch, ignore_errors=True)
    rom_scratch.mkdir(parents=True)

    succeeded = False
    try:
        source_path = rom_scratch / fs_name
        download_result = api.download_rom(rom_id, fs_name, source_path)
        _maybe_warn_hash_mismatch(rom_data, download_result.md5)

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
            source_filename=fs_name,
            source_md5=download_result.md5,
            source_size=download_result.size,
            source_updated_at=str(rom_data.get("updated_at", "")),
            transforms=tuple(transforms_cfg.for_platform(platform)),
            outputs=outputs,
            primary_output_index=primary_index,
            synced_at=_now_iso(),
        )
        write_sidecar(pipeline_outputs[primary_index], new_state)
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
) -> None:
    """Delete files from the previous state that aren't in the new output set."""
    new_set = set(new_outputs)
    # Always remove the old sidecar; we'll write a fresh one at the new primary.
    old_primary = roms_base / previous.outputs[previous.primary_output_index].path
    sidecar_path_for(old_primary).unlink(missing_ok=True)

    for old in previous.outputs:
        old_abs = roms_base / old.path
        if old_abs not in new_set:
            old_abs.unlink(missing_ok=True)


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


def _maybe_warn_hash_mismatch(rom_data: dict, our_md5: str) -> None:
    advertised = rom_data.get("md5_hash")
    if advertised and isinstance(advertised, str) and advertised.lower() != our_md5.lower():
        logger.warning(
            "rom %s: md5 mismatch — RomM advertised %s, we got %s",
            rom_data.get("id"),
            advertised,
            our_md5,
        )


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
