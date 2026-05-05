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
    WrapperConfig,
    default_wrapper_path,
    install_managed_block,
    render_managed_block,
    render_wrapper_script,
    uninstall_managed_block,
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
@click.pass_context
def install_launch_hooks(ctx: click.Context, profile: str | None, dry_run: bool) -> None:
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

    if dry_run:
        click.echo(f"Would write wrapper script: {wrapper_path}")
        click.echo(
            f"  ferry binary: {wrapper_config.ferry_bin}\n"
            f"  log: {wrapper_config.log_path or '(disabled)'}"
        )
        click.echo(f"Would update custom_systems: {install.custom_systems_xml}")
        n_systems = managed_block.count("<system>")
        click.echo(f"  Managed block: {n_systems} system(s) wrapped")
        click.echo("(dry run — no files modified)")
        return

    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(wrapper_script)
    wrapper_path.chmod(0o755)
    click.echo(f"✓ wrote wrapper script: {wrapper_path}")

    install_managed_block(install.custom_systems_xml, managed_block)
    n_systems = managed_block.count("<system>")
    click.echo(f"✓ updated {install.custom_systems_xml}: {n_systems} system(s) wrapped")

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

    click.echo("")
    click.echo("Restart ES-DE so it re-reads custom_systems and uses bundled commands again.")


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
