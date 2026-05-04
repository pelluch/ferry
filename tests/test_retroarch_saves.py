"""Tests for the layout-aware retroarch_saves walker."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ferry.adapters.retroarch_paths import RetroArchInstall
from ferry.adapters.retroarch_saves import list_local_saves
from ferry.domain.state import RomState, TransformedOutput


def _make_rom(
    rom_id: int,
    *,
    source_filename: str,
    output_path: str | None = None,
    platform: str = "snes",
) -> RomState:
    output = TransformedOutput(
        path=output_path or f"{platform}/{source_filename}",
        md5="0" * 32,
        size=1024,
    )
    return RomState(
        rom_id=rom_id,
        platform_slug=platform,
        name=Path(source_filename).stem,
        source_filename=source_filename,
        source_md5="a" * 32,
        source_size=2048,
        source_updated_at="2026-04-01T00:00:00Z",
        transforms=(),
        outputs=(output,),
        primary_output_index=0,
        synced_at="2026-04-01T00:00:01Z",
    )


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _install(
    saves_dir: Path,
    *,
    sort_by_core: bool = False,
    sort_by_content: bool = False,
) -> RetroArchInstall:
    return RetroArchInstall(
        source="native",
        cfg_path=saves_dir.parent / "retroarch.cfg",
        config_root=saves_dir.parent,
        savefile_directory=saves_dir,
        sort_savefiles_enable=sort_by_core,
        sort_savefiles_by_content_enable=sort_by_content,
        has_saves=True,
    )


# ---------------------------------------------------------------------------
# Layout: by-core only (sort_savefiles_enable=true)
# ---------------------------------------------------------------------------


def test_core_subdir_yields_retroarch_dash_core(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    (saves / "snes9x").mkdir(parents=True)
    save = saves / "snes9x" / "Super Mario World.srm"
    save.write_bytes(b"battery")
    rom = _make_rom(11, source_filename="Super Mario World.zip")

    matched, warnings = list_local_saves(_install(saves, sort_by_core=True), [rom])
    assert warnings == []
    assert len(matched) == 1
    s = matched[0]
    assert s.rom_id == 11
    assert s.emulator == "retroarch-snes9x"
    assert s.slot == "default"
    assert s.local_md5 == _md5(b"battery")


# ---------------------------------------------------------------------------
# Layout: by-content only (sort_savefiles_by_content_enable=true) — RetroDECK
# ---------------------------------------------------------------------------


def test_content_subdir_yields_plain_retroarch(tmp_path: Path) -> None:
    """When path tells us platform but not core, we fall back to plain `retroarch`."""
    saves = tmp_path / "saves"
    (saves / "snes").mkdir(parents=True)
    (saves / "snes" / "Mario.srm").write_bytes(b"x")
    rom = _make_rom(1, source_filename="Mario.zip")

    matched, warnings = list_local_saves(_install(saves, sort_by_content=True), [rom])
    assert warnings == []
    assert matched[0].emulator == "retroarch"


# ---------------------------------------------------------------------------
# Layout: both by-core AND by-content
# ---------------------------------------------------------------------------


def test_content_then_core_subdirs(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    (saves / "snes" / "snes9x").mkdir(parents=True)
    (saves / "snes" / "snes9x" / "Mario.srm").write_bytes(b"x")
    rom = _make_rom(1, source_filename="Mario.zip")

    matched, warnings = list_local_saves(
        _install(saves, sort_by_core=True, sort_by_content=True),
        [rom],
    )
    assert warnings == []
    assert matched[0].emulator == "retroarch-snes9x"


# ---------------------------------------------------------------------------
# Layout: flat (neither flag)
# ---------------------------------------------------------------------------


def test_flat_layout_yields_plain_retroarch(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "Mario.srm").write_bytes(b"x")
    rom = _make_rom(1, source_filename="Mario.zip")

    matched, warnings = list_local_saves(_install(saves), [rom])
    assert warnings == []
    assert matched[0].emulator == "retroarch"


# ---------------------------------------------------------------------------
# Stem matching
# ---------------------------------------------------------------------------


def test_match_via_source_filename_stem(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    (saves / "snes9x").mkdir(parents=True)
    (saves / "snes9x" / "Sonic & Knuckles (USA).srm").write_bytes(b"x")
    rom = _make_rom(7, source_filename="Sonic & Knuckles (USA).zip")
    matched, warnings = list_local_saves(_install(saves, sort_by_core=True), [rom])
    assert warnings == []
    assert matched[0].rom_id == 7


def test_match_via_transformed_output_stem(tmp_path: Path) -> None:
    """When unzip transform ran, save uses extracted .iso stem, not .zip."""
    saves = tmp_path / "saves"
    (saves / "dolphin").mkdir(parents=True)
    (saves / "dolphin" / "Pikmin (USA).srm").write_bytes(b"x")
    rom = RomState(
        rom_id=99,
        platform_slug="gc",
        name="Pikmin",
        source_filename="Pikmin (USA).zip",
        source_md5="a" * 32,
        source_size=2048,
        source_updated_at="2026-04-01T00:00:00Z",
        transforms=("unzip",),
        outputs=(TransformedOutput(path="gc/Pikmin (USA).iso", md5="b" * 32, size=4096),),
        primary_output_index=0,
        synced_at="2026-04-01T00:00:01Z",
    )
    matched, warnings = list_local_saves(_install(saves, sort_by_core=True), [rom])
    assert warnings == []
    assert matched[0].rom_id == 99


# ---------------------------------------------------------------------------
# Unmatched saves: warn, don't abort
# ---------------------------------------------------------------------------


def test_unmatched_save_produces_warning(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    (saves / "snes9x").mkdir(parents=True)
    (saves / "snes9x" / "Mystery.srm").write_bytes(b"?")
    rom = _make_rom(1, source_filename="Mario.zip")

    matched, warnings = list_local_saves(_install(saves, sort_by_core=True), [rom])
    assert matched == []
    assert len(warnings) == 1
    assert "Mystery.srm" in warnings[0]


def test_unmatched_does_not_block_matched(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    (saves / "snes9x").mkdir(parents=True)
    (saves / "snes9x" / "Mario.srm").write_bytes(b"a")
    (saves / "snes9x" / "Unknown.srm").write_bytes(b"b")
    rom = _make_rom(1, source_filename="Mario.zip")

    matched, warnings = list_local_saves(_install(saves, sort_by_core=True), [rom])
    assert len(matched) == 1
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Various extensions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", [".srm", ".sav", ".rtc"])
def test_walker_includes_common_save_extensions(tmp_path: Path, ext: str) -> None:
    saves = tmp_path / "saves"
    saves.mkdir()
    save = saves / f"Game{ext}"
    save.write_bytes(b"x")
    rom = _make_rom(1, source_filename="Game.zip")

    matched, _ = list_local_saves(_install(saves), [rom])
    assert len(matched) == 1
    assert matched[0].save_filename == f"Game{ext}"


# ---------------------------------------------------------------------------
# Empty / missing saves dir
# ---------------------------------------------------------------------------


def test_missing_saves_dir_returns_empty(tmp_path: Path) -> None:
    saves = tmp_path / "nonexistent"
    matched, warnings = list_local_saves(_install(saves), [])
    assert matched == []
    assert warnings == []


def test_empty_saves_dir_returns_empty(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    saves.mkdir()
    matched, warnings = list_local_saves(_install(saves), [])
    assert matched == []
    assert warnings == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_saves_returned_in_sorted_path_order(tmp_path: Path) -> None:
    saves = tmp_path / "saves"
    (saves / "snes9x").mkdir(parents=True)
    (saves / "snes9x" / "Z.srm").write_bytes(b"z")
    (saves / "snes9x" / "A.srm").write_bytes(b"a")
    (saves / "snes9x" / "M.srm").write_bytes(b"m")
    roms = [
        _make_rom(1, source_filename="Z.zip"),
        _make_rom(2, source_filename="A.zip"),
        _make_rom(3, source_filename="M.zip"),
    ]
    matched, _ = list_local_saves(_install(saves, sort_by_core=True), roms)
    assert [s.save_filename for s in matched] == ["A.srm", "M.srm", "Z.srm"]
