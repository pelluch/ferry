"""`ferry status` — read-only introspection of state vs disk.

No HTTP calls. Tells the user what ferry currently *knows* (state.json),
what's actually on disk, and where the two diverge. The high-frequency
"did my last sync work?" answer.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from ferry import __version__
from ferry.adapters.sidecar import sidecar_path_for
from ferry.adapters.state_store import default_state_path, load_state
from ferry.config import ConfigError, load_config
from ferry.config.schema import Config
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.state import LibraryState, RomState
from ferry.services.trash import default_trash_root


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
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
    click.echo(f"  api_key:     {_mask(config.romm.api_key)}")

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
        click.echo(f"  roms_base:   {d.roms_base} {_path_status(d.roms_base)}")
        if d.bios_base is None:
            click.echo("  bios_base:   (per-emulator)")
        else:
            click.echo(f"  bios_base:   {d.bios_base} {_path_status(d.bios_base)}")

    if state.roms and config.destination is not None:
        _print_reconcile(state, config)
    elif not state.roms:
        click.echo("")
        click.echo("(state is empty — first sync will populate)")


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
    total_missing_sidecar = 0
    for platform_slug in sorted(by_platform):
        roms = by_platform[platform_slug]
        missing_primary = 0
        missing_sidecar = 0
        for rom in roms:
            primary_abs = roms_base / rom.primary_output.path
            if not primary_abs.exists():
                missing_primary += 1
            elif not sidecar_path_for(primary_abs).exists():
                missing_sidecar += 1
        total_missing_primary += missing_primary
        total_missing_sidecar += missing_sidecar
        resolved_dir = resolve_platform_dir(platform_slug)
        marker = "✓" if (missing_primary == 0 and missing_sidecar == 0) else "✗"
        flags = []
        if missing_primary:
            flags.append(f"{missing_primary} missing on disk")
        if missing_sidecar:
            flags.append(f"{missing_sidecar} missing sidecars")
        suffix = f"  ({', '.join(flags)})" if flags else ""
        slug_display = (
            platform_slug if platform_slug == resolved_dir else f"{platform_slug} → {resolved_dir}/"
        )
        click.echo(f"  {marker} {slug_display:<24} {len(roms):>5}{suffix}")

    if total_missing_primary or total_missing_sidecar:
        click.echo("")
        click.echo("Issues:")
        if total_missing_primary:
            click.echo(
                f"  - {total_missing_primary} ROM(s) have missing primary outputs "
                "(next `ferry sync` will re-download)"
            )
        if total_missing_sidecar:
            click.echo(
                f"  - {total_missing_sidecar} ROM(s) have missing sidecars "
                "(next `ferry sync` will regenerate from state)"
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
            f"{_format_bytes(total_bytes)}{age_str})"
        )


def _path_status(path: Path) -> str:
    if not path.exists():
        return "(missing)"
    if not path.is_dir():
        return "(not a directory)"
    return "(exists)"


def _mask(token: str) -> str:
    if len(token) <= 6:
        return "(set)"
    return f"{token[:4]}…{token[-3:]}"


def _format_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{int(n)} B"
