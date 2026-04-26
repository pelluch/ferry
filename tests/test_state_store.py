from pathlib import Path

import pytest

from ferry.adapters.sidecar import sidecar_path_for, write_sidecar
from ferry.adapters.state_store import (
    StateDecodeError,
    StateSchemaError,
    default_state_path,
    ensure_sidecars,
    load_state,
    recover_state_from_sidecars,
    save_state,
)
from ferry.domain.destination import Destination
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


# ---------------------------------------------------------------------------
# Sidecar recovery — rebuild state from on-disk sidecars
# ---------------------------------------------------------------------------


def test_recovery_returns_empty_state_when_no_sidecars(tmp_path: Path) -> None:
    state = recover_state_from_sidecars([tmp_path])
    assert state.roms == {}


def test_recovery_finds_sidecars_under_roots(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    a = roms_base / "gc" / "A.iso"
    b = roms_base / "snes" / "B.smc"
    write_sidecar(a, make_rom(rom_id=1, name="A"))
    write_sidecar(b, make_rom(rom_id=2, name="B"))

    recovered = recover_state_from_sidecars([roms_base])
    assert set(recovered.roms) == {1, 2}
    assert recovered.roms[1].name == "A"
    assert recovered.roms[2].name == "B"


def test_recovery_skips_malformed_sidecars(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    good = roms_base / "gc" / "Good.iso"
    write_sidecar(good, make_rom(rom_id=42))
    # Drop a corrupt sidecar nearby.
    bad_dir = roms_base / "gc"
    (bad_dir / "Bad.iso.ferry.json").write_text("{ not json")

    recovered = recover_state_from_sidecars([roms_base])
    assert set(recovered.roms) == {42}  # corrupt one skipped, not raised


def test_recovery_returns_empty_when_root_does_not_exist(tmp_path: Path) -> None:
    recovered = recover_state_from_sidecars([tmp_path / "nope"])
    assert recovered.roms == {}


# ---------------------------------------------------------------------------
# ensure_sidecars — regenerate missing sidecars from in-memory state
# ---------------------------------------------------------------------------


def _destination(tmp_path: Path) -> Destination:
    return Destination(roms_base=tmp_path / "ROMs", bios_base=None, preset="esde-native")


def test_ensure_sidecars_regenerates_missing(tmp_path: Path, make_rom) -> None:
    """User deletes only the sidecar; primary still on disk → sidecar restored."""
    dest = _destination(tmp_path)
    primary = dest.roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"data")
    rom = make_rom(rom_id=1)  # default outputs path is "gc/Pikmin.iso"
    state = LibraryState(roms={1: rom})

    # No sidecar yet.
    assert not sidecar_path_for(primary).exists()

    count = ensure_sidecars(state, dest)
    assert count == 1
    assert sidecar_path_for(primary).exists()


def test_ensure_sidecars_skips_existing(tmp_path: Path, make_rom) -> None:
    dest = _destination(tmp_path)
    primary = dest.roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"data")
    rom = make_rom(rom_id=1)
    write_sidecar(primary, rom)  # already there
    state = LibraryState(roms={1: rom})

    count = ensure_sidecars(state, dest)
    assert count == 0


def test_ensure_sidecars_skips_when_primary_missing(tmp_path: Path, make_rom) -> None:
    """If the primary file is gone too, the planner's redownload path handles it."""
    dest = _destination(tmp_path)
    rom = make_rom(rom_id=1)
    # No primary file on disk at all.
    state = LibraryState(roms={1: rom})

    count = ensure_sidecars(state, dest)
    assert count == 0
    # Sidecar deliberately not written — it would be at a phantom path.
    assert not sidecar_path_for(dest.roms_base / rom.primary_output.path).exists()
