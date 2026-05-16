"""Tests for ferry.adapters.dolphin.gamecube_saves.

v3.7 ck2 schema: walker emits ONE LocalSave per ROM (bundle of all
matched GCIs across both cards), not one per .gci file. Slot +
save_filename are derived from `<rom_base_name>`. Card A and Card B
contents are mashed; on filename clash, Card A wins + warn.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferry.adapters.dolphin.dolphin_archive import files_content_hash
from ferry.adapters.dolphin.dolphin_paths import DolphinInstall, RegionEncoding
from ferry.adapters.dolphin.dolphin_tool import DiscHeader, DiscHeaderCache, DolphinTool
from ferry.adapters.dolphin.gamecube_saves import (
    list_local_saves,
    match_rom_gcis,
    region_card_dir,
)
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
# Bundle emission — one LocalSave per ROM
# ---------------------------------------------------------------------------


def test_finds_metroid_save_in_native_install(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root, region_encoding="3-letter")
    roms_base = tmp_path / "roms"

    output_path = "gc/Metroid Prime (USA) (Rev 2).rvz"
    rom = _make_rom(1, output_path=output_path)
    rom_path = _plant_rom(roms_base, output_path)
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")

    tool = _make_tool(
        {str(rom_path): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    )

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1
    save = saves[0]
    assert save.rom_id == 1
    assert save.emulator == "dolphin"
    # v3.7 schema: slot + filename derived from rom base name (primary
    # output stem), NOT from the GCI's internal save name.
    assert save.slot == "Metroid Prime (USA) (Rev 2)"
    assert save.save_filename == "Metroid Prime (USA) (Rev 2).zip"
    # local_path is a sentinel — actual GCI list is reconstructed at
    # upload time via match_rom_gcis. Just point at saves_root so the
    # base class's "exists" probe passes.
    assert save.local_path == saves_root
    assert save.local_size == 8256  # one GCI


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
    assert saves[0].slot == "Metroid"


def test_multiple_gcis_for_one_rom_collapse_into_a_single_bundle(tmp_path: Path) -> None:
    """Smash Melee has many .gci per game (system save + N replays).
    v3.7 bundles them into ONE LocalSave instead of v3.6's one-per-file."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    output_path = "gc/Smash.rvz"
    rom = _make_rom(1, output_path=output_path, name="Super Smash Bros. Melee")
    rom_path = _plant_rom(roms_base, output_path)

    card = saves_root / "USA" / "Card A"
    _plant_gci(card, "01-GALE-smashbros_personal_data.gci", b"a" * 1024)
    _plant_gci(card, "01-GALE-SuperSmashBros0110290334.gci", b"b" * 2048)
    _plant_gci(card, "01-GALE-SuperSmashBros0110290335.gci", b"c" * 4096)

    tool = _make_tool(
        {str(rom_path): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")}
    )

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1  # ONE bundle, not three records
    assert saves[0].slot == "Smash"
    assert saves[0].local_size == 1024 + 2048 + 4096  # sum of all matched GCIs


def test_each_rom_gets_its_own_bundle(tmp_path: Path) -> None:
    """Two ROMs → two distinct LocalSaves keyed on their own rom_base_name."""
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
    assert keys == [(1, "dolphin", "Metroid"), (2, "dolphin", "Smash")]


def test_local_md5_matches_files_content_hash_three_way_invariant(tmp_path: Path) -> None:
    """Walker's `local_md5` equals what RomM (and Argosy) would compute
    on the corresponding bundle zip. This is the load-bearing invariant
    for cross-tool dedup; without it classify-time hash equality breaks."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    output_path = "gc/Smash.rvz"
    rom = _make_rom(1, output_path=output_path)
    rp = _plant_rom(roms_base, output_path)

    card = saves_root / "USA" / "Card A"
    g1 = _plant_gci(card, "01-GALE-personal.gci", b"persistence")
    g2 = _plant_gci(card, "01-GALE-replay-001.gci", b"replay one")
    g3 = _plant_gci(card, "01-GALE-replay-002.gci", b"replay two")

    tool = _make_tool({str(rp): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")})

    saves, _ = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert len(saves) == 1
    expected = files_content_hash([g1, g2, g3], wrapper="Smash")
    assert saves[0].local_md5 == expected


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
# Card A + Card B mashing
# ---------------------------------------------------------------------------


def test_card_a_and_card_b_contents_are_mashed_when_no_clash(tmp_path: Path) -> None:
    """v3.7 widens the upload scope from Card A only (v3.6) to Card A + B
    mashed (Argosy parity). Both cards' GCIs land in the bundle."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    output_path = "gc/Smash.rvz"
    rom = _make_rom(1, output_path=output_path)
    rp = _plant_rom(roms_base, output_path)

    _plant_gci(saves_root / "USA" / "Card A", "01-GALE-personal.gci", b"a" * 100)
    _plant_gci(saves_root / "USA" / "Card B", "01-GALE-replay-overflow.gci", b"b" * 200)

    tool = _make_tool({str(rp): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")})

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1
    assert saves[0].local_size == 300


def test_card_a_wins_filename_clash_with_warning(tmp_path: Path) -> None:
    """Same `<MAKER>-<CODE>-<INTERNAL>.gci` in both cards: deterministic
    Card A bias matches v3 priority. User sees a warning naming both
    card paths so they can manually reconcile if the wrong copy won."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    output_path = "gc/Smash.rvz"
    rom = _make_rom(1, output_path=output_path)
    rp = _plant_rom(roms_base, output_path)

    a_path = _plant_gci(saves_root / "USA" / "Card A", "01-GALE-personal.gci", b"card-a-content")
    b_path = _plant_gci(saves_root / "USA" / "Card B", "01-GALE-personal.gci", b"card-b-content")

    tool = _make_tool({str(rp): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")})

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert len(saves) == 1
    # Bundle reflects Card A's bytes (used in the hash) — verify by
    # asserting the bundle's local_md5 matches what files_content_hash
    # would produce on Card A's path alone.
    assert saves[0].local_md5 == files_content_hash([a_path], wrapper="Smash")
    assert any(
        "01-GALE-personal.gci" in w
        and "Card A" in w
        and "Card B" in w
        and str(a_path) in w
        and str(b_path) in w
        for w in warnings
    )


def test_card_b_only_save_still_picked_up(tmp_path: Path) -> None:
    """If Card A has nothing for this game but Card B does, the Card B
    save still ends up in the bundle (just with a single source)."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    output_path = "gc/Smash.rvz"
    rom = _make_rom(1, output_path=output_path)
    rp = _plant_rom(roms_base, output_path)
    # Card A directory exists with unrelated content; Card B has the GCI.
    (saves_root / "USA" / "Card A").mkdir(parents=True)
    _plant_gci(saves_root / "USA" / "Card B", "01-GALE-personal.gci", b"b-only")

    tool = _make_tool({str(rp): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")})

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert warnings == []
    assert len(saves) == 1
    assert saves[0].local_size == len(b"b-only")


# ---------------------------------------------------------------------------
# match_rom_gcis (used by walker AND backend at upload time)
# ---------------------------------------------------------------------------


def test_match_rom_gcis_returns_empty_for_unsupported_region(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    header = DiscHeader(game_code="GZ2K", maker_code="01", region="NTSC-K")
    paths, warnings = match_rom_gcis(install, header)
    assert paths == []
    assert warnings == []


def test_match_rom_gcis_returns_empty_when_region_dir_missing(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    saves_root.mkdir()  # exists but no region subdirs
    install = _make_install(saves_root)
    header = DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")
    paths, warnings = match_rom_gcis(install, header)
    assert paths == []
    assert warnings == []


def test_match_rom_gcis_returns_sorted_paths(tmp_path: Path) -> None:
    """Deterministic ordering — caller (archive helpers) requires it
    so the produced zip is reproducible."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    card = saves_root / "USA" / "Card A"
    _plant_gci(card, "01-GALE-zzz.gci")
    _plant_gci(card, "01-GALE-aaa.gci")
    _plant_gci(card, "01-GALE-mmm.gci")
    header = DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")

    paths, _ = match_rom_gcis(install, header)
    names = [p.name for p in paths]
    assert names == ["01-GALE-aaa.gci", "01-GALE-mmm.gci", "01-GALE-zzz.gci"]


# ---------------------------------------------------------------------------
# region_card_dir
# ---------------------------------------------------------------------------


def test_region_card_dir_returns_card_a_path(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root, region_encoding="3-letter")
    assert region_card_dir(install, "NTSC-U") == saves_root / "USA" / "Card A"
    assert region_card_dir(install, "PAL") == saves_root / "EUR" / "Card A"


def test_region_card_dir_returns_none_for_unknown_region(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "GC")
    assert region_card_dir(install, "NTSC-K") is None


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
    """`01-GM8E-Foo.gci.deleted` shouldn't match the `*.gci` glob.
    With per-rom bundling the assertion is on the bundle's hash —
    ensure the .deleted file's bytes don't contribute."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, output_path="gc/Metroid.rvz")
    rp = _plant_rom(roms_base, "gc/Metroid.rvz")
    card = saves_root / "USA" / "Card A"
    real = _plant_gci(card, "01-GM8E-MetroidPrime A.gci", b"real")
    _plant_gci(card, "01-GM8E-MetroidPrime A.gci.deleted", b"tombstone-bytes")

    tool = _make_tool({str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")})

    saves, _ = list_local_saves(install, [rom], roms_base=roms_base, tool=tool)
    assert len(saves) == 1
    # Bundle hash should match a hash computed on ONLY the real file —
    # the .deleted tombstone must not contribute.
    assert saves[0].local_md5 == files_content_hash([real], wrapper="Metroid")
