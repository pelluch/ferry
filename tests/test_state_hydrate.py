"""Tests for `services.state_hydrate.hydrate_romm_md5`."""

from __future__ import annotations

import zipfile
from pathlib import Path

from ferry.domain.state import LibraryState, RomState, TransformedOutput
from ferry.services.state_hydrate import hydrate_romm_md5


def _make_rom(
    rom_id: int,
    *,
    output_path: str,
    output_md5: str = "0" * 32,
    source_romm_md5: str | None = None,
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug="gc",
        name=f"Rom-{rom_id}",
        source_filename=Path(output_path).name,
        source_md5="0" * 32,
        source_size=1024,
        source_updated_at="2026-04-25T12:00:00Z",
        source_romm_md5=source_romm_md5,
        transforms=(),
        outputs=(TransformedOutput(path=output_path, md5=output_md5, size=1024),),
        primary_output_index=0,
        synced_at="2026-04-25T12:01:00Z",
    )


def _plant(roms_base: Path, rel_path: str, content: bytes) -> Path:
    target = roms_base / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _plant_zip(roms_base: Path, rel_path: str, members: dict[str, bytes]) -> Path:
    target = roms_base / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return target


def test_hydrate_no_op_when_all_entries_have_romm_md5(tmp_path: Path) -> None:
    state = LibraryState(
        roms={
            1: _make_rom(1, output_path="gc/A.iso", source_romm_md5="a" * 32),
            2: _make_rom(2, output_path="gc/B.iso", source_romm_md5="b" * 32),
        }
    )
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 0
    assert new_state is state  # identity preserved on no-op


def test_hydrate_populates_missing_romm_md5_for_non_archive(tmp_path: Path) -> None:
    """Single-file ROM (not an archive): md5 of the file's bytes."""
    _plant(tmp_path, "gc/Game.iso", b"raw rom bytes")
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Game.iso", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    import hashlib

    assert new_state.roms[1].source_romm_md5 == hashlib.md5(b"raw rom bytes").hexdigest()


def test_hydrate_populates_missing_romm_md5_for_zip_archive(tmp_path: Path) -> None:
    """Zip ROM: md5 of the largest inner file's bytes (RomM's algorithm)."""
    _plant_zip(tmp_path, "gc/Game.zip", {"Game.iso": b"largest inner file content"})
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Game.zip", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    import hashlib

    assert (
        new_state.roms[1].source_romm_md5 == hashlib.md5(b"largest inner file content").hexdigest()
    )


def test_hydrate_skips_entries_with_missing_primary_output(tmp_path: Path) -> None:
    """File deleted from disk → entry stays unhydrated. The
    `_primary_missing` check in compute_plan handles re-download."""
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Missing.iso", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 0
    assert new_state is state
    assert new_state.roms[1].source_romm_md5 is None  # unchanged


def test_hydrate_preserves_existing_md5_when_mixing_with_new(tmp_path: Path) -> None:
    """Mixed state: some entries already hydrated, some not. Only the
    None-valued ones get backfilled; populated ones aren't re-hashed."""
    _plant(tmp_path, "gc/A.iso", b"a-bytes")
    _plant(tmp_path, "gc/B.iso", b"b-bytes")
    state = LibraryState(
        roms={
            1: _make_rom(1, output_path="gc/A.iso", source_romm_md5="preserved" * 4),
            2: _make_rom(2, output_path="gc/B.iso", source_romm_md5=None),
        }
    )
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    assert new_state.roms[1].source_romm_md5 == "preserved" * 4
    import hashlib

    assert new_state.roms[2].source_romm_md5 == hashlib.md5(b"b-bytes").hexdigest()


def test_hydrate_treats_empty_string_as_missing(tmp_path: Path) -> None:
    """Empty string is normalized to None at decode time; defensive
    check at hydration in case some other path inserts `""`."""
    _plant(tmp_path, "gc/A.iso", b"a-bytes")
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/A.iso", source_romm_md5="")})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    assert new_state.roms[1].source_romm_md5  # populated


def test_hydrate_handles_corrupt_archive_gracefully(tmp_path: Path) -> None:
    """A `.zip` that isn't a valid zip file → `hash_orphan_file` falls
    back to direct md5 (RomM's behavior). Hydration uses whatever it
    returns — no exception escapes."""
    _plant(tmp_path, "gc/Bogus.zip", b"not a real zip")
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Bogus.zip", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    # Whatever was computed is fine; the property under test is "no crash."
    assert new_state.roms[1].source_romm_md5 is not None


def test_hydrate_does_not_mutate_input_state(tmp_path: Path) -> None:
    """Returns a new LibraryState; original is left intact."""
    _plant(tmp_path, "gc/A.iso", b"a-bytes")
    original = LibraryState(roms={1: _make_rom(1, output_path="gc/A.iso", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(original, tmp_path)
    assert hydrated == 1
    assert original.roms[1].source_romm_md5 is None  # untouched
    assert new_state.roms[1].source_romm_md5 is not None
