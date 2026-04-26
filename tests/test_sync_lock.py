"""Tests for `ferry.services.sync_lock`.

flock is per-open-file-description even within a single process on Linux,
so the contention test can stay in-process without a subprocess dance.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ferry.services.sync_lock import (
    LockHeld,
    acquire_sync_lock,
    default_lock_path,
)

# ---------------------------------------------------------------------------
# default_lock_path
# ---------------------------------------------------------------------------


def test_default_lock_path_uses_xdg_state_home(tmp_path: Path) -> None:
    p = default_lock_path(env={"XDG_STATE_HOME": str(tmp_path)})
    assert p == tmp_path / "ferry" / "sync.lock"


def test_default_lock_path_falls_back_to_home_local_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = default_lock_path(env={})
    assert p == tmp_path / ".local" / "state" / "ferry" / "sync.lock"


# ---------------------------------------------------------------------------
# acquire / release
# ---------------------------------------------------------------------------


def test_acquire_creates_parent_dir(tmp_path: Path) -> None:
    lock = tmp_path / "deeply" / "nested" / "sync.lock"
    with acquire_sync_lock(lock):
        pass
    assert lock.exists()


def test_acquire_writes_pid(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    with acquire_sync_lock(lock):
        assert lock.read_text().strip() == str(os.getpid())


def test_lock_is_re_acquirable_after_release(tmp_path: Path) -> None:
    """Two sequential syncs (clean exit between) — both should succeed."""
    lock = tmp_path / "sync.lock"
    with acquire_sync_lock(lock):
        pass
    with acquire_sync_lock(lock):
        pass  # would have raised LockHeld if release didn't work


def test_acquire_truncates_stale_pid(tmp_path: Path) -> None:
    """A leftover PID from a crashed prior run gets overwritten on re-acquire."""
    lock = tmp_path / "sync.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999\n")  # stale, kernel doesn't care
    with acquire_sync_lock(lock):
        assert lock.read_text().strip() == str(os.getpid())


# ---------------------------------------------------------------------------
# Contention
# ---------------------------------------------------------------------------


def test_concurrent_acquire_raises_lock_held(tmp_path: Path) -> None:
    """Second acquire while first is still held — LockHeld with first's PID."""
    lock = tmp_path / "sync.lock"
    with acquire_sync_lock(lock):
        with pytest.raises(LockHeld) as exc_info, acquire_sync_lock(lock):
            pytest.fail("second acquire should have raised")
        assert exc_info.value.pid == os.getpid()
        assert exc_info.value.lock_path == lock


def test_lock_held_carries_lock_path(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    with acquire_sync_lock(lock):
        with pytest.raises(LockHeld) as exc_info, acquire_sync_lock(lock):
            pass
        assert exc_info.value.lock_path == lock


def test_lock_held_returns_minus_one_for_unreadable_pid(tmp_path: Path) -> None:
    """If the PID file is empty or garbled, surface -1 rather than crash."""
    import fcntl

    lock = tmp_path / "sync.lock"
    # tmp_path already exists; lock.parent is tmp_path here.
    # Take the lock at a low level *without* writing a PID into the file.
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # File is empty — _read_pid returns -1.
        with pytest.raises(LockHeld) as exc_info, acquire_sync_lock(lock):
            pass
        assert exc_info.value.pid == -1
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# Crash semantics: lock survives in name only, not in effect
# ---------------------------------------------------------------------------


def test_stale_lock_file_does_not_block_acquisition(tmp_path: Path) -> None:
    """A pre-existing lock *file* with a fake PID is no obstacle — what
    matters is whether any kernel process holds the flock on it. Simulates
    the post-crash / post-reboot case."""
    lock = tmp_path / "sync.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("12345\n")
    with acquire_sync_lock(lock):
        assert lock.read_text().strip() == str(os.getpid())
