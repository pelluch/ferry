"""Tests for ferry.adapters.cemu.wiiu_saves.list_local_saves.

Walker shape mirrors `test_wii_saves.py`. Title-ID extraction is driven
through a pre-seeded `WiiUTitleCache` so the tests never shell out to
Cemu: a cache hit short-circuits `lookup_wiiu_title` before it would
invoke the tool. The one "extraction failed" case is induced via the
real `extract_title_id` keys.txt pre-flight (no keys.txt planted →
clean None), so it exercises a genuine failure path with no mocking.
"""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.cemu.cemu_paths import CemuInstall
from ferry.adapters.cemu.cemu_tool import CemuTool, WiiUTitle, WiiUTitleCache
from ferry.adapters.cemu.wiiu_saves import list_local_saves, wiiu_save_folder
from ferry.adapters.dolphin.dolphin_archive import folder_content_hash
from ferry.domain.state import RomState, TransformedOutput

# Real Wii U title ID: BotW (USA).
_BOTW_TITLE_ID = "00050000101C9400"
_BOTW_HIGH = "00050000"
_BOTW_LOW = "101c9400"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_install(
    home: Path, *, saves_root: Path | None = None, data_dir: Path | None = None
) -> CemuInstall:
    return CemuInstall(
        source="retrodeck-flatpak",
        wiiu_saves_root=saves_root or (home / "saves" / "wiiu" / "cemu"),
        data_dir=data_dir or (home / "data" / "Cemu"),
        has_saves=False,
    )


def _make_rom(
    rom_id: int,
    *,
    platform_slug: str = "wiiu",
    output_path: str = "wiiu/Breath of the Wild (USA).wux",
    name: str = "Breath of the Wild",
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        source_filename=Path(output_path).name,
        source_md5="abc",
        source_size=100,
        source_updated_at="2026-01-01T00:00:00Z",
        transforms=(),
        outputs=(TransformedOutput(path=output_path, md5="d", size=10),),
        primary_output_index=0,
        synced_at="2026-01-01T00:00:01Z",
    )


def _dummy_tool() -> CemuTool:
    """A CemuTool that's never actually invoked (cache hits short-circuit)."""
    return CemuTool(
        source="retrodeck-in-sandbox",
        label="test",
        argv_prefix=("sh", "-c", "<snippet>", "_"),
        cwd_via_snippet=True,
    )


def _plant_rom(roms_base: Path, output_path: str) -> Path:
    p = roms_base / output_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake wux")
    return p


def _seed_title(cache: WiiUTitleCache, rom_path: Path, title_id: str = _BOTW_TITLE_ID) -> None:
    """Pre-seed the title cache so the walker resolves a title without
    invoking Cemu (cache hit short-circuits `lookup_wiiu_title`)."""
    cache.put(rom_path, WiiUTitle(title_id=title_id))


def _plant_save_folder(
    wiiu_saves_root: Path,
    files: dict[str, bytes],
    *,
    title_id_high: str = _BOTW_HIGH,
    title_id_low: str = _BOTW_LOW,
) -> Path:
    """Plant *files* under `<root>/<HIGH>/<LOW>/`. Returns the per-title folder."""
    folder = wiiu_saves_root / title_id_high / title_id_low
    folder.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        target = folder / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return folder


# ---------------------------------------------------------------------------
# wiiu_save_folder
# ---------------------------------------------------------------------------


def test_wiiu_save_folder_resolves_to_high_low_path(tmp_path: Path) -> None:
    install = _make_install(tmp_path, saves_root=tmp_path / "wiiu")
    folder = wiiu_save_folder(install, WiiUTitle(title_id=_BOTW_TITLE_ID))
    assert folder == tmp_path / "wiiu" / "00050000" / "101c9400"


# ---------------------------------------------------------------------------
# Empty-input cases
# ---------------------------------------------------------------------------


def test_empty_when_saves_root_missing(tmp_path: Path) -> None:
    install = _make_install(tmp_path, saves_root=tmp_path / "nope")
    saves, warnings = list_local_saves(install, [], roms_base=tmp_path / "roms", tool=_dummy_tool())
    assert saves == []
    assert warnings == []


def test_empty_when_no_wiiu_roms(tmp_path: Path) -> None:
    saves_root = tmp_path / "wiiu"
    saves_root.mkdir(parents=True)
    install = _make_install(tmp_path, saves_root=saves_root)
    rom = _make_rom(1, platform_slug="snes", output_path="snes/Mario.smc")

    saves, warnings = list_local_saves(
        install, [rom], roms_base=tmp_path / "roms", tool=_dummy_tool()
    )
    assert saves == []
    assert warnings == []


# ---------------------------------------------------------------------------
# Happy path — one LocalSave per Wii U title with save state
# ---------------------------------------------------------------------------


def test_emits_local_save_for_wiiu_title_with_save_state(tmp_path: Path) -> None:
    saves_root = tmp_path / "wiiu"
    install = _make_install(tmp_path, saves_root=saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1)
    rom_path = _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    title_folder = _plant_save_folder(
        saves_root,
        {
            "user/80000001/game_data.sav": b"progress",
            "user/common/option.sav": b"opts",
            "meta/meta.xml": b"<menu/>",
        },
    )
    cache = WiiUTitleCache(tmp_path / "cache.json")
    _seed_title(cache, rom_path)

    saves, warnings = list_local_saves(
        install, [rom], roms_base=roms_base, tool=_dummy_tool(), cache=cache
    )
    assert warnings == []
    assert len(saves) == 1
    ls = saves[0]
    assert ls.rom_id == 1
    assert ls.emulator == "cemu"
    # Argosy schema: slot + filename = rom base name (primary output stem).
    assert ls.slot == "Breath of the Wild (USA)"
    assert ls.save_filename == "Breath of the Wild (USA).zip"
    assert ls.local_path == title_folder
    assert ls.local_size == len(b"progress") + len(b"opts") + len(b"<menu/>")


def test_local_md5_matches_folder_content_hash(tmp_path: Path) -> None:
    """Walker's `local_md5` equals what RomM (and Argosy) compute on the
    corresponding zip — the load-bearing cross-tool dedup invariant."""
    saves_root = tmp_path / "wiiu"
    install = _make_install(tmp_path, saves_root=saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1)
    rom_path = _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    title_folder = _plant_save_folder(
        saves_root,
        {"user/80000001/game_data.sav": b"deep progress", "meta/meta.xml": b"<menu/>"},
    )
    cache = WiiUTitleCache(tmp_path / "cache.json")
    _seed_title(cache, rom_path)

    saves, _ = list_local_saves(
        install, [rom], roms_base=roms_base, tool=_dummy_tool(), cache=cache
    )
    assert len(saves) == 1
    assert saves[0].local_md5 == folder_content_hash(title_folder)


def test_skips_title_without_save_folder(tmp_path: Path) -> None:
    """Title resolves but Cemu has no save folder for it yet → no
    LocalSave, no warning."""
    saves_root = tmp_path / "wiiu"
    saves_root.mkdir(parents=True)
    install = _make_install(tmp_path, saves_root=saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1)
    rom_path = _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    cache = WiiUTitleCache(tmp_path / "cache.json")
    _seed_title(cache, rom_path)

    saves, warnings = list_local_saves(
        install, [rom], roms_base=roms_base, tool=_dummy_tool(), cache=cache
    )
    assert saves == []
    assert warnings == []


def test_skips_save_folder_with_only_ignored_files(tmp_path: Path) -> None:
    """Save folder exists but holds only OS cruft → treat as no save yet."""
    saves_root = tmp_path / "wiiu"
    install = _make_install(tmp_path, saves_root=saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1)
    rom_path = _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    _plant_save_folder(saves_root, {".DS_Store": b"cruft", "__MACOSX/x": b"shadow"})
    cache = WiiUTitleCache(tmp_path / "cache.json")
    _seed_title(cache, rom_path)

    saves, warnings = list_local_saves(
        install, [rom], roms_base=roms_base, tool=_dummy_tool(), cache=cache
    )
    assert saves == []
    assert warnings == []


def test_skips_dotfiles_in_size(tmp_path: Path) -> None:
    """OS cruft inside a real save folder doesn't inflate `local_size`."""
    saves_root = tmp_path / "wiiu"
    install = _make_install(tmp_path, saves_root=saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1)
    rom_path = _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    _plant_save_folder(
        saves_root,
        {
            "user/80000001/game_data.sav": b"real",
            "user/.DS_Store": b"cruft",
            "__MACOSX/x": b"shadow",
        },
    )
    cache = WiiUTitleCache(tmp_path / "cache.json")
    _seed_title(cache, rom_path)

    saves, _ = list_local_saves(
        install, [rom], roms_base=roms_base, tool=_dummy_tool(), cache=cache
    )
    assert len(saves) == 1
    assert saves[0].local_size == len(b"real")


# ---------------------------------------------------------------------------
# Platform filtering + multi-rom
# ---------------------------------------------------------------------------


def test_filters_to_wiiu_platform(tmp_path: Path) -> None:
    saves_root = tmp_path / "wiiu"
    install = _make_install(tmp_path, saves_root=saves_root)
    roms_base = tmp_path / "roms"

    wiiu_rom = _make_rom(1)
    snes_rom = _make_rom(2, platform_slug="snes", output_path="snes/Mario.smc")
    wiiu_path = _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    _plant_rom(roms_base, "snes/Mario.smc")
    _plant_save_folder(saves_root, {"user/80000001/game_data.sav": b"x"})
    cache = WiiUTitleCache(tmp_path / "cache.json")
    _seed_title(cache, wiiu_path)

    saves, warnings = list_local_saves(
        install, [wiiu_rom, snes_rom], roms_base=roms_base, tool=_dummy_tool(), cache=cache
    )
    assert warnings == []
    assert len(saves) == 1
    assert saves[0].rom_id == 1


def test_accepts_wii_u_dashed_slug(tmp_path: Path) -> None:
    """RomM's `wii-u` slug resolves to the `wiiu` platform dir too."""
    saves_root = tmp_path / "wiiu"
    install = _make_install(tmp_path, saves_root=saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(1, platform_slug="wii-u")
    rom_path = _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    _plant_save_folder(saves_root, {"user/80000001/game_data.sav": b"x"})
    cache = WiiUTitleCache(tmp_path / "cache.json")
    _seed_title(cache, rom_path)

    saves, _ = list_local_saves(
        install, [rom], roms_base=roms_base, tool=_dummy_tool(), cache=cache
    )
    assert len(saves) == 1


# ---------------------------------------------------------------------------
# Warning paths
# ---------------------------------------------------------------------------


def test_warns_when_rom_file_missing(tmp_path: Path) -> None:
    saves_root = tmp_path / "wiiu"
    saves_root.mkdir(parents=True)
    install = _make_install(tmp_path, saves_root=saves_root)
    rom = _make_rom(1)  # no _plant_rom call

    saves, warnings = list_local_saves(
        install, [rom], roms_base=tmp_path / "roms", tool=_dummy_tool()
    )
    assert saves == []
    assert len(warnings) == 1
    assert "ROM file not on disk" in warnings[0]


def test_warns_when_title_extraction_fails(tmp_path: Path) -> None:
    """ROM on disk but the title ID can't be resolved (here: no keys.txt
    in the data dir → `extract_title_id` pre-flight fails cleanly) →
    warn + skip, don't crash."""
    saves_root = tmp_path / "wiiu"
    saves_root.mkdir(parents=True)
    # data_dir has no keys.txt — the extract pre-flight returns None.
    install = _make_install(tmp_path, saves_root=saves_root, data_dir=tmp_path / "data" / "Cemu")
    roms_base = tmp_path / "roms"

    rom = _make_rom(1)
    _plant_rom(roms_base, "wiiu/Breath of the Wild (USA).wux")
    # No cache seeded → lookup falls through to extract_title_id.

    saves, warnings = list_local_saves(install, [rom], roms_base=roms_base, tool=_dummy_tool())
    assert saves == []
    assert len(warnings) == 1
    assert "could not extract Wii U title ID" in warnings[0]
