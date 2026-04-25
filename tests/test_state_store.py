from pathlib import Path

import pytest

from ferry.adapters.state_store import (
    StateDecodeError,
    StateSchemaError,
    default_state_path,
    load_state,
    save_state,
)
from ferry.domain.state import CURRENT_SCHEMA_VERSION, LibraryState

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_default_path_uses_xdg_state_home(tmp_path: Path) -> None:
    p = default_state_path(env={"XDG_STATE_HOME": str(tmp_path)})
    assert p == tmp_path / "ferry" / "state.json"


def test_default_path_falls_back_to_local_state() -> None:
    p = default_state_path(env={})
    assert p == Path.home() / ".local" / "state" / "ferry" / "state.json"


# ---------------------------------------------------------------------------
# Load behavior
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty_state(tmp_path: Path) -> None:
    state = load_state(tmp_path / "nope.json")
    assert state == LibraryState()
    assert state.schema_version == CURRENT_SCHEMA_VERSION
    assert state.roms == {}


def test_corrupt_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "state.json"
    bad.write_text("{ not json")
    with pytest.raises(StateDecodeError):
        load_state(bad)


def test_future_schema_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "state.json"
    bad.write_text(f'{{"schema_version": {CURRENT_SCHEMA_VERSION + 1}, "roms": {{}}}}')
    with pytest.raises(StateSchemaError):
        load_state(bad)


# ---------------------------------------------------------------------------
# Save / load roundtrip
# ---------------------------------------------------------------------------


def test_save_then_load_roundtrips(tmp_path: Path, make_rom) -> None:
    target = tmp_path / "state.json"
    state = LibraryState(
        last_updated_after="2026-04-25T12:00:00Z",
        roms={42: make_rom(42)},
    )
    save_state(state, target)
    assert target.exists()
    assert load_state(target) == state


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nest" / "state.json"
    save_state(LibraryState(), target)
    assert target.exists()


def test_save_is_atomic_no_lingering_tmp(tmp_path: Path, make_rom) -> None:
    target = tmp_path / "state.json"
    save_state(LibraryState(roms={1: make_rom(1)}), target)
    # No `.tmp` file should remain after a clean save.
    assert not (tmp_path / "state.json.tmp").exists()
    # Only the canonical file is in the directory.
    assert {p.name for p in tmp_path.iterdir()} == {"state.json"}


def test_save_overwrites_previous_state(tmp_path: Path, make_rom) -> None:
    target = tmp_path / "state.json"
    save_state(LibraryState(roms={1: make_rom(1)}), target)
    save_state(LibraryState(roms={2: make_rom(2)}), target)
    loaded = load_state(target)
    assert set(loaded.roms) == {2}
