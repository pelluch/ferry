import logging
from pathlib import Path
from typing import Any

import click

from ferry.adapters.cemu.cemu_paths import CemuInstall, discover_cemu_installs
from ferry.adapters.cemu.cemu_tool import (
    WiiUTitleCache,
    discover_cemu_tool,
)
from ferry.adapters.cemu.cemu_tool import (
    default_cache_path as default_wiiu_cache_path,
)
from ferry.adapters.dolphin.dolphin_paths import (
    DolphinInstall,
    discover_dolphin_installs,
)
from ferry.adapters.dolphin.dolphin_tool import (
    DiscHeaderCache,
    default_cache_path,
    discover_dolphin_tool,
)
from ferry.adapters.retroarch.retroarch_paths import (
    RetroArchInstall,
    discover_retroarch_installs,
)
from ferry.adapters.romm import (
    RommApi,
    RommApiError,
    RommAuthError,
    RommForbiddenError,
    RommHttpAdapter,
)
from ferry.adapters.state_store import (
    default_state_path,
    load_state,
    save_state,
)
from ferry.cli._utils import DEFAULT_PREVIEW
from ferry.config import ConfigError, SavesConfig, SyncConfig, load_config
from ferry.config.schema import Config
from ferry.domain.install_selection import (
    InstallResolution,
    ResolutionReason,
    resolve_install,
)
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.rom_files import resolve_local_filename
from ferry.domain.save_plan import PlannedSaveAction, SavePlan
from ferry.domain.state import LibraryState, RomState
from ferry.domain.sync_plan import (
    AddAction,
    DeleteAction,
    SyncPlan,
    UpdateAction,
    compute_plan,
)
from ferry.services.cemu_save_backend import CemuSaveBackend
from ferry.services.gamecube_save_backend import GameCubeSaveBackend
from ferry.services.launch_hooks import (
    default_snapshot_path,
    detect_drift,
    read_snapshot,
)
from ferry.services.save_backend import (
    RetroArchSaveBackend,
    SaveSyncResult,
    get_or_register_device,
)
from ferry.services.save_backend_base import SaveBackend
from ferry.services.sync_executor import (
    ExecutionResult,
    default_scratch_root,
    execute_plan,
)
from ferry.services.sync_lock import LockHeld, acquire_sync_lock, default_lock_path
from ferry.services.trash import default_trash_root, purge_expired
from ferry.services.wii_save_backend import WiiSaveBackend

logger = logging.getLogger(__name__)

# `SaveBackend` is the structural Protocol from `save_backend_base`;
# all `_prepare_save_backends` / `_run_*` helpers operate on it
# uniformly. Concrete backends (RetroArch + Dolphin) are constructed
# in `_prepare_save_backends` and stored as `list[SaveBackend]`.


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would happen without modifying anything.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Print every entry in each section instead of truncating (dry-run only).",
)
@click.option(
    "--saves-only",
    is_flag=True,
    help="Skip library reconciliation; only sync save data.",
)
@click.option(
    "--rom",
    "rom_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Sync save data only for the ROM at this path (resolves rom_id by "
        "matching against state.json outputs). Implies --saves-only. Used by "
        "launch-wrapper hooks."
    ),
)
@click.pass_context
def sync(
    ctx: click.Context,
    dry_run: bool,
    full: bool,
    saves_only: bool,
    rom_path: Path | None,
) -> None:
    """Sync the configured collection from RomM to the destination."""
    try:
        loaded = load_config(ctx.obj.get("config_path"))
    except ConfigError as e:
        raise click.ClickException(str(e)) from e

    config = loaded.config
    # --rom narrows save sync to a single game; library work is meaningless
    # in that scope, so it implies --saves-only.
    if rom_path is not None:
        saves_only = True

    if not saves_only:
        if config.sync is None:
            raise click.ClickException(
                "[sync] is required for sync. Add at least one source to your config:\n\n"
                "    [sync]\n"
                '    collections = ["Steam Deck"]\n'
                '    # or platforms = ["gba", "snes"]'
            )
        if config.destination is None:
            raise click.ClickException(
                "[destination] is required for sync. Run `ferry detect` for help."
            )

    try:
        with acquire_sync_lock(default_lock_path()):
            if saves_only:
                _run_saves_only(config, dry_run=dry_run, full=full, rom_path=rom_path)
            else:
                assert config.sync is not None  # checked above
                _run_sync(config, config.sync, dry_run=dry_run, full=full)
    except LockHeld as e:
        raise click.ClickException(
            f"another ferry sync is already running (pid {e.pid}, lock at "
            f"{e.lock_path}).\nWait for it to finish, or check `ps -p {e.pid}` "
            "if you suspect it's stuck."
        ) from e

    _warn_on_launch_hook_upstream_drift()


def _run_sync(config: Config, sync_cfg: SyncConfig, *, dry_run: bool, full: bool) -> None:
    click.echo(f"connecting to {config.romm.url}…")
    try:
        with RommHttpAdapter(config.romm, logger) as http:
            api = RommApi(http)
            collection_ids, collection_errors = _resolve_collections(api, sync_cfg.collections)
            platform_ids, platform_errors = _resolve_platforms(api, sync_cfg.platforms)
            all_errors = collection_errors + platform_errors
            if all_errors:
                raise click.ClickException("\n\n".join(all_errors))
            if sync_cfg.collections:
                click.echo(
                    f"✓ resolved {len(collection_ids)} collection(s): "
                    + ", ".join(sync_cfg.collections)
                )
            if sync_cfg.platforms:
                click.echo(
                    f"✓ resolved {len(platform_ids)} platform(s): " + ", ".join(sync_cfg.platforms)
                )
            click.echo("fetching ROMs…")
            current_roms = _fetch_roms(
                api,
                collection_ids=collection_ids,
                platform_ids=platform_ids,
                primary_only=sync_cfg.primary_version_only,
            )
            click.echo(f"✓ {len(current_roms)} unique ROM(s) after dedup")

            state_path = default_state_path()
            state = load_state(state_path)
            _warn_if_romm_hashes_disabled(current_roms)
            trash_root = default_trash_root()
            plan = compute_plan(
                current_roms=current_roms,
                state=state,
                destination=config.destination,
            )

            if dry_run:
                _print_plan(plan, full=full, config=config)
                _print_save_sync_preview(config, api, state, full=full)
                return

            # Purge expired trash *only* on the real-run path. Dry-run must
            # never modify state, including trash entries.
            purged = purge_expired(trash_root, sync_cfg.trash_retention_days)
            if purged:
                click.echo(
                    f"purged {purged} trash entr"
                    f"{'y' if purged == 1 else 'ies'} older than "
                    f"{sync_cfg.trash_retention_days} days"
                )

            _print_plan_summary(plan)
            will_act = bool(
                plan.to_add or plan.to_update or (plan.to_delete and sync_cfg.delete_on_remove)
            )
            save_backends, state = _prepare_save_backends(config, api, state, state_path)
            if not will_act:
                click.echo("")
                if plan.is_empty:
                    click.echo("Nothing to do — local state matches RomM.")
                else:
                    click.echo(
                        f"Nothing to execute — {len(plan.to_delete)} ROM(s) no "
                        "longer in collection ([sync].delete_on_remove = false)."
                    )
                    click.echo("Set delete_on_remove = true in your config to trash them.")
                _run_all_save_syncs(save_backends, state, state_path)
                return

            click.echo("")
            click.echo("Executing plan…")
            click.echo("")
            scratch_root = default_scratch_root()
            result = execute_plan(
                plan=plan,
                config=config,
                api=api,
                state=state,
                state_path=state_path,
                scratch_root=scratch_root,
                trash_root=trash_root,
                delete_on_remove=sync_cfg.delete_on_remove,
                progress=click.echo,
                on_rom_delete=(
                    (lambda rom, td: _delete_for_rom_all(save_backends, rom, td))
                    if save_backends
                    else None
                ),
            )
            _print_execution_summary(plan, result)
            _run_all_save_syncs(save_backends, state, state_path)
    except RommAuthError as e:
        raise click.ClickException(
            f"{e}\n\ncheck the API key — it may be expired or revoked."
        ) from e
    except RommApiError as e:
        raise click.ClickException(str(e)) from e


def _run_saves_only(
    config: Config,
    *,
    dry_run: bool,
    full: bool,
    rom_path: Path | None,
) -> None:
    """Save-sync-only path. Skips library reconciliation entirely.

    `--saves-only` runs a full save-sync (every backend, every tracked
    ROM). `--rom <path>` (which implies --saves-only) further narrows to
    a single ROM via the per-backend `sync_for_rom` method — used by
    launch-wrapper hooks for fast pre/post sync per game.

    Save sync requires `[saves]` to be configured. If not, this is a
    no-op with a friendly hint.
    """
    if config.saves is None or not config.saves.enabled:
        click.echo("Save sync is not configured. Add a `[saves]` section to your config.")
        return

    state_path = default_state_path()
    state = load_state(state_path)

    rom: RomState | None = None
    if rom_path is not None:
        roms_base = config.destination.roms_base if config.destination is not None else None
        rom = _find_rom_by_path(state, rom_path, roms_base=roms_base)
        if rom is None:
            raise click.ClickException(
                f"ROM at {rom_path} isn't tracked by ferry — no matching entry "
                f"in state.json. Run `ferry sync` to register the library first."
            )

    click.echo(f"connecting to {config.romm.url}…")
    try:
        with RommHttpAdapter(config.romm, logger) as http:
            api = RommApi(http)
            if dry_run:
                # The existing preview path covers the "all backends, full sync"
                # case. For --rom we'd want a narrower preview but that's
                # out of MVP scope; the preview shows what a full save sync
                # would do, which is a superset of the per-rom plan.
                _print_save_sync_preview(config, api, state, full=full)
                return
            save_backends, state = _prepare_save_backends(config, api, state, state_path)
            if not save_backends:
                return
            _run_all_save_syncs(save_backends, state, state_path, rom=rom)
    except RommAuthError as e:
        raise click.ClickException(
            f"{e}\n\ncheck the API key — it may be expired or revoked."
        ) from e
    except RommApiError as e:
        # Per the user's "RomM unreachable shouldn't block" stance, log + exit
        # 0 — the launch wrapper continues with whatever's on disk. Library
        # mode (full sync) treats RomMApiError as fatal because the user
        # explicitly invoked it; saves-only is more often run from automation
        # (launch hooks) where we want to fail soft.
        click.echo(f"save sync skipped: {e}")


def _warn_if_romm_hashes_disabled(current_roms: list[dict[str, Any]]) -> None:
    """Emit a one-line note when most/all roms in the response lack `md5_hash`.

    RomM's hash computation is configurable; large libraries sometimes
    disable it for performance. Without `rom.md5_hash`, ferry's
    `compute_plan` falls back to file-size comparison instead of
    deterministic md5 equality — same-size content replacements slip
    through that fallback. Worth telling the user once so they know
    they're on the weaker check.

    Threshold: >50% of roms missing the field. Below that, this is
    probably noise (a few unscanned files), not a global setting.
    """
    if not current_roms:
        return
    missing = sum(
        1 for rom in current_roms if not isinstance(rom.get("md5_hash"), str) or not rom["md5_hash"]
    )
    if missing > len(current_roms) // 2:
        click.echo(
            f"note: {missing}/{len(current_roms)} ROMs in RomM lack `md5_hash`; "
            "ferry will fall back to file-size comparison for change detection. "
            "Enable hash computation in RomM admin settings for deterministic "
            "checks (catches same-size file replacements that the size fallback "
            "misses)."
        )


def _warn_on_launch_hook_upstream_drift() -> None:
    """Emit a tail-of-output warning when the bundled ES-DE systems file
    changed since `install-launch-hooks` last ran.

    Surfaces the same condition `ferry status` reports, but in the sync
    output so the systemd-timer's regular runs flag it without the user
    needing to remember to run status. Local-drift-only is intentionally
    NOT echoed here — it's user-controlled (they edited the file), so
    nagging on every timer fire would be noise. They'll see it on next
    `ferry status`.
    """
    snapshot = read_snapshot(default_snapshot_path())
    if snapshot is None:
        return
    drift = detect_drift(snapshot)
    if not drift.upstream_drift:
        return
    click.echo("")
    click.echo(
        "⚠ launch hooks: bundled `es_systems.xml` changed since "
        "`ferry install-launch-hooks` last ran. Re-run that command to "
        "refresh the managed block. (`ferry status` shows full drift state.)"
    )


def _find_rom_by_path(
    state: LibraryState, rom_path: Path, *, roms_base: Path | None
) -> RomState | None:
    """Resolve a ROM file path to its RomState by scanning state outputs.

    Returns None when no rom in state has *rom_path* among its
    `outputs[]`. The caller surfaces a friendly error.

    Compares as a relative path under `roms_base` when one is given
    (state stores outputs as roms_base-relative strings). When
    `roms_base` is None (e.g. launch-hook with no destination
    configured), falls back to absolute-path string match against the
    output's stored value.
    """
    rel_match: str | None = None
    if roms_base is not None:
        try:
            rel_match = str(rom_path.resolve().relative_to(roms_base.resolve()))
        except ValueError:
            rel_match = None
    abs_match = str(rom_path.resolve()) if rom_path.is_absolute() else None
    for rom in state.roms.values():
        for output in rom.outputs:
            if rel_match is not None and output.path == rel_match:
                return rom
            if abs_match is not None and output.path == abs_match:
                return rom
    return None


def _print_save_sync_preview(
    config: Config, api: RommApi, state: LibraryState, *, full: bool
) -> None:
    """Show what `ferry sync` (real run) WOULD do for each save backend.

    Builds read-only backends and calls `.plan(state)` — does ONE GET
    per backend (`/api/saves`); no device registration, no upload, no
    download, no state mutation. Falls back to install-selection-only
    messaging when an install isn't viable (no install detected, raw
    memcard mode, dolphin-tool missing, etc.).
    """
    if config.saves is None:
        return  # silent — feature is opt-in
    click.echo("")
    if not config.saves.enabled:
        click.echo("Save sync: disabled ([saves].enabled = false)")
        return

    device_id = state.device_id  # may be None — backend's plan() tolerates that

    _preview_retroarch(config, api, state, device_id=device_id, full=full)
    _preview_dolphin(config, api, state, device_id=device_id, full=full)
    _preview_cemu(config, api, state, device_id=device_id, full=full)


def _preview_retroarch(
    config: Config,
    api: RommApi,
    state: LibraryState,
    *,
    device_id: str | None,
    full: bool,
) -> None:
    assert config.saves is not None
    installs = discover_retroarch_installs()
    if not installs:
        click.echo("Save sync (RetroArch): would skip (no install detected)")
        return
    install = _resolve_retroarch_install_for_preview(config.saves, installs)
    if install is None:
        return  # message already printed
    backend = RetroArchSaveBackend(install=install, api=api, device_id=device_id or "", log=logger)
    click.echo(f"Save sync (RetroArch): targeting {install.source} @ {install.savefile_directory}")
    plan = backend.plan(state)
    _print_save_plan(plan, full=full)


def _resolve_retroarch_install_for_preview(
    saves_cfg: SavesConfig, installs: list[RetroArchInstall]
) -> RetroArchInstall | None:
    # Outer caller already printed the no-installs message before the
    # list got here, so we never see NO_INSTALLS at this site.
    resolution = resolve_install(
        installs,
        configured_source=saves_cfg.retroarch_install,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )
    if resolution.reason == ResolutionReason.EXPLICIT_MISMATCH:
        click.echo(
            f"Save sync (RetroArch): would skip ([saves].retroarch_install = "
            f"{saves_cfg.retroarch_install!r} but no install matches)"
        )
    elif resolution.reason == ResolutionReason.AMBIGUOUS:
        click.echo(
            "Save sync (RetroArch): would skip (multiple active installs — "
            "set [saves].retroarch_install)"
        )
    return resolution.install


def _preview_dolphin(
    config: Config,
    api: RommApi,
    state: LibraryState,
    *,
    device_id: str | None,
    full: bool,
) -> None:
    """Dry-run preview for both Dolphin-family backends (GC + Wii).

    Resolves the Dolphin install once, then runs each backend's
    preflight + plan independently. A skip on one backend doesn't
    block the other.
    """
    assert config.saves is not None
    if config.destination is None:
        return  # caller-side guard; defensive
    installs = discover_dolphin_installs()
    if not installs:
        for label in (GameCubeSaveBackend.backend_label, WiiSaveBackend.backend_label):
            click.echo(f"Save sync ({label}): would skip (no install detected)")
        return
    install = _resolve_dolphin_install_for_preview(config.saves, installs)
    if install is None:
        return  # message already printed
    tool = discover_dolphin_tool()
    if tool is None:
        for label in (GameCubeSaveBackend.backend_label, WiiSaveBackend.backend_label):
            click.echo(f"Save sync ({label}): would skip (dolphin-tool not found)")
        return
    cache = DiscHeaderCache(default_cache_path())
    _preview_gamecube(
        install=install,
        api=api,
        state=state,
        device_id=device_id,
        tool=tool,
        cache=cache,
        roms_base=config.destination.roms_base,
        full=full,
    )
    _preview_wii(
        install=install,
        api=api,
        state=state,
        device_id=device_id,
        tool=tool,
        cache=cache,
        roms_base=config.destination.roms_base,
        full=full,
    )


def _preview_gamecube(
    *,
    install: DolphinInstall,
    api: RommApi,
    state: LibraryState,
    device_id: str | None,
    tool,
    cache: DiscHeaderCache,
    roms_base: Path,
    full: bool,
) -> None:
    label = GameCubeSaveBackend.backend_label
    if install.slot_a_mode != "gci_folder":
        click.echo(
            f"Save sync ({label}): would skip — Slot A mode is "
            f"{install.slot_a_mode!r} (need GCI Folder)"
        )
        return
    backend = GameCubeSaveBackend(
        install=install,
        api=api,
        device_id=device_id or "",
        tool=tool,
        roms_base=roms_base,
        cache=cache,
        log=logger,
    )
    click.echo(f"Save sync ({label}): targeting {install.source} @ {install.saves_root}")
    plan = backend.plan(state)
    _print_save_plan(plan, full=full)


def _preview_wii(
    *,
    install: DolphinInstall,
    api: RommApi,
    state: LibraryState,
    device_id: str | None,
    tool,
    cache: DiscHeaderCache,
    roms_base: Path,
    full: bool,
) -> None:
    label = WiiSaveBackend.backend_label
    if install.wii_saves_root is None:
        # Wii layout not pinned for this install profile (today only
        # retrodeck has one). Stay quiet — not user-actionable.
        return
    backend = WiiSaveBackend(
        install=install,
        api=api,
        device_id=device_id or "",
        tool=tool,
        roms_base=roms_base,
        cache=cache,
        log=logger,
    )
    click.echo(f"Save sync ({label}): targeting {install.source} @ {install.wii_saves_root}")
    plan = backend.plan(state)
    _print_save_plan(plan, full=full)


def _resolve_dolphin_install_for_preview(
    saves_cfg: SavesConfig, installs: list[DolphinInstall]
) -> DolphinInstall | None:
    # Outer caller already printed the no-installs message; we never
    # see NO_INSTALLS at this site. The skip-message references both
    # Dolphin-family labels since the resolution governs both backends.
    resolution = resolve_install(
        installs,
        configured_source=saves_cfg.dolphin_install,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )
    if resolution.reason == ResolutionReason.EXPLICIT_MISMATCH:
        click.echo(
            f"Save sync (Dolphin): would skip ([saves].dolphin_install = "
            f"{saves_cfg.dolphin_install!r} but no install matches)"
        )
    elif resolution.reason == ResolutionReason.AMBIGUOUS:
        click.echo(
            "Save sync (Dolphin): would skip (multiple active installs — "
            "set [saves].dolphin_install)"
        )
    return resolution.install


def _preview_cemu(
    config: Config,
    api: RommApi,
    state: LibraryState,
    *,
    device_id: str | None,
    full: bool,
) -> None:
    """Dry-run preview for the Cemu (Wii U) backend."""
    assert config.saves is not None
    if config.destination is None:
        return  # caller-side guard; defensive
    label = CemuSaveBackend.backend_label
    installs = discover_cemu_installs()
    if not installs:
        click.echo(f"Save sync ({label}): would skip (no install detected)")
        return
    resolution = resolve_install(
        installs,
        configured_source=None,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )
    if resolution.install is None:
        if resolution.reason == ResolutionReason.AMBIGUOUS:
            click.echo(f"Save sync ({label}): would skip (multiple active installs)")
        return
    tool = discover_cemu_tool()
    if tool is None:
        click.echo(f"Save sync ({label}): would skip (cemu not found)")
        return
    backend = CemuSaveBackend(
        install=resolution.install,
        api=api,
        device_id=device_id or "",
        tool=tool,
        roms_base=config.destination.roms_base,
        cache=WiiUTitleCache(default_wiiu_cache_path()),
        log=logger,
    )
    click.echo(
        f"Save sync ({label}): targeting {resolution.install.source} "
        f"@ {resolution.install.wiiu_saves_root}"
    )
    plan = backend.plan(state)
    _print_save_plan(plan, full=full)


def _print_save_plan(plan: SavePlan, *, full: bool) -> None:
    """Render a `SavePlan` in the existing dry-run output style."""
    if plan.failed:
        for f in plan.failed:
            click.echo(f"  ✗ {f}")
        return

    summary: list[str] = []
    if plan.to_upload:
        summary.append(f"{len(plan.to_upload)} upload(s)")
    if plan.to_download:
        summary.append(f"{len(plan.to_download)} download(s)")
    if plan.conflicts_resolved:
        summary.append(f"{plan.conflicts_resolved} conflict(s) resolved")
    if plan.ambiguous:
        summary.append(f"{len(plan.ambiguous)} ambiguous (would skip)")
    if plan.skipped:
        summary.append(f"{plan.skipped} unchanged")
    if plan.drop_prior_count:
        summary.append(f"{plan.drop_prior_count} stale record(s) cleared")

    if not summary:
        click.echo("  (nothing to do)")
        return
    click.echo("  " + ", ".join(summary))

    cap = None if full else DEFAULT_PREVIEW
    _print_planned_actions("  Would upload", plan.to_upload, "↑", cap)
    _print_planned_actions("  Would download", plan.to_download, "↓", cap)
    if plan.ambiguous:
        click.echo("")
        click.echo("  Ambiguous (would skip — re-evaluated next sync):")
        shown = plan.ambiguous if cap is None else plan.ambiguous[:cap]
        for entry in shown:
            click.echo(f"    ? {entry}")
        if cap is not None and len(plan.ambiguous) > cap:
            click.echo(f"    ... and {len(plan.ambiguous) - cap} more")


def _print_planned_actions(
    title: str,
    items: tuple[PlannedSaveAction, ...],
    sigil: str,
    cap: int | None,
) -> None:
    if not items:
        return
    click.echo("")
    click.echo(f"{title} ({len(items)}):")
    shown = items if cap is None else items[:cap]
    for a in shown:
        click.echo(
            f"    {sigil} {a.rom_name} — {a.save_filename} "
            f"(emulator={a.emulator}, slot={a.slot}, {a.reason})"
        )
    if cap is not None and len(items) > cap:
        click.echo(f"    ... and {len(items) - cap} more (run with --full to list all)")


def _prepare_save_backends(
    config: Config,
    api: RommApi,
    state: LibraryState,
    state_path,
) -> tuple[list[SaveBackend], LibraryState]:
    """Build every configured save backend (RetroArch + Dolphin GC + Wii).

    All backends share the device_id — registration runs once. The two
    Dolphin-family backends additionally share the install resolution,
    `dolphin-tool` discovery, and disc-header cache. On blockers we
    surface a friendly one-liner per backend and continue (a Dolphin
    failure doesn't block RetroArch sync, and a Wii failure doesn't
    block GC sync).

    Returns `(backends, state)` — `state` may be a new LibraryState if
    we just registered this client and cached the device_id.
    """
    if config.saves is None or not config.saves.enabled:
        return [], state

    # Pre-flight: do we have any potential backend to prepare? If not,
    # skip device registration entirely (no point asking RomM for a
    # device id we won't use).
    ra_install = _select_retroarch_install(config.saves)
    dolphin_install = _select_dolphin_install(config.saves)
    cemu_install = _select_cemu_install()
    if ra_install is None and dolphin_install is None and cemu_install is None:
        return [], state

    try:
        device_id, state = get_or_register_device(api, state)
    except RommForbiddenError:
        click.echo("")
        click.echo(
            "save sync skipped: your RomM API token lacks write scopes.\n"
            "  ferry needs `devices.write` and `assets.write` to sync saves.\n"
            "  create a new token in RomM's web UI with those scopes, then\n"
            "  set FERRY_ROMM_API_KEY (or [romm].api_key) to the new value."
        )
        return [], state
    except RommApiError as e:
        click.echo("")
        click.echo(f"save sync skipped: device registration failed ({e}).")
        return [], state

    if state.device_id is not None:
        save_state(state, state_path)

    backends: list[SaveBackend] = []
    if ra_install is not None:
        backends.append(
            RetroArchSaveBackend(install=ra_install, api=api, device_id=device_id, log=logger)
        )

    if dolphin_install is not None:
        backends.extend(_build_dolphin_family_backends(dolphin_install, config, api, device_id))

    if cemu_install is not None:
        cemu_backend = _build_cemu_backend(cemu_install, config, api, device_id)
        if cemu_backend is not None:
            backends.append(cemu_backend)

    return backends, state


def _select_cemu_install() -> CemuInstall | None:
    """Auto-select the Cemu install ferry should sync, or None.

    No `[saves].cemu_install` override yet — v5 targets a single
    RetroDECK Cemu profile, so there's nothing to disambiguate. An
    override can be added later if multi-install setups become real.
    NO_INSTALLS is silent (a non-event — RA/Dolphin may still carry
    the user's saves); AMBIGUOUS surfaces a one-liner.
    """
    resolution = resolve_install(
        discover_cemu_installs(),
        configured_source=None,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )
    if resolution.install is None and resolution.reason == ResolutionReason.AMBIGUOUS:
        click.echo("")
        click.echo("save sync (Cemu) skipped: multiple installs with active saves.")
    return resolution.install


def _build_cemu_backend(
    install: CemuInstall,
    config: Config,
    api: RommApi,
    device_id: str,
) -> CemuSaveBackend | None:
    """Construct a CemuSaveBackend, or skip with a message if `cemu`
    isn't discoverable (ferry needs it to read Wii U title IDs)."""
    if config.destination is None:
        return None  # caller guard checked this; defensive
    tool = discover_cemu_tool()
    if tool is None:
        click.echo("")
        click.echo(
            f"save sync ({CemuSaveBackend.backend_label}) skipped: cemu not found.\n"
            "  Install Cemu (RetroDECK) — ferry needs it to read Wii U title IDs."
        )
        return None
    return CemuSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=tool,
        roms_base=config.destination.roms_base,
        cache=WiiUTitleCache(default_wiiu_cache_path()),
        log=logger,
    )


def _build_dolphin_family_backends(
    install: DolphinInstall,
    config: Config,
    api: RommApi,
    device_id: str,
) -> list[SaveBackend]:
    """Build GC + Wii backends from one Dolphin install.

    Shared preflight (`dolphin-tool` discovery + disc-header cache)
    runs once; each backend then applies its own filters. Either or
    both may be skipped with a message.
    """
    if config.destination is None:
        return []  # caller-side guard; defensive
    tool = discover_dolphin_tool()
    if tool is None:
        click.echo("")
        click.echo(
            "save sync (Dolphin) skipped: dolphin-tool not found.\n"
            "  Install Dolphin (native, RetroDECK, or EmuDeck Flatpak) — ferry "
            "needs dolphin-tool to read GameCube/Wii disc headers."
        )
        return []
    cache = DiscHeaderCache(default_cache_path())
    backends: list[SaveBackend] = []
    gc_backend = _build_gamecube_backend(install, config, api, device_id, tool, cache)
    if gc_backend is not None:
        backends.append(gc_backend)
    wii_backend = _build_wii_backend(install, config, api, device_id, tool, cache)
    if wii_backend is not None:
        backends.append(wii_backend)
    return backends


def _select_retroarch_install(saves_cfg: SavesConfig) -> RetroArchInstall | None:
    """Apply the user's `retroarch_install` override or fall back to auto-select."""
    resolution = resolve_install(
        discover_retroarch_installs(),
        configured_source=saves_cfg.retroarch_install,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )
    if resolution.install is None:
        click.echo("")
        click.echo(_retroarch_skip_message(resolution, saves_cfg))
    return resolution.install


def _retroarch_skip_message(
    resolution: InstallResolution[RetroArchInstall], saves_cfg: SavesConfig
) -> str:
    match resolution.reason:
        case ResolutionReason.NO_INSTALLS:
            return "save sync (RetroArch) skipped: no install detected."
        case ResolutionReason.EXPLICIT_MISMATCH:
            return (
                f"save sync (RetroArch) skipped: [saves].retroarch_install = "
                f"{saves_cfg.retroarch_install!r} but no install matched."
            )
        case ResolutionReason.AMBIGUOUS:
            return (
                "save sync (RetroArch) skipped: multiple installs with active saves "
                "(set [saves].retroarch_install to disambiguate)."
            )
        case _:
            return "save sync (RetroArch) skipped."  # defensive


def _select_dolphin_install(saves_cfg: SavesConfig) -> DolphinInstall | None:
    """Apply the user's `dolphin_install` override or fall back to auto-select.

    Returns the install regardless of GC- or Wii-specific filters
    (Slot A mode for GC; `wii_saves_root is not None` for Wii) —
    those land at backend-builder time so each backend can decide
    independently. NO_INSTALLS is silent here (RA may still be
    configured); other failure reasons surface a `skipped:` line
    that covers the whole Dolphin family.
    """
    resolution = resolve_install(
        discover_dolphin_installs(),
        configured_source=saves_cfg.dolphin_install,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )
    if resolution.install is None:
        message = _dolphin_skip_message(resolution, saves_cfg)
        if message is not None:
            click.echo("")
            click.echo(message)
    return resolution.install


def _dolphin_skip_message(
    resolution: InstallResolution[DolphinInstall], saves_cfg: SavesConfig
) -> str | None:
    """None means stay silent — the no-installs case is a non-event for
    Dolphin since RA may still carry the user's saves."""
    match resolution.reason:
        case ResolutionReason.NO_INSTALLS:
            return None
        case ResolutionReason.EXPLICIT_MISMATCH:
            return (
                f"save sync (Dolphin) skipped: [saves].dolphin_install = "
                f"{saves_cfg.dolphin_install!r} but no install matched."
            )
        case ResolutionReason.AMBIGUOUS:
            return (
                "save sync (Dolphin) skipped: multiple installs with active saves "
                "(set [saves].dolphin_install to disambiguate)."
            )
        case _:
            return None


def _build_gamecube_backend(
    install: DolphinInstall,
    config: Config,
    api: RommApi,
    device_id: str,
    tool,
    cache: DiscHeaderCache,
) -> GameCubeSaveBackend | None:
    """Construct a GameCubeSaveBackend, or skip with a message if Slot A
    isn't in GCI Folder mode (raw `.raw` memcards aren't supported).

    `tool` and `cache` are shared with the Wii backend — both read the
    same disc headers so caching across backends is the right call.
    """
    if config.destination is None:
        return None  # caller guard checked this; defensive
    if install.slot_a_mode != "gci_folder":
        click.echo("")
        click.echo(
            f"save sync ({GameCubeSaveBackend.backend_label}) skipped: Slot A mode is "
            f"{install.slot_a_mode!r} on {install.source}; ferry only syncs "
            "GCI Folder saves. Switch in Dolphin Config > GameCube > Slot A."
        )
        return None
    return GameCubeSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=tool,
        roms_base=config.destination.roms_base,
        cache=cache,
        log=logger,
    )


def _build_wii_backend(
    install: DolphinInstall,
    config: Config,
    api: RommApi,
    device_id: str,
    tool,
    cache: DiscHeaderCache,
) -> WiiSaveBackend | None:
    """Construct a WiiSaveBackend, or skip silently when this install
    profile doesn't have a verified Wii layout.

    `wii_saves_root is None` means the install profile (emudeck-flatpak,
    native — for v3.6) hasn't been mapped yet. Stay quiet rather than
    nag: it's not user-actionable, and the GC backend may still
    proceed on the same install.
    """
    if config.destination is None:
        return None  # caller guard checked this; defensive
    if install.wii_saves_root is None:
        return None
    return WiiSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=tool,
        roms_base=config.destination.roms_base,
        cache=cache,
        log=logger,
    )


def _delete_for_rom_all(backends: list[SaveBackend], rom, trash_dir: Path) -> None:
    """Fire each backend's delete_for_rom hook for a trashed ROM.

    Each backend writes its own files into `<trash_dir>/saves/...` —
    paths don't collide between backends because their save trees are
    disjoint.
    """
    for backend in backends:
        try:
            backend.delete_for_rom(rom, trash_dir)
        except Exception:
            logger.exception(
                "%s.delete_for_rom failed for rom %d", type(backend).__name__, rom.rom_id
            )


def _run_all_save_syncs(
    backends: list[SaveBackend],
    state: LibraryState,
    state_path,
    *,
    rom: RomState | None = None,
) -> None:
    """Run every backend's sync sequentially and print per-backend summaries.

    `rom` narrows each backend to `sync_for_rom(rom, state)` — used by the
    `--rom` launch-wrapper mode. Default (None) runs full per-backend sync.
    """
    if not backends:
        return
    for backend in backends:
        _run_save_sync(backend, state, state_path, rom=rom)


def _run_save_sync(
    backend: SaveBackend,
    state: LibraryState,
    state_path,
    *,
    rom: RomState | None = None,
) -> None:
    """Run save sync and print a summary block in the existing layout."""
    label = backend.backend_label
    click.echo("")
    if rom is not None:
        click.echo(f"Syncing {label} saves for {rom.name}…")
        result = backend.sync_for_rom(rom, state)
    else:
        click.echo(f"Syncing {label} saves…")
        result = backend.sync(state)
    if result.updated_roms:
        for rom_id, updated_rom in result.updated_roms.items():
            state.roms[rom_id] = updated_rom
        save_state(state, state_path)
    _print_save_sync_summary(result, label=label)


def _print_save_sync_summary(result: SaveSyncResult, *, label: str = "Save") -> None:
    click.echo("")
    click.echo(f"{label} save sync:")
    click.echo(f"  Uploaded:   {result.uploaded}")
    click.echo(f"  Downloaded: {result.downloaded}")
    click.echo(f"  Skipped:    {result.skipped}")
    if result.conflicts_resolved:
        click.echo(f"  Conflicts resolved: {result.conflicts_resolved}")
    if result.upload_conflicts:
        click.echo(
            f"  Upload conflicts: {result.upload_conflicts} "
            f"(server has newer; next sync will resolve)"
        )
    if result.ambiguous:
        click.echo("")
        click.echo("Ambiguous (within tolerance — skipped, will re-evaluate next sync):")
        for line in result.ambiguous[:DEFAULT_PREVIEW]:
            click.echo(f"  · {line}")
    if result.failed:
        click.echo("")
        click.echo("Failed:")
        for line in result.failed[:DEFAULT_PREVIEW]:
            click.echo(f"  ✗ {line}")
    if result.warnings:
        # Walker warnings (unmatched filenames) are routine; don't shout.
        unmatched = sum("could not match" in w for w in result.warnings)
        if unmatched:
            click.echo(
                f"  ({unmatched} local save file(s) didn't match any synced ROM — "
                f"may belong to ROMs not synced via ferry)"
            )


def _resolve_collections(api: RommApi, names: tuple[str, ...]) -> tuple[list[int], list[str]]:
    """Resolve manual-collection names → ids.

    Returns (ids, errors) — errors is a list of human-readable error blocks
    (missing names, ambiguous names). Caller combines errors from multiple
    resolvers so the user sees all problems at once.
    """
    if not names:
        return [], []
    available = api.list_collections()
    by_name: dict[str, list[dict[str, Any]]] = {}
    for c in available:
        by_name.setdefault(c.get("name", ""), []).append(c)

    resolved: list[int] = []
    missing: list[str] = []
    ambiguous: list[tuple[str, list[Any]]] = []
    for name in names:
        matches = by_name.get(name, [])
        if not matches:
            missing.append(name)
            continue
        if len(matches) > 1:
            ambiguous.append((name, [m.get("id") for m in matches]))
            continue
        resolved.append(int(matches[0]["id"]))

    errors: list[str] = []
    for name, ids in ambiguous:
        errors.append(
            f"multiple collections named {name!r} found (ids: {ids}). "
            f"matching by id is not yet supported; rename one in RomM."
        )
    if missing:
        all_names = sorted(by_name)
        errors.append(
            f"collection(s) not found in RomM: {missing}\n"
            f"available: {', '.join(all_names) if all_names else '(none)'}"
        )
    return resolved, errors


def _resolve_platforms(api: RommApi, slugs: tuple[str, ...]) -> tuple[list[int], list[str]]:
    """Resolve RomM platform slugs → ids.

    Returns (ids, errors) so callers can present platform misses alongside
    other resolution errors.
    """
    if not slugs:
        return [], []
    available = api.list_platforms()
    by_slug = {p.get("slug"): p for p in available}
    resolved: list[int] = []
    missing: list[str] = []
    for slug in slugs:
        match = by_slug.get(slug)
        if match is None:
            missing.append(slug)
            continue
        resolved.append(int(match["id"]))

    errors: list[str] = []
    if missing:
        all_slugs = sorted(s for s in by_slug if s)
        errors.append(
            f"platform slug(s) not found in RomM: {missing}\n"
            f"available: {', '.join(all_slugs) if all_slugs else '(none)'}"
        )
    return resolved, errors


def _fetch_roms(
    api: RommApi,
    *,
    collection_ids: list[int],
    platform_ids: list[int],
    primary_only: bool,
) -> list[dict[str, Any]]:
    """Union-by-rom_id across all configured sources. Insertion order preserved."""
    by_id: dict[int, dict[str, Any]] = {}

    for cid in collection_ids:
        for rom in api.list_roms(collection_id=cid, primary_only=primary_only):
            rom_id = rom.get("id")
            if isinstance(rom_id, int):
                by_id.setdefault(rom_id, rom)

    if platform_ids:
        for rom in api.list_roms(platform_ids=platform_ids, primary_only=primary_only):
            rom_id = rom.get("id")
            if isinstance(rom_id, int):
                by_id.setdefault(rom_id, rom)

    return list(by_id.values())


def _print_plan_summary(plan: SyncPlan) -> None:
    click.echo("")
    click.echo("Sync plan:")
    click.echo(f"  Add:        {len(plan.to_add)}")
    click.echo(f"  Update:     {len(plan.to_update)}")
    click.echo(f"  Delete:     {len(plan.to_delete)}")
    click.echo(f"  Unchanged:  {plan.unchanged_count}")


def _print_plan(plan: SyncPlan, *, full: bool, config: Config) -> None:
    _print_plan_summary(plan)

    cap = None if full else DEFAULT_PREVIEW
    _print_section("To add", plan.to_add, "+", cap, config)
    _print_section("To update", plan.to_update, "↻", cap, config)

    if plan.to_delete:
        delete_active = bool(config.sync and config.sync.delete_on_remove)
        title = (
            "To delete"
            if delete_active
            else "No longer in collection (would trash if `[sync].delete_on_remove = true`)"
        )
        _print_section(title, plan.to_delete, "-", cap, config)

    click.echo("")
    if plan.is_empty:
        click.echo("Nothing to do — local state matches RomM.")
    else:
        click.echo("(dry run — no files modified)")


def _print_section(
    title: str,
    items: list[AddAction] | list[UpdateAction] | list[DeleteAction],
    sigil: str,
    cap: int | None,
    config: Config,
) -> None:
    if not items:
        return
    click.echo("")
    click.echo(f"{title} ({len(items)}):")
    shown = items if cap is None else items[:cap]
    for a in shown:
        line = f"  {sigil} {a.name} ({a.platform_slug}, rom_id={a.rom_id})"
        details = _format_action_destination(a, config)
        if details:
            line += f" → {details}"
        click.echo(line)
    if cap is not None and len(items) > cap:
        click.echo(f"  ... and {len(items) - cap} more (run with --full to list all)")


def _format_action_destination(
    action: AddAction | UpdateAction | DeleteAction,
    config: Config,
) -> str:
    """Render the on-disk path + pipeline summary for a planned action."""
    if config.destination is None:  # guarded earlier; defensive
        return ""
    roms_base = config.destination.roms_base

    if isinstance(action, DeleteAction):
        # Show the existing primary output so the user knows what would go.
        primary = action.previous.outputs[action.previous.primary_output_index]
        return str(roms_base / primary.path)

    # Add / Update: source filename → resolved platform dir
    local_filename = resolve_local_filename(action.rom_data, logger=logger)
    platform_dir = roms_base / resolve_platform_dir(action.platform_slug)
    pipeline = config.transforms.for_platform(action.platform_slug)
    pipeline_str = f" [{' → '.join(pipeline)}]" if pipeline else ""
    return f"{platform_dir / local_filename}{pipeline_str}"


def _print_execution_summary(plan: SyncPlan, result: ExecutionResult) -> None:
    click.echo("")
    click.echo("Sync complete:")
    click.echo(f"  Synced:  {len(result.succeeded)}")
    click.echo(f"  Deleted: {len(result.deleted)}")
    click.echo(f"  Failed:  {len(result.failed)}")
    if result.failed:
        click.echo("")
        click.echo("Failures:")
        for f in result.failed:
            click.echo(f"  ✗ {f.name} ({f.platform_slug}, rom_id={f.rom_id}): {f.error}")
        click.echo("")
        click.echo("Re-running sync will retry failed actions.")
    if result.deleted:
        click.echo("")
        click.echo("Trashed ROMs (recoverable until retention expires):")
        for d in result.deleted[:DEFAULT_PREVIEW]:
            click.echo(f"  - {d.name} ({d.platform_slug}, rom_id={d.rom_id}) → {d.trash_dir}")
        if len(result.deleted) > DEFAULT_PREVIEW:
            click.echo(f"  ... and {len(result.deleted) - DEFAULT_PREVIEW} more")
