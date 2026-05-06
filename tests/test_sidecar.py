"""Tests for the sidecar adapter — canonical layout under sidecars_root,
legacy fallbacks (v1 plain + v2 dot-prefixed next-to-rom), migration
sweep, and find/read/write semantics across all three locations."""

from pathlib import Path

import pytest

from ferry.adapters.sidecar import (
    SIDECAR_PREFIX,
    SIDECAR_SUFFIX,
    default_sidecars_root,
    find_sidecars,
    legacy_sidecar_paths_for,
    migrate_legacy_sidecars,
    read_sidecar,
    sidecar_path_for,
    write_sidecar,
)
from ferry.domain.state import StateDecodeError, rom_to_json

# ---------------------------------------------------------------------------
# Path resolvers
# ---------------------------------------------------------------------------


def test_canonical_sidecar_lives_under_sidecars_root(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    assert sidecar_path_for(primary, roms_base=roms_base, sidecars_root=sidecars_root) == (
        sidecars_root / "gc" / f"Pikmin.iso{SIDECAR_SUFFIX}"
    )


def test_canonical_sidecar_uses_default_root_when_unspecified(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    primary = roms_base / "gba" / "Game.gba"
    expected = default_sidecars_root() / "gba" / f"Game.gba{SIDECAR_SUFFIX}"
    assert sidecar_path_for(primary, roms_base=roms_base) == expected


def test_canonical_path_requires_primary_under_roms_base(tmp_path: Path) -> None:
    primary = tmp_path / "elsewhere" / "rom.bin"
    with pytest.raises(ValueError):
        sidecar_path_for(primary, roms_base=tmp_path / "ROMs", sidecars_root=tmp_path / "s")


def test_legacy_sidecar_paths_returns_dot_then_plain(tmp_path: Path) -> None:
    primary = tmp_path / "gc" / "Pikmin.iso"
    dot, plain = legacy_sidecar_paths_for(primary)
    assert dot == primary.with_name(SIDECAR_PREFIX + primary.name + SIDECAR_SUFFIX)
    assert plain == primary.with_name(primary.name + SIDECAR_SUFFIX)


# ---------------------------------------------------------------------------
# Default sidecars root respects XDG_STATE_HOME
# ---------------------------------------------------------------------------


def test_default_sidecars_root_uses_xdg_state_home(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert default_sidecars_root({"XDG_STATE_HOME": str(state)}) == state / "ferry" / "sidecars"


def test_default_sidecars_root_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path("/tmp/fakehome")
    monkeypatch.setattr("ferry.adapters.sidecar.Path.home", lambda: home)
    assert default_sidecars_root({}) == home / ".local" / "state" / "ferry" / "sidecars"


# ---------------------------------------------------------------------------
# write_sidecar
# ---------------------------------------------------------------------------


def test_write_creates_parent_dirs_and_returns_path(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    written = write_sidecar(primary, make_rom(), roms_base=roms_base, sidecars_root=sidecars_root)
    assert written == sidecars_root / "gc" / f"Pikmin.iso{SIDECAR_SUFFIX}"
    assert written.exists()


def test_write_is_atomic_no_lingering_tmp(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    written = write_sidecar(primary, make_rom(), roms_base=roms_base, sidecars_root=sidecars_root)
    tmp = written.with_name(written.name + ".tmp")
    assert not tmp.exists()


def test_write_removes_legacy_dot_sidecar(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    dot, _plain = legacy_sidecar_paths_for(primary)
    dot.write_text(rom_to_json(make_rom(rom_id=99)))

    write_sidecar(primary, make_rom(rom_id=1), roms_base=roms_base, sidecars_root=sidecars_root)
    assert not dot.exists()  # migrated to canonical


def test_write_removes_legacy_plain_sidecar(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    _dot, plain = legacy_sidecar_paths_for(primary)
    plain.write_text(rom_to_json(make_rom(rom_id=99)))

    write_sidecar(primary, make_rom(rom_id=1), roms_base=roms_base, sidecars_root=sidecars_root)
    assert not plain.exists()


def test_write_does_not_pollute_rom_tree(tmp_path: Path, make_rom) -> None:
    """Sidecar must not appear next to the ROM under the new layout."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)

    write_sidecar(primary, make_rom(), roms_base=roms_base, sidecars_root=sidecars_root)

    assert list(primary.parent.iterdir()) == []  # nothing alongside the ROM


# ---------------------------------------------------------------------------
# read_sidecar
# ---------------------------------------------------------------------------


def test_read_returns_none_for_missing_sidecar(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    primary = roms_base / "gc" / "nothing.iso"
    primary.parent.mkdir(parents=True)
    assert read_sidecar(primary, roms_base=roms_base) is None


def test_write_then_read_roundtrips(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    rom = make_rom()
    write_sidecar(primary, rom, roms_base=roms_base, sidecars_root=sidecars_root)
    assert read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root) == rom


def test_read_corrupt_sidecar_raises(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    canonical = sidecar_path_for(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{ not json")
    with pytest.raises(StateDecodeError):
        read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root)


def test_read_falls_back_to_dot_legacy(tmp_path: Path, make_rom) -> None:
    """Pre-relocation v2 sidecars (next-to-rom, dot-prefixed) still read."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    dot, _plain = legacy_sidecar_paths_for(primary)
    rom = make_rom()
    dot.write_text(rom_to_json(rom))

    assert read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root) == rom


def test_read_falls_back_to_plain_legacy(tmp_path: Path, make_rom) -> None:
    """Pre-relocation v1 sidecars (next-to-rom, no dot) still read."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    _dot, plain = legacy_sidecar_paths_for(primary)
    rom = make_rom()
    plain.write_text(rom_to_json(rom))

    assert read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root) == rom


def test_canonical_takes_precedence_over_legacy(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    canonical_rom = make_rom(rom_id=1, name="Canonical")
    legacy_rom = make_rom(rom_id=2, name="Legacy")
    canonical = sidecar_path_for(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    canonical.parent.mkdir(parents=True)
    canonical.write_text(rom_to_json(canonical_rom))
    dot, _plain = legacy_sidecar_paths_for(primary)
    dot.write_text(rom_to_json(legacy_rom))

    result = read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    assert result is not None
    assert result.rom_id == 1


def test_dot_legacy_takes_precedence_over_plain_legacy(tmp_path: Path, make_rom) -> None:
    """Two legacies on disk → dot (v2) wins over plain (v1)."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    dot_rom = make_rom(rom_id=1, name="V2")
    plain_rom = make_rom(rom_id=2, name="V1")
    dot, plain = legacy_sidecar_paths_for(primary)
    dot.write_text(rom_to_json(dot_rom))
    plain.write_text(rom_to_json(plain_rom))

    result = read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    assert result is not None
    assert result.rom_id == 1


def test_read_with_no_roms_base_only_checks_legacy(tmp_path: Path, make_rom) -> None:
    """Launch-hook flow without [destination] configured can still read
    legacy next-to-rom sidecars; canonical lookup is skipped."""
    primary = tmp_path / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    dot, _plain = legacy_sidecar_paths_for(primary)
    rom = make_rom()
    dot.write_text(rom_to_json(rom))

    assert read_sidecar(primary, roms_base=None) == rom


def test_multi_output_sidecar_lists_all_files(tmp_path: Path, make_rom, make_output) -> None:
    """For multi-disc ROMs, the primary's sidecar carries all output paths."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "psx" / "Game.m3u"
    rom = make_rom(
        outputs=(
            make_output("psx/CD1.cue"),
            make_output("psx/CD1.bin"),
            make_output("psx/Game.m3u"),
        ),
        primary_output_index=2,
    )
    write_sidecar(primary, rom, roms_base=roms_base, sidecars_root=sidecars_root)
    decoded = read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    assert decoded is not None
    assert {o.path for o in decoded.outputs} == {
        "psx/CD1.cue",
        "psx/CD1.bin",
        "psx/Game.m3u",
    }


# ---------------------------------------------------------------------------
# find_sidecars — recovery walker
# ---------------------------------------------------------------------------


def test_find_walks_canonical_and_legacy(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    a = roms_base / "gc" / "Canon.iso"
    b = roms_base / "ps2" / "Legacy.iso"
    write_sidecar(a, make_rom(rom_id=1), roms_base=roms_base, sidecars_root=sidecars_root)
    # `b` left at v2 legacy location.
    b.parent.mkdir(parents=True)
    dot, _plain = legacy_sidecar_paths_for(b)
    dot.write_text(rom_to_json(make_rom(rom_id=2)))

    found = find_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)
    canonical = sidecar_path_for(a, roms_base=roms_base, sidecars_root=sidecars_root)
    assert canonical in found
    assert dot in found
    assert len(found) == 2


def test_find_returns_empty_when_neither_root_exists(tmp_path: Path) -> None:
    assert (
        find_sidecars(
            roms_base=tmp_path / "no-rom-tree",
            sidecars_root=tmp_path / "no-sidecar-tree",
        )
        == []
    )


def test_find_ignores_non_sidecar_files(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    write_sidecar(primary, make_rom(), roms_base=roms_base, sidecars_root=sidecars_root)
    (roms_base / "gc" / "notes.txt").write_text("hi")
    primary.touch()  # the ROM stub itself

    found = find_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)
    assert len(found) == 1


# ---------------------------------------------------------------------------
# migrate_legacy_sidecars — one-shot sweep
# ---------------------------------------------------------------------------


def test_migrate_promotes_dot_legacy_to_canonical(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    dot, _plain = legacy_sidecar_paths_for(primary)
    rom = make_rom(rom_id=42)
    dot.write_text(rom_to_json(rom))

    migrated = migrate_legacy_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)
    assert migrated == 1
    assert not dot.exists()
    canonical = sidecar_path_for(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    assert canonical.exists()
    decoded = read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    assert decoded is not None
    assert decoded.rom_id == 42


def test_migrate_promotes_plain_legacy_too(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    _dot, plain = legacy_sidecar_paths_for(primary)
    plain.write_text(rom_to_json(make_rom(rom_id=42)))

    migrated = migrate_legacy_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)
    assert migrated == 1
    assert not plain.exists()


def test_migrate_is_idempotent(tmp_path: Path, make_rom) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    dot, _plain = legacy_sidecar_paths_for(primary)
    dot.write_text(rom_to_json(make_rom()))

    first = migrate_legacy_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)
    second = migrate_legacy_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)
    assert first == 1
    assert second == 0


def test_migrate_drops_legacy_when_canonical_already_exists(tmp_path: Path, make_rom) -> None:
    """Canonical wins on collision; legacy is just removed."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    canonical_rom = make_rom(rom_id=1, name="Canonical")
    legacy_rom = make_rom(rom_id=2, name="Legacy")
    write_sidecar(primary, canonical_rom, roms_base=roms_base, sidecars_root=sidecars_root)
    dot, _plain = legacy_sidecar_paths_for(primary)
    dot.write_text(rom_to_json(legacy_rom))

    migrate_legacy_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)

    assert not dot.exists()
    decoded = read_sidecar(primary, roms_base=roms_base, sidecars_root=sidecars_root)
    assert decoded is not None
    assert decoded.rom_id == 1  # canonical preserved


def test_migrate_skips_malformed_legacy(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gc" / "Junk.iso"
    primary.parent.mkdir(parents=True)
    dot, _plain = legacy_sidecar_paths_for(primary)
    dot.write_text("{ not json")

    migrated = migrate_legacy_sidecars(roms_base=roms_base, sidecars_root=sidecars_root)
    assert migrated == 0
    assert dot.exists()  # left alone for the user to inspect


def test_migrate_returns_zero_when_roms_base_missing(tmp_path: Path) -> None:
    assert (
        migrate_legacy_sidecars(
            roms_base=tmp_path / "no-rom-tree", sidecars_root=tmp_path / "sidecars"
        )
        == 0
    )
