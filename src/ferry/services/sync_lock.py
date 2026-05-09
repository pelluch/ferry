"""Single-process lock around `ferry sync` to prevent concurrent runs.

Two `ferry sync` invocations racing would step on each other's state.json,
sidecars, and trash moves. The timer-driven sync firing while the user is
running a manual sync is the realistic trigger; manual+manual is also
possible. This module provides a kernel-managed advisory lock that fails
fast (non-blocking) when a sync is already in progress.

Implementation: `fcntl.flock(LOCK_EX | LOCK_NB)` on a sentinel file at
`$XDG_STATE_HOME/ferry/sync.lock`. The lock is held by the open file
descriptor, not by the file itself — kernel releases it automatically when
the process exits for any reason (clean exit, SIGKILL, segfault, OOM kill,
power loss, hard reboot). No manual cleanup ever needed; the on-disk file
persists harmlessly. The PID written to the file is informational only.

Linux-only via `fcntl`. A future Windows port would swap this for
`msvcrt.locking` or the `filelock` PyPI package — same context-manager
contract, different backend.
"""

from __future__ import annotations

import fcntl
import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from ferry.domain.user_dirs import state_dir

logger = logging.getLogger(__name__)


class LockHeld(Exception):
    """Another ferry sync is currently holding the lock."""

    def __init__(self, pid: int, lock_path: Path) -> None:
        self.pid = pid
        self.lock_path = lock_path
        super().__init__(f"sync lock at {lock_path} is held by pid {pid}")


def default_lock_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the canonical sync.lock path."""
    return state_dir(env) / "ferry" / "sync.lock"


@contextmanager
def acquire_sync_lock(lock_path: Path) -> Iterator[None]:
    """Acquire the sync lock or raise LockHeld immediately.

    The lock file is created if missing. On acquisition, the holder's PID is
    written into the file (informational — the actual mutual exclusion is
    the kernel-held flock, which survives stale PID files and crashes).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            held_pid = _read_pid(lock_path)
            raise LockHeld(held_pid, lock_path) from e

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        try:
            yield
        finally:
            # flock auto-releases on close; explicit unlock is belt-and-braces
            # but harmless and makes the release point obvious in tracebacks.
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_pid(lock_path: Path) -> int:
    """Best-effort read of the PID from a held lock file. Returns -1 on failure."""
    try:
        text = lock_path.read_text().strip()
    except OSError:
        return -1
    try:
        return int(text)
    except ValueError:
        return -1
