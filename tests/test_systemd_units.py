"""Tests for the pure file-side of systemd unit installation.

Schedule validation tests rely on real `systemd-analyze`; they skip if it's
unavailable (e.g., on macOS CI runners). Everything else is OS-agnostic.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from ferry.services.systemd_units import (
    MINIMUM_SCHEDULE_INTERVAL,
    SERVICE_FILENAME,
    TIMER_FILENAME,
    ScheduleError,
    default_units_dir,
    remove_units,
    validate_schedule,
    write_units,
)

_HAS_SYSTEMD_ANALYZE = shutil.which("systemd-analyze") is not None
_FAKE_FERRY = Path("/home/u/.local/bin/ferry")


# ---------------------------------------------------------------------------
# default_units_dir
# ---------------------------------------------------------------------------


def test_default_units_dir_uses_xdg_config_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_units_dir() == tmp_path / "systemd" / "user"


def test_default_units_dir_falls_back_to_home_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert default_units_dir() == tmp_path / ".config" / "systemd" / "user"


# ---------------------------------------------------------------------------
# write_units
# ---------------------------------------------------------------------------


def test_write_units_creates_both_files(tmp_path: Path) -> None:
    paths = write_units(tmp_path, ferry_path=_FAKE_FERRY)
    assert paths["service"] == tmp_path / SERVICE_FILENAME
    assert paths["timer"] == tmp_path / TIMER_FILENAME
    assert paths["service"].exists()
    assert paths["timer"].exists()


def test_write_units_creates_target_dir_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "deeply" / "nested"
    write_units(target, ferry_path=_FAKE_FERRY)
    assert (target / SERVICE_FILENAME).exists()


def test_write_units_substitutes_ferry_path(tmp_path: Path) -> None:
    write_units(tmp_path, ferry_path=Path("/opt/special/ferry"))
    service_text = (tmp_path / SERVICE_FILENAME).read_text()
    assert "ExecStart=/opt/special/ferry sync" in service_text
    # The placeholder must not survive — would mean broken substitution.
    assert "__FERRY_PATH__" not in service_text


def test_write_units_default_schedule_is_daily(tmp_path: Path) -> None:
    write_units(tmp_path, ferry_path=_FAKE_FERRY)
    timer_text = (tmp_path / TIMER_FILENAME).read_text()
    assert "OnCalendar=daily" in timer_text


def test_write_units_substitutes_schedule(tmp_path: Path) -> None:
    write_units(tmp_path, ferry_path=_FAKE_FERRY, schedule="hourly")
    timer_text = (tmp_path / TIMER_FILENAME).read_text()
    assert "OnCalendar=hourly" in timer_text
    assert "OnCalendar=daily" not in timer_text


def test_write_units_substitutes_complex_schedule(tmp_path: Path) -> None:
    spec = "Mon *-*-* 03:00:00"
    write_units(tmp_path, ferry_path=_FAKE_FERRY, schedule=spec)
    assert f"OnCalendar={spec}" in (tmp_path / TIMER_FILENAME).read_text()


def test_write_units_is_idempotent_on_re_run(tmp_path: Path) -> None:
    write_units(tmp_path, ferry_path=_FAKE_FERRY, schedule="daily")
    write_units(tmp_path, ferry_path=_FAKE_FERRY, schedule="hourly")
    timer_text = (tmp_path / TIMER_FILENAME).read_text()
    assert "OnCalendar=hourly" in timer_text
    # Old schedule is fully replaced, not appended.
    assert timer_text.count("OnCalendar=") == 1


# ---------------------------------------------------------------------------
# remove_units
# ---------------------------------------------------------------------------


def test_remove_units_removes_both_files(tmp_path: Path) -> None:
    write_units(tmp_path, ferry_path=_FAKE_FERRY)
    removed = remove_units(tmp_path)
    assert sorted(p.name for p in removed) == sorted([SERVICE_FILENAME, TIMER_FILENAME])
    assert not (tmp_path / SERVICE_FILENAME).exists()
    assert not (tmp_path / TIMER_FILENAME).exists()


def test_remove_units_returns_empty_when_nothing_present(tmp_path: Path) -> None:
    assert remove_units(tmp_path) == []


def test_remove_units_handles_partial_state(tmp_path: Path) -> None:
    """One file present, one missing — remove what's there, return what was removed."""
    (tmp_path / SERVICE_FILENAME).write_text("stub")
    removed = remove_units(tmp_path)
    assert len(removed) == 1
    assert removed[0].name == SERVICE_FILENAME


def test_remove_units_ignores_other_files(tmp_path: Path) -> None:
    """User may have other unrelated unit files in ~/.config/systemd/user/."""
    write_units(tmp_path, ferry_path=_FAKE_FERRY)
    bystander = tmp_path / "syncthing.service"
    bystander.write_text("[Unit]\n")
    remove_units(tmp_path)
    assert bystander.exists()


# ---------------------------------------------------------------------------
# validate_schedule (real systemd-analyze)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze not available")
def test_validate_schedule_accepts_daily() -> None:
    validate_schedule("daily")  # 24h gap → fine


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze not available")
def test_validate_schedule_accepts_hourly() -> None:
    validate_schedule("hourly")  # 60min gap → fine


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze not available")
def test_validate_schedule_accepts_complex_spec() -> None:
    validate_schedule("Mon *-*-* 03:00:00")  # weekly Monday 3am


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze not available")
def test_validate_schedule_accepts_exact_minimum() -> None:
    """`*:0/10` fires every 10 minutes — equal to MINIMUM, must pass (strict <)."""
    assert timedelta(minutes=10) == MINIMUM_SCHEDULE_INTERVAL
    validate_schedule("*:0/10")


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze not available")
def test_validate_schedule_rejects_below_minimum() -> None:
    with pytest.raises(ScheduleError, match="minimum is 10min"):
        validate_schedule("*:0/9")


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze not available")
def test_validate_schedule_rejects_every_minute() -> None:
    with pytest.raises(ScheduleError, match="minimum is 10min"):
        validate_schedule("*:0/1")


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze not available")
def test_validate_schedule_rejects_invalid_syntax() -> None:
    with pytest.raises(ScheduleError, match="systemd rejected"):
        validate_schedule("not-a-real-cadence")
