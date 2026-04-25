from pathlib import Path

import pytest

from ferry.adapters.sidecar import (
    SIDECAR_SUFFIX,
    find_sidecars,
    read_sidecar,
    sidecar_path_for,
    write_sidecar,
)
from ferry.domain.state import StateDecodeError


def test_sidecar_path_appends_suffix(tmp_path: Path) -> None:
    primary = tmp_path / "gc" / "Pikmin.iso"
    assert sidecar_path_for(primary) == tmp_path / "gc" / f"Pikmin.iso{SIDECAR_SUFFIX}"


def test_write_creates_parent_dirs_and_returns_path(tmp_path: Path, make_rom) -> None:
    primary = tmp_path / "gc" / "Pikmin.iso"
    written = write_sidecar(primary, make_rom())
    assert written == sidecar_path_for(primary)
    assert written.exists()


def test_read_returns_none_for_missing_sidecar(tmp_path: Path) -> None:
    primary = tmp_path / "gc" / "nothing.iso"
    primary.parent.mkdir(parents=True)
    assert read_sidecar(primary) is None


def test_write_then_read_roundtrips(tmp_path: Path, make_rom) -> None:
    primary = tmp_path / "gc" / "Pikmin.iso"
    rom = make_rom()
    write_sidecar(primary, rom)
    assert read_sidecar(primary) == rom


def test_write_is_atomic_no_lingering_tmp(tmp_path: Path, make_rom) -> None:
    primary = tmp_path / "gc" / "Pikmin.iso"
    write_sidecar(primary, make_rom())
    sidecar = sidecar_path_for(primary)
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    assert not tmp.exists()


def test_read_corrupt_sidecar_raises(tmp_path: Path) -> None:
    primary = tmp_path / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    sidecar_path_for(primary).write_text("{ not json")
    with pytest.raises(StateDecodeError):
        read_sidecar(primary)


def test_multi_output_sidecar_lists_all_files(tmp_path: Path, make_rom, make_output) -> None:
    """For multi-disc ROMs, the primary's sidecar carries all output paths."""
    primary = tmp_path / "psx" / "Game.m3u"
    rom = make_rom(
        outputs=(
            make_output("psx/CD1.cue"),
            make_output("psx/CD1.bin"),
            make_output("psx/Game.m3u"),
        ),
        primary_output_index=2,
    )
    write_sidecar(primary, rom)
    decoded = read_sidecar(primary)
    assert decoded is not None
    assert {o.path for o in decoded.outputs} == {
        "psx/CD1.cue",
        "psx/CD1.bin",
        "psx/Game.m3u",
    }


# ---------------------------------------------------------------------------
# find_sidecars — directory walking for state recovery
# ---------------------------------------------------------------------------


def test_find_sidecars_walks_recursively(tmp_path: Path, make_rom) -> None:
    a = tmp_path / "gc" / "Pikmin.iso"
    b = tmp_path / "ps2" / "GoW.iso"
    write_sidecar(a, make_rom(rom_id=1))
    write_sidecar(b, make_rom(rom_id=2))
    found = find_sidecars([tmp_path])
    assert found == [
        sidecar_path_for(a),
        sidecar_path_for(b),
    ]


def test_find_sidecars_returns_empty_when_root_missing(tmp_path: Path) -> None:
    assert find_sidecars([tmp_path / "nope"]) == []


def test_find_sidecars_ignores_non_sidecar_files(tmp_path: Path, make_rom) -> None:
    primary = tmp_path / "gc" / "Pikmin.iso"
    write_sidecar(primary, make_rom())
    # Drop unrelated junk into the same tree.
    (tmp_path / "gc" / "notes.txt").write_text("hi")
    (tmp_path / "gc" / "Pikmin.iso").touch()  # the actual ROM stub
    found = find_sidecars([tmp_path])
    assert found == [sidecar_path_for(primary)]


def test_find_sidecars_skips_files_with_sidecar_in_middle(tmp_path: Path, make_rom) -> None:
    """Only files ending in `.ferry.json` count; not e.g. `Game.ferry.json.bak`."""
    primary = tmp_path / "gc" / "Pikmin.iso"
    write_sidecar(primary, make_rom())
    (tmp_path / "gc" / "decoy.ferry.json.bak").write_text("{}")
    found = find_sidecars([tmp_path])
    assert found == [sidecar_path_for(primary)]
