import logging
from pathlib import Path
from typing import Any

import click

from ferry.adapters.dolphin_paths import (
    DolphinInstall,
    discover_dolphin_installs,
)
from ferry.adapters.dolphin_paths import (
    select_active_install as select_active_dolphin,
)
from ferry.adapters.dolphin_tool import (
    DiscHeaderCache,
    default_cache_path,
    discover_dolphin_tool,
)
from ferry.adapters.retroarch_paths import (
    RetroArchInstall,
    discover_retroarch_installs,
    select_active_install,
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
    ensure_sidecars,
    load_state,
    recover_state_from_sidecars,
    save_state,
)
from ferry.config import ConfigError, SavesConfig, SyncConfig, load_config
from ferry.config.schema import Config
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.state import LibraryState
from ferry.domain.sync_plan import (
    AddAction,
    DeleteAction,
    SyncPlan,
    UpdateAction,
    compute_plan,
)
from ferry.services.dolphin_save_backend import DolphinSaveBackend
from ferry.services.save_backend import (
    RetroArchSaveBackend,
    SaveSyncResult,
    get_or_register_device,
)
from ferry.services.sync_executor import (
    ExecutionResult,
    default_scratch_root,
    execute_plan,
)
from ferry.services.sync_lock import LockHeld, acquire_sync_lock, default_lock_path
from ferry.services.trash import default_trash_root, purge_expired

logger = logging.getLogger(__name__)

# Local Protocol-like alias — both backends expose `.sync()` and
# `.delete_for_rom()`. Until checkpoint 6 extracts a real Protocol, the
# union keeps the type information honest.
_SaveBackend = RetroArchSaveBackend | DolphinSaveBackend

# How many entries per section to print before truncating with "... and N more".
_DEFAULT_PREVIEW = 20


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
@click.pass_context
def sync(ctx: click.Context, dry_run: bool, full: bool) -> None:
    """Sync the configured collection from RomM to the destination."""
    try:
        loaded = load_config(ctx.obj.get("config_path"))
    except ConfigError as e:
        raise click.ClickException(str(e)) from e

    config = loaded.config
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

    sync_cfg: SyncConfig = config.sync
    try:
        with acquire_sync_lock(default_lock_path()):
            _run_sync(config, sync_cfg, dry_run=dry_run, full=full)
    except LockHeld as e:
        raise click.ClickException(
            f"another ferry sync is already running (pid {e.pid}, lock at "
            f"{e.lock_path}).\nWait for it to finish, or check `ps -p {e.pid}` "
            "if you suspect it's stuck."
        ) from e


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
            if not state.roms and config.destination is not None:
                recovered = recover_state_from_sidecars([config.destination.roms_base])
                if recovered.roms:
                    click.echo(f"recovered {len(recovered.roms)} ROM(s) from on-disk sidecars")
                    state = recovered
                    save_state(state, state_path)
            if config.destination is not None:
                regenerated = ensure_sidecars(state, config.destination)
                if regenerated:
                    click.echo(f"regenerated {regenerated} missing sidecar(s) from state")
            trash_root = default_trash_root()
            plan = compute_plan(
                current_roms=current_roms,
                state=state,
                destination=config.destination,
            )

            if dry_run:
                _print_plan(plan, full=full, config=config)
                _print_save_sync_preview(config)
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


def _print_save_sync_preview(config: Config) -> None:
    """Show what `ferry sync` (real run) WOULD do for each save backend.

    Resolution-only — no HTTP calls, no state mutation. Mirrors the same
    install-selection logic as the real run so users can verify their
    `[saves].*_install` settings before committing to a sync.
    """
    if config.saves is None:
        return  # silent — feature is opt-in
    click.echo("")
    if not config.saves.enabled:
        click.echo("Save sync: disabled ([saves].enabled = false)")
        return
    _preview_retroarch(config)
    _preview_dolphin(config)


def _preview_retroarch(config: Config) -> None:
    assert config.saves is not None
    installs = discover_retroarch_installs()
    if not installs:
        click.echo("Save sync (RetroArch): would skip (no install detected)")
        return
    if config.saves.retroarch_install is not None:
        match = next(
            (i for i in installs if i.source == config.saves.retroarch_install),
            None,
        )
        if match is None:
            click.echo(
                f"Save sync (RetroArch): would skip ([saves].retroarch_install = "
                f"{config.saves.retroarch_install!r} but no install matches)"
            )
            return
        click.echo(
            f"Save sync (RetroArch): would target {match.source} @ {match.savefile_directory} "
            f"(selected via [saves].retroarch_install)"
        )
        return
    active = select_active_install(installs)
    if active is None:
        click.echo(
            "Save sync (RetroArch): would skip (multiple active installs — "
            "set [saves].retroarch_install)"
        )
        return
    why = "has active saves" if active.has_saves else "priority order"
    click.echo(
        f"Save sync (RetroArch): would target {active.source} @ {active.savefile_directory} "
        f"(selected because {why})"
    )


def _preview_dolphin(config: Config) -> None:
    assert config.saves is not None
    installs = discover_dolphin_installs()
    if not installs:
        click.echo("Save sync (Dolphin): would skip (no install detected)")
        return
    install = _select_dolphin_install_for_preview(config.saves, installs)
    if install is None:
        return  # message already printed
    if install.slot_a_mode != "gci_folder":
        click.echo(
            f"Save sync (Dolphin): would skip — Slot A mode is "
            f"{install.slot_a_mode!r} (need GCI Folder)"
        )
        return
    if discover_dolphin_tool() is None:
        click.echo("Save sync (Dolphin): would skip (dolphin-tool not found)")
        return
    click.echo(f"Save sync (Dolphin): would target {install.source} @ {install.saves_root}")


def _select_dolphin_install_for_preview(
    saves_cfg: SavesConfig, installs: list[DolphinInstall]
) -> DolphinInstall | None:
    if saves_cfg.dolphin_install is not None:
        match = next((i for i in installs if i.source == saves_cfg.dolphin_install), None)
        if match is None:
            click.echo(
                f"Save sync (Dolphin): would skip ([saves].dolphin_install = "
                f"{saves_cfg.dolphin_install!r} but no install matches)"
            )
        return match
    active = select_active_dolphin(installs)
    if active is None:
        click.echo(
            "Save sync (Dolphin): would skip (multiple active installs — "
            "set [saves].dolphin_install)"
        )
    return active


def _prepare_save_backends(
    config: Config,
    api: RommApi,
    state: LibraryState,
    state_path,
) -> tuple[list[_SaveBackend], LibraryState]:
    """Build every configured save backend (RetroArch + Dolphin).

    Both backends share the device_id — registration runs once. On
    blockers we surface a friendly one-liner per backend and continue
    (a Dolphin failure doesn't block RetroArch sync, and vice versa).

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
    if ra_install is None and dolphin_install is None:
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

    backends: list[_SaveBackend] = []
    if ra_install is not None:
        backends.append(
            RetroArchSaveBackend(install=ra_install, api=api, device_id=device_id, log=logger)
        )

    if dolphin_install is not None:
        dolphin_backend = _build_dolphin_backend(dolphin_install, config, api, device_id)
        if dolphin_backend is not None:
            backends.append(dolphin_backend)

    return backends, state


def _select_retroarch_install(saves_cfg: SavesConfig) -> RetroArchInstall | None:
    """Apply the user's `retroarch_install` override or fall back to auto-select."""
    installs = discover_retroarch_installs()
    if not installs:
        click.echo("")
        click.echo("save sync (RetroArch) skipped: no install detected.")
        return None

    if saves_cfg.retroarch_install is not None:
        for install in installs:
            if install.source == saves_cfg.retroarch_install:
                return install
        click.echo("")
        click.echo(
            f"save sync (RetroArch) skipped: [saves].retroarch_install = "
            f"{saves_cfg.retroarch_install!r} but no install matched."
        )
        return None

    active = select_active_install(installs)
    if active is None:
        click.echo("")
        click.echo(
            "save sync (RetroArch) skipped: multiple installs with active saves "
            "(set [saves].retroarch_install to disambiguate)."
        )
    return active


def _select_dolphin_install(saves_cfg: SavesConfig) -> DolphinInstall | None:
    """Apply the user's `dolphin_install` override or fall back to auto-select.

    Skips the install when Slot A isn't in GCI Folder mode — v3 only
    syncs GCI Folder saves; raw `.raw` memcards aren't supported.
    """
    installs = discover_dolphin_installs()
    if not installs:
        return None  # silent — RA may still be configured

    selected: DolphinInstall | None = None
    if saves_cfg.dolphin_install is not None:
        selected = next((i for i in installs if i.source == saves_cfg.dolphin_install), None)
        if selected is None:
            click.echo("")
            click.echo(
                f"save sync (Dolphin) skipped: [saves].dolphin_install = "
                f"{saves_cfg.dolphin_install!r} but no install matched."
            )
            return None
    else:
        selected = select_active_dolphin(installs)
        if selected is None:
            click.echo("")
            click.echo(
                "save sync (Dolphin) skipped: multiple installs with active saves "
                "(set [saves].dolphin_install to disambiguate)."
            )
            return None

    if selected.slot_a_mode != "gci_folder":
        click.echo("")
        click.echo(
            f"save sync (Dolphin) skipped: Slot A mode is "
            f"{selected.slot_a_mode!r} on {selected.source}; ferry only syncs "
            "GCI Folder saves. Switch in Dolphin Config > GameCube > Slot A."
        )
        return None

    return selected


def _build_dolphin_backend(
    install: DolphinInstall,
    config: Config,
    api: RommApi,
    device_id: str,
) -> DolphinSaveBackend | None:
    """Construct a DolphinSaveBackend, or skip with a message if dolphin-tool
    or roms_base aren't available."""
    if config.destination is None:
        return None  # caller guard checked this; defensive
    tool = discover_dolphin_tool()
    if tool is None:
        click.echo("")
        click.echo(
            "save sync (Dolphin) skipped: dolphin-tool not found.\n"
            "  Install Dolphin (native, RetroDECK, or EmuDeck Flatpak) — ferry "
            "needs dolphin-tool to read GameCube disc headers."
        )
        return None
    cache = DiscHeaderCache(default_cache_path())
    return DolphinSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=tool,
        roms_base=config.destination.roms_base,
        cache=cache,
        log=logger,
    )


def _delete_for_rom_all(backends: list[_SaveBackend], rom, trash_dir: Path) -> None:
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
    backends: list[_SaveBackend],
    state: LibraryState,
    state_path,
) -> None:
    """Run every backend's sync sequentially and print per-backend summaries."""
    if not backends:
        return
    for backend in backends:
        _run_save_sync(backend, state, state_path)


def _run_save_sync(
    backend: _SaveBackend,
    state: LibraryState,
    state_path,
) -> None:
    """Run save sync and print a summary block in the existing layout."""
    label = "RetroArch" if isinstance(backend, RetroArchSaveBackend) else "Dolphin"
    click.echo("")
    click.echo(f"Syncing {label} saves…")
    result = backend.sync(state)
    if result.updated_roms:
        for rom_id, rom in result.updated_roms.items():
            state.roms[rom_id] = rom
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
    if result.ambiguous:
        click.echo("")
        click.echo("Ambiguous (within tolerance — skipped, will re-evaluate next sync):")
        for line in result.ambiguous[:_DEFAULT_PREVIEW]:
            click.echo(f"  · {line}")
    if result.failed:
        click.echo("")
        click.echo("Failed:")
        for line in result.failed[:_DEFAULT_PREVIEW]:
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

    cap = None if full else _DEFAULT_PREVIEW
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
    fs_name = action.rom_data.get("fs_name") or f"rom-{action.rom_id}"
    platform_dir = roms_base / resolve_platform_dir(action.platform_slug)
    pipeline = config.transforms.for_platform(action.platform_slug)
    pipeline_str = f" [{' → '.join(pipeline)}]" if pipeline else ""
    return f"{platform_dir / fs_name}{pipeline_str}"


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
        for d in result.deleted[:_DEFAULT_PREVIEW]:
            click.echo(f"  - {d.name} ({d.platform_slug}, rom_id={d.rom_id}) → {d.trash_dir}")
        if len(result.deleted) > _DEFAULT_PREVIEW:
            click.echo(f"  ... and {len(result.deleted) - _DEFAULT_PREVIEW} more")
