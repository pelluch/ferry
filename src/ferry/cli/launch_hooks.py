"""`ferry install-launch-hooks` / `uninstall-launch-hooks` CLI commands.

Generates wrapper script + custom_systems XML overrides that wire
`ferry sync --rom %ROM%` into ES-DE's launch chain (pre + post per
session). Supports RetroDECK and native ES-DE installs; auto-detects
which is present.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import click

from ferry.adapters.esde_paths import (
    ESDEInstall,
    discover_esde_installs,
    select_active_install,
)
from ferry.config import ConfigError, load_config
from ferry.config.schema import LaunchHooksConfig
from ferry.services.launch_hooks import (
    HookStatus,
    WrapperConfig,
    default_snapshot_path,
    default_wrapper_path,
    delete_snapshot,
    detect_drift,
    install_managed_block,
    make_snapshot,
    read_snapshot,
    render_managed_block,
    render_wrapper_script,
    uninstall_managed_block,
    write_snapshot,
)

logger = logging.getLogger(__name__)


@click.command(name="install-launch-hooks")
@click.option(
    "--profile",
    type=click.Choice(["retrodeck-flatpak", "native"]),
    default=None,
    help="Pick a specific ES-DE profile when multiple are detected.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be written without modifying anything.",
)
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Overwrite the managed block even if local edits are detected. "
        "Without this, ferry refuses to clobber hand-edits to the block."
    ),
)
@click.pass_context
def install_launch_hooks(
    ctx: click.Context, profile: str | None, dry_run: bool, force: bool
) -> None:
    """Wire `ferry sync --rom` into ES-DE's launch chain (pre + post)."""
    try:
        loaded = load_config(ctx.obj.get("config_path"))
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    config = loaded.config

    install = _select_install(profile)
    if install.bundled_systems_xml is None:
        raise click.ClickException(
            f"can't generate launch hooks for {install.source}: bundled "
            f"es_systems.xml not found. Has ES-DE been launched at least once?"
        )

    wrapper_path = default_wrapper_path()
    log_path = _resolve_log_path(config.launch_hooks)
    wrapper_config = WrapperConfig(
        ferry_bin=_resolve_ferry_bin(),
        host_home=Path.home(),
        host_xdg_config=Path.home() / ".config",
        host_xdg_state=Path.home() / ".local" / "state",
        host_xdg_cache=Path.home() / ".cache",
        log_path=log_path if config.launch_hooks.log_enabled else None,
    )
    wrapper_script = render_wrapper_script(wrapper_config)
    managed_block = render_managed_block(install.bundled_systems_xml, wrapper_path)

    snapshot_path = default_snapshot_path()
    existing_snapshot = read_snapshot(snapshot_path)
    drift = detect_drift(existing_snapshot) if existing_snapshot is not None else None

    # Local-drift gate: refuse to clobber hand-edits unless --force.
    if drift is not None and drift.local_drift and not force:
        if dry_run:
            # In dry-run we still show the full preview but flag the gate so
            # the user knows the real run will refuse without --force.
            click.echo(
                f"⚠ managed block in {install.custom_systems_xml} has been edited "
                "since the last `ferry install-launch-hooks`."
            )
            click.echo(
                "  A real run would REFUSE to overwrite without --force. "
                "Either copy your edits aside and re-run, or pass --force "
                "to clobber them."
            )
            click.echo("")
        else:
            raise click.ClickException(
                f"managed block in {install.custom_systems_xml} has been edited "
                "since the last `ferry install-launch-hooks`. Re-running would "
                "overwrite those edits.\n\n"
                "Either:\n"
                "  - copy your edits aside, re-run install-launch-hooks, then re-apply, or\n"
                "  - re-run with --force to overwrite (your edits will be lost)."
            )

    n_systems = managed_block.count("<system>")

    if dry_run:
        click.echo(f"Would write wrapper script: {wrapper_path}")
        click.echo(
            f"  ferry binary: {wrapper_config.ferry_bin}\n"
            f"  log: {wrapper_config.log_path or '(disabled)'}"
        )
        click.echo(f"Would update custom_systems: {install.custom_systems_xml}")
        click.echo(f"  Managed block: {n_systems} system(s) wrapped")
        click.echo(f"Would write snapshot: {snapshot_path}")
        _print_install_drift_preview(drift)
        click.echo("(dry run — no files modified)")
        return

    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(wrapper_script)
    wrapper_path.chmod(0o755)
    click.echo(f"✓ wrote wrapper script: {wrapper_path}")

    install_managed_block(install.custom_systems_xml, managed_block)
    click.echo(f"✓ updated {install.custom_systems_xml}: {n_systems} system(s) wrapped")

    snapshot = make_snapshot(
        bundled_path=install.bundled_systems_xml,
        custom_systems_path=install.custom_systems_xml,
    )
    write_snapshot(snapshot_path, snapshot)
    click.echo(f"✓ wrote drift snapshot: {snapshot_path}")

    click.echo("")
    click.echo(
        "Restart ES-DE for the changes to take effect. Each game launch "
        "now runs `ferry sync --rom <path>` before and after."
    )
    if config.launch_hooks.log_enabled:
        click.echo(f"Per-session log: {wrapper_config.log_path}")


@click.command(name="uninstall-launch-hooks")
@click.option(
    "--profile",
    type=click.Choice(["retrodeck-flatpak", "native"]),
    default=None,
    help="Pick a specific ES-DE profile when multiple are detected.",
)
@click.pass_context
def uninstall_launch_hooks(ctx: click.Context, profile: str | None) -> None:
    """Remove ferry's launch-hook wrappers + wrapper script."""
    install = _select_install(profile)
    wrapper_path = default_wrapper_path()
    snapshot_path = default_snapshot_path()

    removed = uninstall_managed_block(install.custom_systems_xml)
    if removed:
        click.echo(f"✓ removed managed block from {install.custom_systems_xml}")
    else:
        click.echo(f"  no managed block in {install.custom_systems_xml} (nothing to remove)")

    if wrapper_path.is_file():
        wrapper_path.unlink()
        click.echo(f"✓ removed wrapper script: {wrapper_path}")
    else:
        click.echo(f"  wrapper script {wrapper_path} not present (nothing to remove)")

    if delete_snapshot(snapshot_path):
        click.echo(f"✓ removed drift snapshot: {snapshot_path}")
    else:
        click.echo(f"  drift snapshot {snapshot_path} not present (nothing to remove)")

    click.echo("")
    click.echo("Restart ES-DE so it re-reads custom_systems and uses bundled commands again.")


def _print_install_drift_preview(drift: HookStatus | None) -> None:
    """Tell the user what the snapshot vs disk look like in dry-run.

    Mirrors the `status`-side rendering (`format_hook_status_line`) so the
    dry-run preview describes the same drift state the user would see in
    `ferry status` if they ran it instead.
    """
    if drift is None:
        click.echo("Drift status: no snapshot — clean install (would write fresh snapshot)")
        return
    if drift.is_clean:
        click.echo("Drift status: ✓ snapshot matches disk — re-running is a no-op")
        return
    if drift.upstream_drift and drift.local_drift:
        click.echo(
            "Drift status: ⚠ bundled file changed AND managed block edited — "
            "real run requires --force (clobbers local edits)"
        )
        return
    if drift.upstream_drift:
        click.echo(
            "Drift status: ⚠ bundled file changed — real run rebuilds managed "
            "block from new bundled"
        )
        return
    if drift.local_drift:
        click.echo(
            "Drift status: ⚠ managed block edited locally — real run requires "
            "--force (clobbers your edits)"
        )
        return
    if not drift.bundled_present:
        click.echo(
            "Drift status: ⚠ bundled file from snapshot is missing — real run "
            "rebuilds against the currently-discovered bundled file"
        )
        return
    if not drift.block_present:
        click.echo("Drift status: ⚠ managed block was removed since install — real run re-adds it")
        return


def _select_install(profile: str | None) -> ESDEInstall:
    """Resolve an `ESDEInstall` from discovery + the optional --profile override."""
    installs = discover_esde_installs()
    if not installs:
        raise click.ClickException(
            "no ES-DE install detected. ferry probes RetroDECK's flatpak and "
            "native ES-DE; neither is present."
        )
    if profile is not None:
        for install in installs:
            if install.source == profile:
                return install
        available = ", ".join(i.source for i in installs)
        raise click.ClickException(f"--profile {profile!r} not found. detected: {available}")
    active = select_active_install(installs)
    if active is None:
        sources = ", ".join(i.source for i in installs)
        raise click.ClickException(
            f"multiple ES-DE installs detected ({sources}); pass --profile to disambiguate."
        )
    return active


def _resolve_log_path(launch_hooks_cfg: LaunchHooksConfig) -> Path:
    """Default to `$XDG_STATE_HOME/ferry/launch.log` when no override."""
    if launch_hooks_cfg.log_path is not None:
        return launch_hooks_cfg.log_path
    import os

    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "ferry" / "launch.log"


def _resolve_ferry_bin() -> Path:
    """Find the host-side ferry binary the wrapper should call.

    Defaults to the currently-running ferry's path; falls back to
    `~/.local/bin/ferry` (uv tool default). The user can edit the
    generated wrapper if they need a different path.
    """
    if sys.argv and Path(sys.argv[0]).is_absolute():
        candidate = Path(sys.argv[0])
        if candidate.is_file():
            return candidate
    on_path = shutil.which("ferry")
    if on_path:
        return Path(on_path)
    return Path.home() / ".local" / "bin" / "ferry"
