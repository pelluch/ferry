"""Atomic persistence of the library state file.

The state file lives at $XDG_STATE_HOME/ferry/state.json (default
~/.local/state/ferry/state.json). Reads return a fresh empty state if the
file doesn't exist; writes are atomic (temp + fsync + rename).

When state.json is missing or empty but the user still has ROM files +
sidecars on disk (DESIGN.md §5.5), `recover_state_from_sidecars` rebuilds
state by walking the sidecar tree — that's the recovery path for "user
deleted state.json but kept everything else." Sidecars live under
`$XDG_STATE_HOME/ferry/sidecars/` (post v8 ck4); the recovery walker
also picks up legacy next-to-rom sidecars from earlier ferry versions.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from ferry.adapters.sidecar import find_sidecars, sidecar_path_for, write_sidecar
from ferry.domain.destination import Destination
from ferry.domain.state import (
    LibraryState,
    StateDecodeError,
    StateSchemaError,
    from_json,
    rom_from_json,
    to_json,
)

logger = logging.getLogger(__name__)

__all__ = [
    "StateDecodeError",
    "StateSchemaError",
    "default_state_path",
    "ensure_sidecars",
    "load_state",
    "recover_state_from_sidecars",
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


def ensure_sidecars(state: LibraryState, destination: Destination) -> int:
    """Regenerate missing sidecars for ROMs already in *state*.

    For each ROM in state, if the primary output still exists on disk but
    its sidecar is missing, rewrite the sidecar from the in-memory RomState.
    No download required — sidecar contents come straight from state. ROMs
    whose primary is also missing are skipped (the planner's missing-on-disk
    check picks them up and re-syncs the whole thing, which writes both the
    file and a fresh sidecar).

    Returns the number of sidecars regenerated.
    """
    regenerated = 0
    for rom in state.roms.values():
        primary_abs = destination.roms_base / rom.primary_output.path
        if not primary_abs.exists():
            continue
        if sidecar_path_for(primary_abs, roms_base=destination.roms_base).exists():
            continue
        write_sidecar(primary_abs, rom, roms_base=destination.roms_base)
        regenerated += 1
    return regenerated


def recover_state_from_sidecars(roms_base: Path) -> LibraryState:
    """Walk for `*.ferry.json` sidecars under both the canonical sidecars
    root and `roms_base` (legacy fallback), and rebuild a LibraryState.

    Sidecars carry the same RomState as state.json's entries (DESIGN.md
    §5.5), so recovery is a straight read of each sidecar. Sidecars that
    fail to parse are logged and skipped — recovery is best-effort, not
    all-or-nothing.

    Returns an empty LibraryState when no sidecars are found, so callers
    can treat the "nothing to recover" case the same as a fresh first run.
    """
    recovered: dict[int, object] = {}
    for sidecar_path in find_sidecars(roms_base=roms_base):
        try:
            rom = rom_from_json(sidecar_path.read_text())
        except StateDecodeError as e:
            logger.warning("ignoring malformed sidecar %s: %s", sidecar_path, e)
            continue
        if rom.rom_id in recovered:
            logger.warning("duplicate sidecar for rom_id %d (using first found)", rom.rom_id)
            continue
        recovered[rom.rom_id] = rom
    return LibraryState(roms=dict(recovered))  # type: ignore[arg-type]
