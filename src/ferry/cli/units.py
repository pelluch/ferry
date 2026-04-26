"""`ferry install-units` and `ferry uninstall-units`.

Thin orchestration over `ferry.services.systemd_units`:
  - probe systemd availability,
  - validate the schedule,
  - write/remove the two unit files,
  - run the systemctl side-effects (daemon-reload, enable --now, disable --now).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import click

from ferry.services.systemd_units import (
    SERVICE_FILENAME,
    TIMER_FILENAME,
    TIMER_UNIT_NAME,
    ScheduleError,
    default_units_dir,
    remove_units,
    systemd_user_available,
    validate_schedule,
    write_units,
)

_FERRY_NOT_ON_PATH_HINT = (
    "ferry binary not found on $PATH — the systemd service needs an absolute "
    "path. Install ferry globally first:\n\n"
    "    uv tool install /path/to/ferry/checkout\n"
    "    # or, once the binary is published from a git remote:\n"
    "    uv tool install git+https://github.com/.../ferry\n\n"
    "Then re-run `ferry install-units`."
)

_NO_SYSTEMD_HINT = (
    "no reachable systemd user instance.\n\n"
    "ferry's scheduled-sync unit relies on `systemctl --user`. If you're on a "
    "non-systemd distro (Devuan, Artix, Void, Alpine, …) you can schedule "
    "ferry yourself with cron:\n\n"
    "    crontab -e   # then add:\n"
    "    @daily $HOME/.local/bin/ferry sync >> $HOME/.cache/ferry/cron.log 2>&1\n\n"
    "(Native cron support is on the roadmap — see DESIGN.md §9.)"
)


@click.command("install-units")
@click.option(
    "--schedule",
    "schedule",
    metavar="SPEC",
    help=(
        "systemd OnCalendar spec (e.g. `daily`, `hourly`, `Mon *-*-* 03:00:00`). "
        "Default: daily. Schedules faster than every 10 minutes are rejected."
    ),
)
def install_units(schedule: str | None) -> None:
    """Install ferry's systemd user timer for unattended sync."""
    if not systemd_user_available():
        raise click.ClickException(_NO_SYSTEMD_HINT)

    ferry_path = _resolve_ferry_path()
    if ferry_path is None:
        raise click.ClickException(_FERRY_NOT_ON_PATH_HINT)

    if schedule is not None:
        try:
            validate_schedule(schedule)
        except ScheduleError as e:
            raise click.ClickException(str(e)) from e

    target_dir = default_units_dir()
    written = write_units(target_dir, ferry_path=ferry_path, schedule=schedule)
    click.echo(f"wrote {written['service']}")
    click.echo(f"  (ExecStart={ferry_path} sync)")
    click.echo(f"wrote {written['timer']}")
    if schedule is not None:
        click.echo(f"  (OnCalendar={schedule})")

    _systemctl("daemon-reload")
    _systemctl("enable", "--now", TIMER_UNIT_NAME)

    click.echo("")
    click.echo(f"✓ enabled {TIMER_UNIT_NAME}")
    click.echo("")
    click.echo("Inspect the timer:")
    click.echo("    systemctl --user list-timers ferry-sync.timer")
    click.echo("")
    click.echo("Change the schedule later by re-running with a new --schedule:")
    click.echo('    ferry install-units --schedule "Mon *-*-* 03:00:00"')
    click.echo("")
    click.echo("Remove with: ferry uninstall-units")


@click.command("uninstall-units")
def uninstall_units() -> None:
    """Remove ferry's systemd user timer and service files."""
    target_dir = default_units_dir()

    if systemd_user_available():
        # Best-effort: timer may already be disabled, or never enabled. Don't
        # crash on a non-zero from systemctl — just surface what it said.
        _systemctl_lenient("disable", "--now", TIMER_UNIT_NAME)

    removed = remove_units(target_dir)
    if removed:
        for path in removed:
            click.echo(f"removed {path}")
    else:
        click.echo(
            f"nothing to remove — neither {SERVICE_FILENAME} nor "
            f"{TIMER_FILENAME} were present in {target_dir}"
        )

    if systemd_user_available():
        _systemctl("daemon-reload")

    click.echo("")
    click.echo("✓ ferry's systemd units are uninstalled")


def _systemctl(*args: str) -> None:
    cmd = ["systemctl", "--user", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip() or "(no output)"
        raise click.ClickException(f"`{' '.join(cmd)}` failed:\n{msg}")


def _systemctl_lenient(*args: str) -> None:
    """Run systemctl, surface any output but don't crash on failure."""
    cmd = ["systemctl", "--user", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        if msg:
            click.echo(f"(systemctl: {msg})", err=True)


def _resolve_ferry_path() -> Path | None:
    """Locate the absolute path to the user's installed ferry binary.

    Returns None if ferry isn't on $PATH (e.g., during dev when invoked via
    `uv run`). The systemd service needs an absolute path that survives the
    timer firing in a fresh shell environment, so PATH-relative isn't enough.
    """
    found = shutil.which("ferry")
    return Path(found).resolve() if found else None
