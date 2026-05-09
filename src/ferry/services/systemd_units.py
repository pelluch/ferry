"""Install and remove ferry's systemd user units.

The two unit files (`ferry-sync.service`, `ferry-sync.timer`) ship as
package data under `ferry/data/systemd/`. The CLI orchestrates by:
  1. probing for an available systemd user instance,
  2. validating any user-supplied --schedule via `systemd-analyze`,
  3. writing the units (this module),
  4. running `systemctl --user daemon-reload` + enable/disable.

Steps 1, 2, and 4 are systemd-dependent. Step 3 is pure file I/O — that's
the surface this module exposes plus the validators that guard it.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

from ferry.domain.user_dirs import config_dir

# Floor for periodic sync cadence. RomM library updates are not real-time,
# typical sync runs take seconds, and the only thing a faster cadence buys
# you is a hot RomM server and noisy logs. Picked deliberately above the
# obvious foot-guns (every 1/5 minutes during initial debugging).
MINIMUM_SCHEDULE_INTERVAL = timedelta(minutes=10)

SERVICE_FILENAME = "ferry-sync.service"
TIMER_FILENAME = "ferry-sync.timer"
TIMER_UNIT_NAME = "ferry-sync.timer"

# Sentinel placeholder in the service template — substituted at install time
# with the absolute path to the user's ferry binary. The template is not
# directly usable; install_units must always do this rewrite.
_FERRY_PATH_PLACEHOLDER = "__FERRY_PATH__"

# Matches `(in UTC): Sun 2026-04-26 07:00:00 UTC` from `systemd-analyze calendar`.
_UTC_LINE_RE = re.compile(r"\(in UTC\):\s+\S+\s+(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+UTC")


class ScheduleError(ValueError):
    """User-supplied --schedule was rejected (invalid syntax or too frequent)."""


class SystemdUnavailableError(RuntimeError):
    """No reachable systemd user instance — install/uninstall can't proceed."""


def default_units_dir() -> Path:
    """Resolve the canonical user-units directory."""
    return config_dir() / "systemd" / "user"


def systemd_user_available() -> bool:
    """True iff `systemctl --user` can reach an active user instance."""
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-unit-files",
                "--type=timer",
                "--no-pager",
                "--no-legend",
                "--plain",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def validate_schedule(spec: str) -> None:
    """Reject schedules that fire faster than MINIMUM_SCHEDULE_INTERVAL.

    Uses `systemd-analyze calendar --iterations=2` which both validates the
    syntax (returns non-zero on bad specs) and exposes the cadence (gap
    between consecutive firings). Raises ScheduleError on any failure.
    """
    try:
        result = subprocess.run(
            ["systemd-analyze", "calendar", "--iterations=2", spec],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError as e:
        raise ScheduleError(
            "`systemd-analyze` not found — required to validate the schedule."
        ) from e

    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip() or "unknown error"
        raise ScheduleError(f"systemd rejected schedule {spec!r}: {msg}")

    timestamps = [
        datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        for m in _UTC_LINE_RE.finditer(result.stdout)
    ]
    if len(timestamps) < 2:
        raise ScheduleError(
            f"could not determine cadence for {spec!r}: systemd-analyze returned "
            f"{len(timestamps)} firing(s). Output:\n{result.stdout}"
        )

    gap = timestamps[1] - timestamps[0]
    if gap < MINIMUM_SCHEDULE_INTERVAL:
        raise ScheduleError(
            f"schedule {spec!r} fires every {_format_gap(gap)} — minimum is "
            f"{_format_gap(MINIMUM_SCHEDULE_INTERVAL)}. Pick a slower cadence "
            f"(e.g. `hourly`, `daily`, or `*:0/30` for every 30 minutes)."
        )


def write_units(
    target_dir: Path,
    *,
    ferry_path: Path,
    schedule: str | None = None,
) -> dict[str, Path]:
    """Write service+timer to target_dir, baking in ferry's absolute path
    and optionally rewriting OnCalendar=.

    Returns {"service": path, "timer": path} for the two written files.
    Caller is responsible for `systemctl --user daemon-reload` afterwards.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    service_text = _read_template(SERVICE_FILENAME)
    if _FERRY_PATH_PLACEHOLDER not in service_text:
        # Defensive: someone modified the template without updating this code.
        raise RuntimeError(
            f"service template is missing the {_FERRY_PATH_PLACEHOLDER} sentinel "
            "— refusing to write a unit with an unsubstituted ExecStart. "
            f"Template content:\n{service_text}"
        )
    service_text = service_text.replace(_FERRY_PATH_PLACEHOLDER, str(ferry_path))

    timer_text = _read_template(TIMER_FILENAME)
    if schedule is not None:
        timer_text = _rewrite_oncalendar(timer_text, schedule)

    service_path = target_dir / SERVICE_FILENAME
    timer_path = target_dir / TIMER_FILENAME
    service_path.write_text(service_text)
    timer_path.write_text(timer_text)
    return {"service": service_path, "timer": timer_path}


def remove_units(target_dir: Path) -> list[Path]:
    """Remove our two unit files from target_dir. Returns paths actually removed."""
    removed: list[Path] = []
    for name in (SERVICE_FILENAME, TIMER_FILENAME):
        path = target_dir / name
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def _read_template(filename: str) -> str:
    return resources.files("ferry.data.systemd").joinpath(filename).read_text()


def _rewrite_oncalendar(timer_text: str, schedule: str) -> str:
    """Replace the first `OnCalendar=...` line. Templates ship with exactly one."""
    new_text, count = re.subn(
        r"^OnCalendar=.*$",
        f"OnCalendar={schedule}",
        timer_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 0:
        # Defensive: someone modified the template without updating this code.
        raise RuntimeError(
            f"timer template is missing an `OnCalendar=` line — refusing to write "
            f"a unit without a schedule. Template content:\n{timer_text}"
        )
    return new_text


def _format_gap(td: timedelta) -> str:
    seconds = int(td.total_seconds())
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours}h" if hours > 1 else "1h"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}min"
    return f"{seconds}s"
