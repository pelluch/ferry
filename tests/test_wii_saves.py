"""Tests for `ferry.adapters.dolphin.wii_saves.list_local_saves`.

Walker shape mirrors `test_gamecube_saves.py`: mocked `DolphinTool` for
header lookups, planted ROM bytes + save folders under tmp_path, and
state assertions on the emitted LocalSave records.

The load-bearing property — that the walker's `local_md5` matches what
RomM would compute on a zip of the same folder — is exercised in
`test_walker_local_md5_matches_zip_content_hash`. That's the assertion
that makes ck1's `compute_content_hash` mirror useful: ferry's
classify-time hash compare succeeds on byte-stable content even though
zip bytes drift.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ferry.adapters.dolphin.dolphin_archive import archive_save_folder, compute_content_hash
from ferry.adapters.dolphin.dolphin_paths import DolphinInstall
from ferry.adapters.dolphin.dolphin_tool import DiscHeader, DiscHeaderCache, DolphinTool
from ferry.adapters.dolphin.wii_saves import list_local_saves, wii_save_folder
from ferry.domain.state import RomState, TransformedOutput

# Real Wii title_id: Metroid Prime 3 — Corruption (USA), 0x00010000_524d3345
_METROID_PRIME_3_TITLE_ID = 0x00010000524D3345
_METROID_PRIME_3_HIGH = "00010000"
_METROID_PRIME_3_LOW = "524d3345"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_install(
    saves_root: Path,
    *,
    wii_saves_root: Path | None,
) -> DolphinInstall:
    return DolphinInstall(
        source="retrodeck-flatpak",
        saves_root=saves_root,
        config_path=saves_root.parent / "Dolphin.ini",
        region_encoding="2-letter",
        settings=None,
        has_saves=False,
        wii_saves_root=wii_saves_root,
    )


def _make_rom(
    rom_id: int,
    *,
    platform_slug: str = "wii",
    output_path: str = "wii/Metroid Prime 3 (USA).rvz",
    name: str = "Metroid Prime 3 - Corruption",
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
    """DolphinTool with `read_header` mocked to a path → header lookup table."""
    headers = headers or {}
    tool = MagicMock(spec=DolphinTool)
    tool.read_header = MagicMock(side_effect=lambda p: headers.get(str(p)))
    return tool


def _plant_rom(roms_base: Path, output_path: str) -> Path:
    p = roms_base / output_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake wii rom")
    return p


def _plant_save_folder(
    wii_saves_root: Path,
    title_id_high: str,
    title_id_low: str,
    files: dict[str, bytes],
) -> Path:
    """Plant *files* under the title parent `<HIGH>/<LOW>/`.

    Relpaths in *files* should typically be under `data/` (real Wii
    saves) but `content/` and other subdirs are valid too — the v3.7
    walker zips the whole title parent recursively. Returns the title
    parent (matches what the walker will set as `LocalSave.local_path`).
    """
    title_parent = wii_saves_root / title_id_high / title_id_low
    title_parent.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        target = title_parent / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return title_parent


def _wii_header(title_id: int = _METROID_PRIME_3_TITLE_ID) -> DiscHeader:
    return DiscHeader(game_code="RM3E", maker_code="01", region="NTSC-U", title_id=title_id)


# ---------------------------------------------------------------------------
# wii_save_folder
# ---------------------------------------------------------------------------


def test_wii_save_folder_resolves_to_expected_path(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "gc", wii_saves_root=tmp_path / "wii" / "title")
    folder = wii_save_folder(install, _wii_header())
    # v3.7: title parent (parent of data/), recursive — includes
    # data/, content/, and any other subdirs Dolphin populates.
    assert folder == tmp_path / "wii" / "title" / "00010000" / "524d3345"


def test_wii_save_folder_returns_none_without_wii_saves_root(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "gc", wii_saves_root=None)
    assert wii_save_folder(install, _wii_header()) is None


def test_wii_save_folder_returns_none_when_header_lacks_title_id(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "gc", wii_saves_root=tmp_path / "wii" / "title")
    header_without_id = DiscHeader(
        game_code="RM3E", maker_code="01", region="NTSC-U", title_id=None
    )
    assert wii_save_folder(install, header_without_id) is None


# ---------------------------------------------------------------------------
# list_local_saves — empty / skip cases
# ---------------------------------------------------------------------------


def test_walker_returns_empty_when_install_has_no_wii_saves_root(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "gc", wii_saves_root=None)
    rom = _make_rom(1)
    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=_make_tool())
    assert saves == []
    assert warnings == []


def test_walker_returns_empty_when_wii_saves_root_does_not_exist(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "gc", wii_saves_root=tmp_path / "wii" / "title")
    rom = _make_rom(1)
    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=_make_tool())
    assert saves == []
    assert warnings == []


def test_walker_skips_non_wii_roms(tmp_path: Path) -> None:
    """Walker filters by platform → only Wii ROMs reach dolphin-tool."""
    wii_root = tmp_path / "wii" / "title"
    wii_root.mkdir(parents=True)
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    gc_rom = _make_rom(1, platform_slug="ngc", output_path="gc/Metroid.rvz")
    snes_rom = _make_rom(2, platform_slug="snes", output_path="snes/Mario.smc")

    tool = _make_tool()
    saves, warnings = list_local_saves(install, [gc_rom, snes_rom], roms_base=tmp_path, tool=tool)

    assert saves == []
    assert warnings == []
    tool.read_header.assert_not_called()


def test_walker_skips_titles_without_save_state(tmp_path: Path) -> None:
    """A Wii title with no NAND save yet → no LocalSave, no warning.
    Triggered when the title parent doesn't exist OR exists but has no
    non-ignored files anywhere underneath."""
    wii_root = tmp_path / "wii" / "title"
    wii_root.mkdir(parents=True)
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    tool = _make_tool({str(rom_path): _wii_header()})

    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)

    assert saves == []
    assert warnings == []


def test_walker_skips_title_parent_with_only_ignored_files(tmp_path: Path) -> None:
    """Title parent exists but contains only OS cruft → treat as
    `no save yet` (no warning, no emission)."""
    wii_root = tmp_path / "wii" / "title"
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    _plant_save_folder(
        wii_root,
        _METROID_PRIME_3_HIGH,
        _METROID_PRIME_3_LOW,
        {".DS_Store": b"cruft", "__MACOSX/x": b"shadow"},
    )
    tool = _make_tool({str(rom_path): _wii_header()})

    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)

    assert saves == []
    assert warnings == []


# ---------------------------------------------------------------------------
# list_local_saves — happy paths
# ---------------------------------------------------------------------------


def test_walker_emits_local_save_for_wii_title_with_save_state(tmp_path: Path) -> None:
    wii_root = tmp_path / "wii" / "title"
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    title_parent = _plant_save_folder(
        wii_root,
        _METROID_PRIME_3_HIGH,
        _METROID_PRIME_3_LOW,
        {"data/save.bin": b"main save", "data/banner.bin": b"banner"},
    )
    tool = _make_tool({str(rom_path): _wii_header()})

    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)

    assert warnings == []
    assert len(saves) == 1
    ls = saves[0]
    assert ls.rom_id == 1
    # v3.7 Argosy compat: distinct emulator tag, slot == filename base
    # == rom base name (= primary_output stem).
    assert ls.emulator == "dolphin_wii"
    assert ls.slot == "Metroid Prime 3 (USA)"
    assert ls.save_filename == "Metroid Prime 3 (USA).zip"
    # local_path is the title parent (not the data subfolder) so the
    # archiver picks up data/, content/, and any other subdirs.
    assert ls.local_path == title_parent
    assert ls.local_size == len(b"main save") + len(b"banner")


def test_walker_includes_content_subfolder_in_archive_scope(tmp_path: Path) -> None:
    """v3.7 widens the archive scope from data/ to the title parent.
    Files under content/ (VC titles, system-update content) should now
    contribute to the archive's hash and size."""
    wii_root = tmp_path / "wii" / "title"
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    _plant_save_folder(
        wii_root,
        _METROID_PRIME_3_HIGH,
        _METROID_PRIME_3_LOW,
        {
            "data/save.bin": b"actual state",
            "content/title.tmd": b"vc content",
            "content/sub/extra.bin": b"deep content",
        },
    )
    tool = _make_tool({str(rom_path): _wii_header()})

    saves, _warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)
    assert len(saves) == 1
    expected_size = len(b"actual state") + len(b"vc content") + len(b"deep content")
    assert saves[0].local_size == expected_size


def test_walker_local_md5_matches_zip_content_hash(tmp_path: Path) -> None:
    """The load-bearing property: walker's `local_md5` equals what RomM
    would compute on the zip we'd build from the same folder — and
    what Argosy would compute on its end (three-way invariant). Without
    this, classify-time hash equality wouldn't succeed on byte-stable
    content."""
    wii_root = tmp_path / "wii" / "title"
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    title_parent = _plant_save_folder(
        wii_root,
        _METROID_PRIME_3_HIGH,
        _METROID_PRIME_3_LOW,
        {"data/save.bin": b"main save", "data/nested/extra.dat": b"deep state"},
    )
    tool = _make_tool({str(rom_path): _wii_header()})

    saves, _warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)
    assert len(saves) == 1

    archive = tmp_path / "out.zip"
    archive_save_folder(title_parent, archive)
    assert saves[0].local_md5 == compute_content_hash(archive)


def test_walker_skips_dotfiles_in_size_and_mtime(tmp_path: Path) -> None:
    """OS cruft inside a save folder shouldn't inflate `local_size`
    or pull `local_mtime` forward."""
    wii_root = tmp_path / "wii" / "title"
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    _plant_save_folder(
        wii_root,
        _METROID_PRIME_3_HIGH,
        _METROID_PRIME_3_LOW,
        {
            "data/save.bin": b"real",
            "data/.DS_Store": b"cruft",
            "data/__MACOSX/save.bin": b"shadow",
            ".DS_Store": b"top-level cruft",
        },
    )
    tool = _make_tool({str(rom_path): _wii_header()})

    saves, _warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)
    assert len(saves) == 1
    assert saves[0].local_size == len(b"real")


# ---------------------------------------------------------------------------
# list_local_saves — warning paths
# ---------------------------------------------------------------------------


def test_walker_warns_when_rom_file_missing(tmp_path: Path) -> None:
    wii_root = tmp_path / "wii" / "title"
    wii_root.mkdir(parents=True)
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)  # no _plant_rom call

    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=_make_tool())

    assert saves == []
    assert len(warnings) == 1
    assert "ROM file not on disk" in warnings[0]


def test_walker_warns_when_dolphin_tool_returns_none(tmp_path: Path) -> None:
    wii_root = tmp_path / "wii" / "title"
    wii_root.mkdir(parents=True)
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    tool = _make_tool({})  # empty → read_header returns None

    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)

    assert saves == []
    assert len(warnings) == 1
    assert "could not read disc header" in warnings[0]


def test_walker_warns_when_header_has_no_title_id(tmp_path: Path) -> None:
    """A ROM tagged Wii in RomM but actually a GameCube disc → header
    parses but lacks `title_id`. Warn + skip; don't crash."""
    wii_root = tmp_path / "wii" / "title"
    wii_root.mkdir(parents=True)
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    gc_header = DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U", title_id=None)
    tool = _make_tool({str(rom_path): gc_header})

    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool)

    assert saves == []
    assert len(warnings) == 1
    assert "no title_id" in warnings[0]


# ---------------------------------------------------------------------------
# list_local_saves — cache behavior
# ---------------------------------------------------------------------------


def test_walker_uses_cache_when_provided(tmp_path: Path) -> None:
    """When the cache already has the header, walker doesn't re-shell out."""
    wii_root = tmp_path / "wii" / "title"
    install = _make_install(tmp_path / "gc", wii_saves_root=wii_root)
    rom = _make_rom(1)
    rom_path = _plant_rom(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    _plant_save_folder(
        wii_root, _METROID_PRIME_3_HIGH, _METROID_PRIME_3_LOW, {"data/save.bin": b"x"}
    )

    cache = DiscHeaderCache(tmp_path / "cache.json")
    cache.put(rom_path, _wii_header())

    tool = _make_tool({})  # empty → would fail without the cache
    saves, warnings = list_local_saves(install, [rom], roms_base=tmp_path, tool=tool, cache=cache)

    assert warnings == []
    assert len(saves) == 1
    tool.read_header.assert_not_called()
