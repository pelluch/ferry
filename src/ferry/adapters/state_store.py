"""Atomic persistence of the library state file.

The state file lives at $XDG_STATE_HOME/ferry/state.json (default
~/.local/state/ferry/state.json). Reads return a fresh empty state if the
file doesn't exist; writes are atomic (temp + fsync + rename).

Recovery from a missing state.json is the `ferry reconcile` flow: walk
the on-disk ROM tree, classify each file against RomM via name + md5,
and adopt confident matches back into state.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from ferry.domain.state import (
    LibraryState,
    StateDecodeError,
    StateSchemaError,
    from_json,
    to_json,
)

logger = logging.getLogger(__name__)

__all__ = [
    "StateDecodeError",
    "StateSchemaError",
    "default_state_path",
    "load_state",
    "save_state",
]


def default_state_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the canonical state.json path, honoring XDG_STATE_HOME."""
    env = env if env is not None else os.environ
    base = env.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "ferry" / "state.json"


def load_state(path: Path | None = None, env: Mapping[str, str] | None = None) -> LibraryState:
    """Read state.json, or return a fresh empty state if the file doesn't exist.

    Raises:
        StateSchemaError: file uses a schema_version newer than this ferry knows.
        StateDecodeError: file is corrupt / malformed.
    """
    target = path if path is not None else default_state_path(env)
    if not target.exists():
        return LibraryState()
    text = target.read_text()
    return from_json(text)


def save_state(
    state: LibraryState,
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Write *state* to disk atomically.

    Writes to a sibling tempfile, fsyncs, then renames over the target so
    a crash mid-write can never produce a half-written state.json.
    """
    target = path if path is not None else default_state_path(env)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = to_json(state)
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(target)
