"""`ferry status` — read-only introspection of state vs disk.

No HTTP calls. Tells the user what ferry currently *knows* (state.json),
what's actually on disk, and where the two diverge. The high-frequency
"did my last sync work?" answer.
"""

from __future__ import annotations

import contextlib
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from ferry import __version__
from ferry.adapters.dolphin_paths import (
    DolphinInstall,
    discover_dolphin_installs,
)
from ferry.adapters.esde_paths import ESDEInstall, discover_esde_installs
from ferry.adapters.retroarch_core_info import CoreInfoIndex
from ferry.adapters.retroarch_paths import (
    RetroArchInstall,
    discover_retroarch_installs,
)
from ferry.adapters.retroarch_saves import list_local_saves as list_ra_local_saves
from ferry.adapters.state_store import default_state_path, load_state
from ferry.cli._utils import format_bytes, mask_token, path_status
from ferry.config import ConfigError, load_config
from ferry.config.schema import Config
from ferry.domain.install_selection import ResolutionReason, resolve_install
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.state import LibraryState, RomState
from ferry.services.launch_hooks import (
    DriftKind,
    HookStatus,
    classify_drift,
    default_snapshot_path,
    detect_drift,
    extract_managed_block,
    read_snapshot,
)
from ferry.services.trash import default_trash_root


@click.command()
@click.option(
    "--show-all",
    is_flag=True,
    help=(
        "Include backup-noise entries in the orphan-saves listing "
        "(RetroDECK's `_YYYYMMDD_HHMMSS` SRM backups)."
    ),
)
@click.pass_context
def status(ctx: click.Context, show_all: bool) -> None:
    """Show what ferry knows: configured sources, on-disk state, and reconcile."""
    click.echo(f"ferry {__version__}")

    try:
        loaded = load_config(ctx.obj.get("config_path"))
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    config = loaded.config

    state_path = default_state_path()
    state = load_state(state_path)
    trash_root = default_trash_root()

    click.echo(f"config:        {loaded.config_path}")
    click.echo(f"state:         {state_path} ({len(state.roms)} ROM(s) tracked)")
    _print_trash_summary(trash_root)

    click.echo("")
    click.echo("[romm]")
    click.echo(f"  url:         {config.romm.url}")
    click.echo(f"  api_key:     {mask_token(config.romm.api_key)}")

    click.echo("")
    click.echo("[sync]")
    if config.sync is None:
        click.echo("  (not configured — `ferry sync` will fail)")
    else:
        cols = ", ".join(config.sync.collections) if config.sync.collections else "(none)"
        plats = ", ".join(config.sync.platforms) if config.sync.platforms else "(none)"
        click.echo(f"  collections: {cols}")
        click.echo(f"  platforms:   {plats}")
        click.echo(f"  delete_on_remove: {config.sync.delete_on_remove}")

    click.echo("")
    click.echo("[destination]")
    if config.destination is None:
        click.echo("  (not configured — `ferry detect` for help)")
    else:
        d = config.destination
        click.echo(f"  preset:      {d.preset or '(custom)'}")
        click.echo(f"  roms_base:   {d.roms_base} {path_status(d.roms_base)}")
        if d.bios_base is None:
            click.echo("  bios_base:   (per-emulator)")
        else:
            click.echo(f"  bios_base:   {d.bios_base} {path_status(d.bios_base)}")

    click.echo("")
    click.echo("[saves]")
    _print_retroarch_status(config)
    _print_dolphin_status(config)

    click.echo("")
    click.echo("[launch_hooks]")
    _print_esde_status()

    click.echo("")
    click.echo("[orphan saves]")
    _print_orphan_saves(config, state, show_all=show_all)

    if state.roms and config.destination is not None:
        _print_reconcile(state, config)
    elif not state.roms:
        click.echo("")
        click.echo("(state is empty — first sync will populate)")


def _print_retroarch_status(config: Config) -> None:
    """One-line install summary, plus listing all candidates when ambiguous.

    When the user has `[saves].retroarch_install` configured AND it matches
    a discovered install, the configured choice wins over auto-selection —
    even if multiple installs would otherwise be ambiguous. That mirrors
    what `ferry sync` actually does at runtime. EXPLICIT_MISMATCH (the
    configured name doesn't match any discovered install) falls through
    to auto-select after a warning, so the user sees what's actually there.
    """
    installs = discover_retroarch_installs()
    configured = config.saves.retroarch_install if config.saves else None
    resolution = resolve_install(
        installs,
        configured_source=configured,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )

    if resolution.reason == ResolutionReason.NO_INSTALLS:
        click.echo("  retroarch:   (not detected)")
        return
    if resolution.reason == ResolutionReason.EXPLICIT_MATCH:
        _print_install_line(resolution.install, indent="  retroarch:   ")
        click.echo(f"    (selected via [saves].retroarch_install = {configured!r})")
        return
    if resolution.reason == ResolutionReason.EXPLICIT_MISMATCH:
        click.echo(
            f"  warning: [saves].retroarch_install = {configured!r} "
            "but no discovered install matches; auto-selecting:"
        )
        resolution = resolve_install(
            installs,
            configured_source=None,
            source_of=lambda i: i.source,
            has_active=lambda i: i.has_saves,
        )
    if resolution.reason == ResolutionReason.AMBIGUOUS:
        click.echo("  retroarch:   AMBIGUOUS — multiple active installs detected:")
        for install in installs:
            _print_install_line(install, indent="    ")
        click.echo("    (set [saves.retroarch_install] in config to pick one)")
        return

    active = resolution.install
    assert active is not None
    _print_install_line(active, indent="  retroarch:   ")
    if len(installs) > 1:
        click.echo(
            f"    (out of {len(installs)} detected; selected because "
            f"{'has active saves' if active.has_saves else 'priority order'})"
        )


def _print_install_line(install: RetroArchInstall, *, indent: str) -> None:
    layout = _layout_label(install)
    click.echo(f"{indent}{install.source} @ {install.savefile_directory} ({layout})")


def _layout_label(install: RetroArchInstall) -> str:
    """Compact human-readable layout summary."""
    flags = []
    if install.sort_savefiles_by_content_enable:
        flags.append("by-content")
    if install.sort_savefiles_enable:
        flags.append("by-core")
    return ", ".join(flags) if flags else "flat"


def _print_dolphin_status(config: Config) -> None:
    """One-line Dolphin install summary; warns when SlotA isn't GCI Folder.

    v3 only syncs Dolphin saves when SlotA is in GCI Folder mode (the
    modern Dolphin default). Raw `.raw` memcards aren't supported in
    v3 — surface clearly so the user knows what to do.
    """
    installs = discover_dolphin_installs()
    configured = config.saves.dolphin_install if config.saves else None
    resolution = resolve_install(
        installs,
        configured_source=configured,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    )

    if resolution.reason == ResolutionReason.NO_INSTALLS:
        click.echo("  dolphin:     (not detected)")
        return
    if resolution.reason == ResolutionReason.EXPLICIT_MATCH:
        _print_dolphin_install_line(resolution.install, indent="  dolphin:     ")
        click.echo(f"    (selected via [saves].dolphin_install = {configured!r})")
        return
    if resolution.reason == ResolutionReason.EXPLICIT_MISMATCH:
        click.echo(
            f"  warning: [saves].dolphin_install = {configured!r} "
            "but no discovered install matches; auto-selecting:"
        )
        resolution = resolve_install(
            installs,
            configured_source=None,
            source_of=lambda i: i.source,
            has_active=lambda i: i.has_saves,
        )
    if resolution.reason == ResolutionReason.AMBIGUOUS:
        click.echo("  dolphin:     AMBIGUOUS — multiple active installs detected:")
        for install in installs:
            _print_dolphin_install_line(install, indent="    ")
        click.echo("    (set [saves.dolphin_install] in config to pick one)")
        return

    active = resolution.install
    assert active is not None
    _print_dolphin_install_line(active, indent="  dolphin:     ")
    if len(installs) > 1:
        click.echo(
            f"    (out of {len(installs)} detected; selected because "
            f"{'has active saves' if active.has_saves else 'priority order'})"
        )


def _print_dolphin_install_line(install: DolphinInstall, *, indent: str) -> None:
    mode = install.slot_a_mode
    suffix = ""
    if mode == "raw_memcard":
        suffix = " — RAW MEMCARD MODE (v3 needs GCI Folder; switch in Dolphin Config > GameCube)"
    elif mode == "none":
        suffix = " — Slot A disabled"
    elif mode == "other":
        suffix = " — Slot A is an unsupported device type"
    click.echo(f"{indent}{install.source} @ {install.saves_root} ({mode}){suffix}")


def _print_esde_status() -> None:
    """List discovered ES-DE installs and whether launch hooks are wired up.

    Reports two drift dimensions when a snapshot is present:
      - Upstream drift: bundled `es_systems.xml` changed since install
        (e.g., RetroDECK update); the managed block now wraps stale
        commands.
      - Local drift: managed block in `custom_systems.xml` was edited
        by hand since install; re-running install would clobber edits.
    """
    installs = discover_esde_installs()
    if not installs:
        click.echo("  esde:        (not detected)")
        return
    snapshot = read_snapshot(default_snapshot_path())
    drift = detect_drift(snapshot) if snapshot is not None else None
    for install in installs:
        bundled_repr = (
            str(install.bundled_systems_xml)
            if install.bundled_systems_xml
            else "(no bundled systems file found)"
        )
        custom_status = "exists" if install.has_custom_systems_file else "not yet created"
        click.echo(f"  esde:        {install.source}")
        click.echo(f"    bundled:   {bundled_repr}")
        click.echo(f"    custom:    {install.custom_systems_xml} ({custom_status})")
        click.echo(f"    hooks:     {_describe_hook_status(install, drift)}")


_STATUS_MESSAGES: dict[DriftKind, str] = {
    DriftKind.CLEAN: "✓ installed and in sync",
    DriftKind.NO_SNAPSHOT: (
        "not installed (run `ferry install-launch-hooks` for per-launch save sync)"
    ),
    DriftKind.BLOCK_PRESENT_NO_SNAPSHOT: (
        "managed block present but no drift snapshot — "
        "re-run `ferry install-launch-hooks` to enable drift detection"
    ),
    DriftKind.SNAPSHOT_FOR_OTHER_INSTALL: (
        "not installed for this profile (snapshot belongs to another install)"
    ),
    DriftKind.UPSTREAM_AND_LOCAL_DRIFT: (
        "⚠ bundled changed AND managed block edited locally "
        "(resolve manually — re-running with --force clobbers edits)"
    ),
    DriftKind.UPSTREAM_DRIFT: "⚠ bundled changed (re-run `ferry install-launch-hooks`)",
    DriftKind.LOCAL_DRIFT: (
        "⚠ managed block edited locally "
        "(re-running `ferry install-launch-hooks` clobbers edits unless --force)"
    ),
    DriftKind.BUNDLED_MISSING: (
        "⚠ bundled file from snapshot is missing (re-run `ferry install-launch-hooks` to refresh)"
    ),
    DriftKind.BLOCK_REMOVED: (
        "⚠ managed block was removed "
        "(re-run `ferry install-launch-hooks` to reinstate, "
        "or `uninstall-launch-hooks` to clear)"
    ),
}


def _describe_hook_status(install: ESDEInstall, drift: HookStatus | None) -> str:
    """Single-line summary of launch-hooks state for one install.

    Drift is global (one snapshot per machine) but installs are per-source.
    We only attribute a snapshot's state to the install whose
    `custom_systems_xml` matches the snapshot's path — other discovered
    installs that don't match get a "not installed" line.
    """
    kind = classify_drift(
        drift,
        custom_systems_path=install.custom_systems_xml,
        block_present=extract_managed_block(install.custom_systems_xml) is not None,
    )
    return _STATUS_MESSAGES[kind]


# Stem-suffix pattern matching RetroDECK's SRM backup convention:
# `<basename>_YYYYMMDD_HHMMSS.<ext>`. RetroDECK's "backup save data"
# feature creates these as siblings of the canonical SRM. ferry doesn't
# touch them (the walker filters on extension, not stem shape) but they
# DO show up as unmatched-orphan warnings because their stem doesn't
# match any tracked ROM. We classify them out of the default listing
# so the user sees real orphans, not backups.
_BACKUP_SUFFIX_PATTERN = re.compile(r"_\d{8}_\d{6}$")


def _is_backup_noise(filename: str) -> bool:
    """True iff `filename` looks like a RetroDECK SRM backup artifact."""
    return bool(_BACKUP_SUFFIX_PATTERN.search(Path(filename).stem))


def _print_orphan_saves(config: Config, state: LibraryState, *, show_all: bool) -> None:
    """List RetroArch save files on disk that don't match any tracked ROM.

    Calls `list_local_saves` (the same walker save sync uses) and surfaces
    its warnings with the actual filenames so the user can decide:
      - sync the missing ROM to RomM,
      - delete the orphan save,
      - ignore (it's from a ROM the user manages outside ferry).

    Backup-noise entries (RetroDECK `_YYYYMMDD_HHMMSS` SRMs) are
    classified separately and hidden by default; `--show-all` exposes
    them. Dolphin's walker is per-rom-scoped so doesn't surface
    unmatched GCIs the same way; out of scope for this listing.
    """
    installs = discover_retroarch_installs()
    if not installs:
        click.echo("  retroarch:   (no install detected)")
        return
    install = _resolve_ra_install_for_orphans(config, installs)
    if install is None:
        # Configured choice missing or ambiguous; the [saves] section
        # already explained why. Don't double-warn here.
        return
    core_info = _load_core_info_index(install)
    _, warnings = list_ra_local_saves(install, state.roms.values(), core_info)

    real: list[str] = []
    backup: list[str] = []
    for warning in warnings:
        # Walker warnings include the relative path quoted in the
        # message; recover it from the warning text so we can classify.
        filename = _extract_filename_from_warning(warning)
        if filename is None:
            real.append(warning)
            continue
        if _is_backup_noise(filename):
            backup.append(filename)
        else:
            real.append(filename)

    if not real and not backup:
        click.echo("  retroarch:   ✓ all local saves match a tracked ROM")
        return

    parts = []
    if real:
        parts.append(f"{len(real)} unmatched")
    if backup:
        parts.append(f"{len(backup)} RetroDECK backup{'s' if len(backup) != 1 else ''}")
    click.echo(f"  retroarch:   {', '.join(parts)}")

    for entry in real:
        click.echo(f"    - {entry}")
    if show_all:
        for entry in backup:
            click.echo(f"    - {entry}  (RetroDECK backup)")
    elif backup:
        click.echo(f"    ({len(backup)} backup-noise hidden — pass --show-all to list)")


def _resolve_ra_install_for_orphans(
    config: Config, installs: list[RetroArchInstall]
) -> RetroArchInstall | None:
    """Pick the RetroArch install for orphan-listing purposes.

    Mirrors `_select_retroarch_install` in `cli/sync.py`: configured
    override wins, otherwise auto-select. Returns None when ambiguous
    or the configured choice doesn't match — in those cases the
    `[saves]` section has already told the user; we stay quiet here.
    """
    return resolve_install(
        installs,
        configured_source=config.saves.retroarch_install if config.saves else None,
        source_of=lambda i: i.source,
        has_active=lambda i: i.has_saves,
    ).install


def _load_core_info_index(install: RetroArchInstall) -> CoreInfoIndex:
    """Wrap the install in a `CoreInfoIndex`; loading is lazy + best-effort.

    Orphan listing only needs filenames, not accurate emulator labels —
    this is here to satisfy the walker's optional `core_info` argument.
    """
    return CoreInfoIndex(install)


def _extract_filename_from_warning(warning: str) -> str | None:
    """Pull the quoted filename out of a walker warning. None if not found."""
    # Walker emits `could not match save 'Foo.srm' to any known ROM ...`
    # and `could not read save 'Foo.srm': ...`. Single-quoted in both.
    match = re.search(r"'([^']+)'", warning)
    return match.group(1) if match else None


def _print_reconcile(state: LibraryState, config: Config) -> None:
    """Per-platform breakdown of what's tracked vs. on disk."""
    if config.destination is None:
        return
    roms_base = config.destination.roms_base

    by_platform: dict[str, list[RomState]] = defaultdict(list)
    for rom in state.roms.values():
        by_platform[rom.platform_slug].append(rom)

    click.echo("")
    click.echo("ROMs by platform:")
    total_missing_primary = 0
    for platform_slug in sorted(by_platform):
        roms = by_platform[platform_slug]
        missing_primary = 0
        for rom in roms:
            primary_abs = roms_base / rom.primary_output.path
            if not primary_abs.exists():
                missing_primary += 1
        total_missing_primary += missing_primary
        resolved_dir = resolve_platform_dir(platform_slug)
        marker = "✓" if missing_primary == 0 else "✗"
        suffix = f"  ({missing_primary} missing on disk)" if missing_primary else ""
        slug_display = (
            platform_slug if platform_slug == resolved_dir else f"{platform_slug} → {resolved_dir}/"
        )
        click.echo(f"  {marker} {slug_display:<24} {len(roms):>5}{suffix}")

    if total_missing_primary:
        click.echo("")
        click.echo("Issues:")
        click.echo(
            f"  - {total_missing_primary} ROM(s) have missing primary outputs "
            "(next `ferry sync` will re-download)"
        )


def _print_trash_summary(trash_root: Path) -> None:
    if not trash_root.exists():
        click.echo(f"trash:         {trash_root} (empty)")
        return
    entries = [e for e in trash_root.iterdir() if e.is_dir()]
    total_bytes = 0
    oldest_age: timedelta | None = None
    now = datetime.now(UTC)
    for e in entries:
        for f in e.rglob("*"):
            if f.is_file():
                with contextlib.suppress(OSError):
                    total_bytes += f.stat().st_size
        if "__" in e.name:
            ts_str = e.name.split("__", 1)[0]
            try:
                ts = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                age = now - ts
                if oldest_age is None or age > oldest_age:
                    oldest_age = age
            except ValueError:
                pass
    if not entries:
        click.echo(f"trash:         {trash_root} (empty)")
    else:
        age_str = f", oldest {oldest_age.days} days" if oldest_age else ""
        click.echo(
            f"trash:         {trash_root} "
            f"({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, "
            f"{format_bytes(total_bytes)}{age_str})"
        )
