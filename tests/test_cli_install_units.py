"""Tests for `ferry install-units` and `ferry uninstall-units`.

systemctl side-effects are mocked via subprocess.run patching. The pure
file-write logic is covered separately in test_systemd_units.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from ferry.cli import app
from ferry.services.systemd_units import (
    SERVICE_FILENAME,
    TIMER_FILENAME,
    ScheduleError,
)


@dataclass
class FakeRun:
    """Records subprocess.run invocations and returns canned exit codes."""

    calls: list[list[str]] = field(default_factory=list)
    returncode: int = 0
    stderr: str = ""

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr=self.stderr)


@pytest.fixture
def fake_units_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect default_units_dir() to a tmp dir."""
    target = tmp_path / "systemd-user"
    monkeypatch.setattr("ferry.cli.units.default_units_dir", lambda: target)
    return target


@pytest.fixture
def fake_systemctl(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRun]:
    """Stub subprocess.run inside cli.units so systemctl never runs for real."""
    fake = FakeRun()
    monkeypatch.setattr("ferry.cli.units.subprocess.run", fake)
    yield fake


@pytest.fixture
def systemd_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ferry.cli.units.systemd_user_available", lambda: True)


@pytest.fixture
def systemd_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ferry.cli.units.systemd_user_available", lambda: False)


@pytest.fixture
def ferry_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "fake-bin" / "ferry"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr("ferry.cli.units.shutil.which", lambda _name: str(fake))
    return fake


@pytest.fixture
def ferry_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ferry.cli.units.shutil.which", lambda _name: None)


@pytest.fixture
def skip_schedule_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some tests don't want to depend on systemd-analyze in the test runner."""
    monkeypatch.setattr("ferry.cli.units.validate_schedule", lambda _spec: None)


# ---------------------------------------------------------------------------
# install-units — happy paths
# ---------------------------------------------------------------------------


def test_install_units_writes_files_and_runs_systemctl(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
    ferry_on_path: Path,
) -> None:
    result = CliRunner().invoke(app, ["install-units"])
    assert result.exit_code == 0, result.output
    assert (fake_units_dir / SERVICE_FILENAME).exists()
    assert (fake_units_dir / TIMER_FILENAME).exists()

    service_text = (fake_units_dir / SERVICE_FILENAME).read_text()
    assert f"ExecStart={ferry_on_path} sync" in service_text

    cmds = [c[2:] for c in fake_systemctl.calls]  # strip ["systemctl", "--user"]
    assert ["daemon-reload"] in cmds
    assert ["enable", "--now", "ferry-sync.timer"] in cmds


def test_install_units_default_schedule_is_daily(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
    ferry_on_path: Path,
) -> None:
    CliRunner().invoke(app, ["install-units"])
    timer_text = (fake_units_dir / TIMER_FILENAME).read_text()
    assert "OnCalendar=daily" in timer_text


def test_install_units_with_schedule_rewrites_oncalendar(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
    ferry_on_path: Path,
    skip_schedule_validation: None,
) -> None:
    result = CliRunner().invoke(app, ["install-units", "--schedule", "hourly"])
    assert result.exit_code == 0, result.output
    timer_text = (fake_units_dir / TIMER_FILENAME).read_text()
    assert "OnCalendar=hourly" in timer_text
    assert "OnCalendar=daily" not in timer_text
    assert "OnCalendar=hourly" in result.output


def test_install_units_re_run_overwrites_schedule(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
    ferry_on_path: Path,
    skip_schedule_validation: None,
) -> None:
    runner = CliRunner()
    runner.invoke(app, ["install-units", "--schedule", "daily"])
    runner.invoke(app, ["install-units", "--schedule", "hourly"])
    timer_text = (fake_units_dir / TIMER_FILENAME).read_text()
    assert timer_text.count("OnCalendar=") == 1
    assert "OnCalendar=hourly" in timer_text


# ---------------------------------------------------------------------------
# install-units — refusals
# ---------------------------------------------------------------------------


def test_install_units_errors_when_systemd_missing(
    fake_units_dir: Path,
    systemd_absent: None,
    ferry_on_path: Path,
) -> None:
    result = CliRunner().invoke(app, ["install-units"])
    assert result.exit_code != 0
    assert "systemd" in result.output
    assert "cron" in result.output  # the fallback hint
    # No files should have been written.
    assert not (fake_units_dir / SERVICE_FILENAME).exists()


def test_install_units_errors_when_ferry_not_on_path(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
    ferry_not_on_path: None,
) -> None:
    result = CliRunner().invoke(app, ["install-units"])
    assert result.exit_code != 0
    assert "not found on $PATH" in result.output
    assert "uv tool install" in result.output
    assert not (fake_units_dir / SERVICE_FILENAME).exists()


def test_install_units_propagates_schedule_error(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
    ferry_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_spec: str) -> None:
        raise ScheduleError("schedule '*:0/1' fires every 1min — minimum is 10min.")

    monkeypatch.setattr("ferry.cli.units.validate_schedule", _raise)
    result = CliRunner().invoke(app, ["install-units", "--schedule", "*:0/1"])
    assert result.exit_code != 0
    assert "minimum is 10min" in result.output
    assert not (fake_units_dir / SERVICE_FILENAME).exists()


def test_install_units_aborts_when_systemctl_fails(
    fake_units_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    systemd_present: None,
    ferry_on_path: Path,
) -> None:
    """If `systemctl --user enable --now` fails, surface the error visibly."""
    fake = FakeRun(returncode=1, stderr="Failed to enable unit: blah")
    monkeypatch.setattr("ferry.cli.units.subprocess.run", fake)
    result = CliRunner().invoke(app, ["install-units"])
    assert result.exit_code != 0
    assert "Failed to enable unit" in result.output


# ---------------------------------------------------------------------------
# uninstall-units
# ---------------------------------------------------------------------------


def test_uninstall_units_removes_existing_files(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
    ferry_on_path: Path,
) -> None:
    fake_units_dir.mkdir(parents=True)
    (fake_units_dir / SERVICE_FILENAME).write_text("stub")
    (fake_units_dir / TIMER_FILENAME).write_text("stub")

    result = CliRunner().invoke(app, ["uninstall-units"])
    assert result.exit_code == 0, result.output
    assert not (fake_units_dir / SERVICE_FILENAME).exists()
    assert not (fake_units_dir / TIMER_FILENAME).exists()
    assert "removed" in result.output

    cmds = [c[2:] for c in fake_systemctl.calls]
    assert ["disable", "--now", "ferry-sync.timer"] in cmds
    assert ["daemon-reload"] in cmds


def test_uninstall_units_tolerates_already_clean_state(
    fake_units_dir: Path,
    fake_systemctl: FakeRun,
    systemd_present: None,
) -> None:
    """Files already missing — should still succeed and tell the user."""
    result = CliRunner().invoke(app, ["uninstall-units"])
    assert result.exit_code == 0, result.output
    assert "nothing to remove" in result.output


def test_uninstall_units_works_without_systemd(
    fake_units_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    systemd_absent: None,
) -> None:
    """User switched distros, took units with them, removing leftover files
    must not require systemd to be back."""
    fake_units_dir.mkdir(parents=True)
    (fake_units_dir / SERVICE_FILENAME).write_text("stub")
    (fake_units_dir / TIMER_FILENAME).write_text("stub")

    fake = FakeRun()
    monkeypatch.setattr("ferry.cli.units.subprocess.run", fake)

    result = CliRunner().invoke(app, ["uninstall-units"])
    assert result.exit_code == 0, result.output
    assert not (fake_units_dir / SERVICE_FILENAME).exists()
    assert fake.calls == []  # no systemctl calls when systemd absent


def test_uninstall_units_lenient_on_disable_failure(
    fake_units_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    systemd_present: None,
) -> None:
    """`disable --now` may fail (timer never enabled, already disabled, etc.) —
    we should still proceed to remove the files and run daemon-reload."""
    fake_units_dir.mkdir(parents=True)
    (fake_units_dir / TIMER_FILENAME).write_text("stub")

    # Both `disable --now` AND `daemon-reload` go through this fake. Make
    # `disable` fail but `daemon-reload` succeed by checking args.
    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if "disable" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not loaded")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("ferry.cli.units.subprocess.run", _run)

    result = CliRunner().invoke(app, ["uninstall-units"])
    assert result.exit_code == 0, result.output
    assert not (fake_units_dir / TIMER_FILENAME).exists()


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def test_install_units_listed_in_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert "install-units" in result.output
    assert "uninstall-units" in result.output
