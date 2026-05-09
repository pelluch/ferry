"""Tests for ferry.adapters.dolphin.dolphin_saves.list_local_saves."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferry.adapters.dolphin.dolphin_paths import DolphinInstall, RegionEncoding
from ferry.adapters.dolphin.dolphin_saves import list_local_saves
from ferry.adapters.dolphin.dolphin_tool import DiscHeader, DiscHeaderCache, DolphinTool
from ferry.domain.state import RomState, TransformedOutput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_install(
    saves_root: Path, *, region_encoding: RegionEncoding = "3-letter"
) -> DolphinInstall:
    return DolphinInstall(
        source="native",
        saves_root=saves_root,
        config_path=saves_root.parent / "Config" / "Dolphin.ini",
        region_encoding=region_encoding,
        settings=None,
        has_saves=False,
    )


def _make_rom(
    rom_id: int,
    *,
    platform_slug: str = "ngc",
    output_path: str = "gc/Metroid Prime (USA) (Rev 2).rvz",
    name: str = "Metroid Prime",
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        source_filename=Path(output_path).name.replace(".rvz", ".zip"),
        source_md5="abc",
        source_size=100,
        source_updated_at="2026-01-01T00:00:00Z",
        transforms=("unzip",),
        outputs=(TransformedOutput(path=output_path, md5="d", size=10),),
        primary_output_index=0,
        synced_at="2026-01-01T00:00:01Z",
    )


def _make_tool(headers: dict[str, DiscHeader] | None = None) -> DolphinTool:
    """Return a DolphinTool whose read_header is mocked to look up the path
    in `headers`. Missing → returns None (simulates dolphin-tool failure)."""
    headers = headers or {}
    tool = MagicMock(spec=DolphinTool)
    tool.read_header = MagicMock(side_effect=lambda p: headers.get(str(p)))
    return tool


def _plant_rom(roms_base: Path, output_path: str) -> Path:
    p = roms_base / output_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake rvz")
    return p


def _plant_gci(card_dir: Path, filename: str, content: bytes = b"x" * 8256) -> Path:
    card_dir.mkdir(parents=True, exist_ok=True)
    p = card_dir / filename
    p.write_bytes(content)
    return p


# ---------------------------------------------------------------------------
# Empty-input cases
# ---------------------------------------------------------------------------


def test_empty_when_saves_root_missing(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "nope")
    saves, warnings = list_local_saves(install, [], roms_base=tmp_path / "roms", tool=_make_tool())
    assert saves == []
    assert warnings == []


def test_empty_when_no_gamecube_roms(tmp_path: Path) -> None:
    saves_root = tmp_path / "saves"
    saves_root.mkdir()
    install = _make_install(saves_root)
    rom = _make_rom(1, platform_slug="snes", output_path="snes/Mario.smc")
    saves, warnings = list_local_saves(
        install, [rom], roms_base=tmp_path / "roms", tool=_make_tool()
    )
    assert saves == []
    assert warnings == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_finds_metroid_save_in_native_install(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root, region_encoding="3-letter")
    roms_base = tmp_path / "roms"

    output_path = "gc/Metroid Prime (USA) (Rev 2).rvz"
    rom = _make_rom(1, output_path=output_path)
    rom_path = _plant_rom(roms_base, output_path)
    gci = _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")

    tool = _make_tool(
        {str(rom_path): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    )

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1
    save = saves[0]
    assert save.rom_id == 1
    assert save.emulator == "dolphin"
    assert save.slot == "MetroidPrime A"
    assert save.save_filename == "01-GM8E-MetroidPrime A.gci"
    assert save.local_path == gci
    assert save.local_size == 8256


def test_retrodeck_uses_2_letter_region_folder(tmp_path: Path) -> None:
    saves_root = tmp_path / "saves" / "gc" / "dolphin"
    install = _make_install(saves_root, region_encoding="2-letter")
    roms_base = tmp_path / "roms"

    output_path = "gc/Metroid.rvz"
    rom = _make_rom(1, output_path=output_path)
    rom_path = _plant_rom(roms_base, output_path)
    _plant_gci(saves_root / "US" / "Card A", "01-GM8E-MetroidPrime A.gci")

    tool = _make_tool(
        {str(rom_path): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    )

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1
    assert saves[0].slot == "MetroidPrime A"


def test_multiple_gci_files_for_one_rom_each_become_a_save(tmp_path: Path) -> None:
    """Smash Melee has many .gci per game (system save + N replays)."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    output_path = "gc/Smash.rvz"
    rom = _make_rom(1, output_path=output_path, name="Super Smash Bros. Melee")
    rom_path = _plant_rom(roms_base, output_path)

    card = saves_root / "USA" / "Card A"
    _plant_gci(card, "01-GALE-smashbros_personal_data.gci")
    _plant_gci(card, "01-GALE-SuperSmashBros0110290334.gci")
    _plant_gci(card, "01-GALE-SuperSmashBros0110290335.gci")

    tool = _make_tool(
        {str(rom_path): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")}
    )

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    slots = sorted(s.slot for s in saves)
    assert slots == [
        "SuperSmashBros0110290334",
        "SuperSmashBros0110290335",
        "smashbros_personal_data",
    ]


def test_each_save_keyed_by_rom_emulator_slot_triple(tmp_path: Path) -> None:
    """Two ROMs both with one save each → 2 distinct (rom_id, slot) pairs."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom1 = _make_rom(1, output_path="gc/Metroid.rvz")
    rom2 = _make_rom(2, output_path="gc/Smash.rvz", name="Smash")
    rp1 = _plant_rom(roms_base, "gc/Metroid.rvz")
    rp2 = _plant_rom(roms_base, "gc/Smash.rvz")

    card = saves_root / "USA" / "Card A"
    _plant_gci(card, "01-GM8E-MetroidPrime A.gci")
    _plant_gci(card, "01-GALE-smashbros.gci")

    tool = _make_tool(
        {
            str(rp1): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"),
            str(rp2): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U"),
        }
    )

    saves, _ = list_local_saves(install, [rom1, rom2], roms_base=roms_base, tool=tool)
    keys = sorted((s.rom_id, s.emulator, s.slot) for s in saves)
    assert keys == [(1, "dolphin", "MetroidPrime A"), (2, "dolphin", "smashbros")]


def test_walker_filters_to_gc_platform_only(tmp_path: Path) -> None:
    """Non-GC ROMs are skipped without invoking dolphin-tool."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    gc_rom = _make_rom(1, platform_slug="ngc", output_path="gc/Metroid.rvz")
    snes_rom = _make_rom(2, platform_slug="snes", output_path="snes/Mario.smc")
    _plant_rom(roms_base, "gc/Metroid.rvz")
    _plant_rom(roms_base, "snes/Mario.smc")
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")

    tool = _make_tool(
        {
            str(roms_base / "gc/Metroid.rvz"): DiscHeader(
                game_code="GM8E", maker_code="01", region="NTSC-U"
            )
        }
    )

    saves, warnings = list_local_saves(install, [gc_rom, snes_rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1
    # Mocked tool was called only for the GC rom; SNES rom was filtered out.
    assert tool.read_header.call_count == 1


def test_walker_accepts_canonical_gamecube_slug(tmp_path: Path) -> None:
    """`gamecube` (RomM canonical) and `nintendo-gamecube` resolve to `gc` too."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, platform_slug="gamecube", output_path="gc/Metroid.rvz")
    rp = _plant_rom(roms_base, "gc/Metroid.rvz")
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")

    tool = _make_tool({str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")})

    saves, _ = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert len(saves) == 1


# ---------------------------------------------------------------------------
# Region mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoding,region,folder",
    [
        ("3-letter", "NTSC-U", "USA"),
        ("3-letter", "NTSC-J", "JAP"),
        ("3-letter", "PAL", "EUR"),
        ("2-letter", "NTSC-U", "US"),
        ("2-letter", "NTSC-J", "JP"),
        ("2-letter", "PAL", "EU"),
    ],
)
def test_region_mapping_per_encoding(
    tmp_path: Path, encoding: RegionEncoding, region: str, folder: str
) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root, region_encoding=encoding)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/Game.rvz")
    rp = _plant_rom(roms_base, "gc/Game.rvz")
    _plant_gci(saves_root / folder / "Card A", "01-XXXE-Test.gci")

    tool = _make_tool({str(rp): DiscHeader(game_code="XXXE", maker_code="01", region=region)})

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1


def test_unknown_region_warns_and_skips(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    saves_root.mkdir()
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/KoreanGame.rvz")
    rp = _plant_rom(roms_base, "gc/KoreanGame.rvz")

    tool = _make_tool({str(rp): DiscHeader(game_code="GZ2K", maker_code="01", region="NTSC-K")})

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert saves == []
    assert len(warnings) == 1
    assert "unsupported region" in warnings[0]
    assert "NTSC-K" in warnings[0]


# ---------------------------------------------------------------------------
# Warnings on partial failures
# ---------------------------------------------------------------------------


def test_missing_rom_file_warns(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    saves_root.mkdir()
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/Phantom.rvz")  # don't plant the file

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=_make_tool())
    assert saves == []
    assert len(warnings) == 1
    assert "ROM file not on disk" in warnings[0]


def test_dolphin_tool_failure_warns(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    saves_root.mkdir()
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/Garbage.rvz")
    _plant_rom(roms_base, "gc/Garbage.rvz")

    saves, warnings = list_local_saves(
        install,
        [rom],
        roms_base=roms_base,
        tool=_make_tool({}),  # tool returns None
    )
    assert saves == []
    assert len(warnings) == 1
    assert "could not read disc header" in warnings[0]


def test_card_dir_missing_for_region_is_silent(tmp_path: Path) -> None:
    """A discovered install with no saves yet shouldn't generate warnings."""
    saves_root = tmp_path / "GC"
    saves_root.mkdir()
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/Metroid.rvz")
    rp = _plant_rom(roms_base, "gc/Metroid.rvz")

    tool = _make_tool({str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")})

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert saves == []
    assert warnings == []


# ---------------------------------------------------------------------------
# Cache integration
# ---------------------------------------------------------------------------


def test_cache_avoids_redundant_dolphin_tool_calls(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/Metroid.rvz")
    rp = _plant_rom(roms_base, "gc/Metroid.rvz")
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")

    header = DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")
    tool = _make_tool({str(rp): header})
    cache = DiscHeaderCache(tmp_path / "cache.json")

    list_local_saves(install, [rom], roms_base=roms_base, tool=tool, cache=cache)
    list_local_saves(install, [rom], roms_base=roms_base, tool=tool, cache=cache)

    # Second call reuses the cache entry, so dolphin-tool is invoked exactly once.
    assert tool.read_header.call_count == 1


# ---------------------------------------------------------------------------
# Defensive against unexpected filenames
# ---------------------------------------------------------------------------


def test_glob_skips_dot_deleted_markers(tmp_path: Path) -> None:
    """`01-GM8E-Foo.gci.deleted` shouldn't match the `*.gci` glob."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/Metroid.rvz")
    rp = _plant_rom(roms_base, "gc/Metroid.rvz")
    card = saves_root / "USA" / "Card A"
    _plant_gci(card, "01-GM8E-MetroidPrime A.gci")
    _plant_gci(card, "01-GM8E-MetroidPrime A.gci.deleted")  # Dolphin's tombstone

    tool = _make_tool({str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")})

    saves, _ = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert len(saves) == 1
    assert saves[0].save_filename == "01-GM8E-MetroidPrime A.gci"
