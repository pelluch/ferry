"""Tests for dolphin_paths' install discovery + active-install selection."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.dolphin.dolphin_paths import (
    DolphinInstall,
    discover_dolphin_installs,
    select_active_install,
)

# ---------------------------------------------------------------------------
# Path constants — match `_PROFILES` in the module under test
# ---------------------------------------------------------------------------

_RD_SAVES = "retrodeck/saves/gc/dolphin"
_RD_CONFIG = ".var/app/net.retrodeck.retrodeck/config/dolphin-emu/Dolphin.ini"
_EMUDECK_SAVES = ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC"
_EMUDECK_CONFIG = ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Config/Dolphin.ini"
_NATIVE_SAVES = ".local/share/dolphin-emu/GC"
_NATIVE_CONFIG = ".local/share/dolphin-emu/Config/Dolphin.ini"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_saves_dir(home: Path, saves_rel: str) -> Path:
    """Just create the saves_root directory itself (no .gci yet)."""
    saves_root = home / saves_rel
    saves_root.mkdir(parents=True, exist_ok=True)
    return saves_root


def _write_config(home: Path, config_rel: str, *, body: str = "") -> Path:
    config_path = home / config_rel
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(body)
    return config_path


def _plant_gci(saves_root: Path, region: str, name: str = "01-GM8E-Test.gci") -> None:
    card = saves_root / region / "Card A"
    card.mkdir(parents=True, exist_ok=True)
    (card / name).write_bytes(b"x" * 8256)


# ---------------------------------------------------------------------------
# discover_dolphin_installs
# ---------------------------------------------------------------------------


def test_no_installs_returns_empty(tmp_path: Path) -> None:
    assert discover_dolphin_installs(tmp_path) == []


def test_native_install_with_config_and_saves(tmp_path: Path) -> None:
    saves = _make_saves_dir(tmp_path, _NATIVE_SAVES)
    config = _write_config(tmp_path, _NATIVE_CONFIG, body="[Core]\nSlotA = 8\n")
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    install = result[0]
    assert install.source == "native"
    assert install.saves_root == saves
    assert install.config_path == config
    assert install.region_encoding == "3-letter"
    assert install.settings is not None
    assert install.slot_a_mode == "gci_folder"
    assert install.has_saves is False


def test_install_present_when_only_config_exists(tmp_path: Path) -> None:
    """Dolphin.ini written by an installer/Flatpak before first launch — saves
    dir doesn't exist yet, but the install is real and discoverable."""
    _write_config(tmp_path, _NATIVE_CONFIG, body="[Core]\nSlotA = 8\n")
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    assert result[0].source == "native"
    assert result[0].has_saves is False


def test_install_present_when_only_saves_dir_exists(tmp_path: Path) -> None:
    """Saves dir created by RetroDECK at install time but Dolphin.ini hasn't
    been written yet — assume modern defaults (GCI Folder)."""
    _make_saves_dir(tmp_path, _RD_SAVES)
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    assert result[0].source == "retrodeck-flatpak"
    assert result[0].settings is None
    assert result[0].slot_a_mode == "gci_folder"  # modern default
    assert result[0].slot_b_mode == "none"


def test_retrodeck_install_uses_2_letter_regions(tmp_path: Path) -> None:
    saves = _make_saves_dir(tmp_path, _RD_SAVES)
    config = _write_config(tmp_path, _RD_CONFIG, body="[Core]\nSlotA = 8\n")
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    install = result[0]
    assert install.source == "retrodeck-flatpak"
    assert install.saves_root == saves
    assert install.config_path == config
    assert install.region_encoding == "2-letter"


def test_emudeck_flatpak_install(tmp_path: Path) -> None:
    saves = _make_saves_dir(tmp_path, _EMUDECK_SAVES)
    config = _write_config(tmp_path, _EMUDECK_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    assert result[0].source == "emudeck-flatpak"
    assert result[0].saves_root == saves
    assert result[0].config_path == config
    assert result[0].region_encoding == "3-letter"


def test_multiple_installs_returned_in_priority_order(tmp_path: Path) -> None:
    _write_config(tmp_path, _NATIVE_CONFIG)
    _write_config(tmp_path, _EMUDECK_CONFIG)
    _write_config(tmp_path, _RD_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert [i.source for i in result] == ["retrodeck-flatpak", "emudeck-flatpak", "native"]


def test_has_saves_reflects_gci_files_under_saves_root(tmp_path: Path) -> None:
    saves = _make_saves_dir(tmp_path, _RD_SAVES)
    _plant_gci(saves, region="US")  # 2-letter, matches RetroDECK convention
    _write_config(tmp_path, _RD_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert result[0].has_saves is True


def test_has_saves_false_when_no_gci_files(tmp_path: Path) -> None:
    _make_saves_dir(tmp_path, _RD_SAVES)
    _write_config(tmp_path, _RD_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert result[0].has_saves is False


def test_has_saves_ignores_non_gci_files(tmp_path: Path) -> None:
    """`.raw` memcards and `.gci.deleted` markers don't count — we only
    treat live `.gci` files as evidence of active GCI Folder use."""
    saves = _make_saves_dir(tmp_path, _RD_SAVES)
    card = saves / "US" / "Card A"
    card.mkdir(parents=True)
    (card / "Memcard.US.raw").write_bytes(b"x")
    (card / "01-GM8E-Test.gci.deleted").write_bytes(b"x")
    _write_config(tmp_path, _RD_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert result[0].has_saves is False


def test_retrodeck_install_populates_wii_saves_root(tmp_path: Path) -> None:
    """RetroDECK is the v3.6 retrodeck-only ship — its profile pins a
    Wii NAND root."""
    _make_saves_dir(tmp_path, _RD_SAVES)
    _write_config(tmp_path, _RD_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    assert result[0].wii_saves_root == tmp_path / "retrodeck/saves/wii/dolphin/title"


def test_emudeck_install_wii_saves_root_is_none(tmp_path: Path) -> None:
    """EmuDeck Wii layout isn't pinned yet — install reports None and
    the Wii backend skips it."""
    _make_saves_dir(tmp_path, _EMUDECK_SAVES)
    _write_config(tmp_path, _EMUDECK_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    assert result[0].wii_saves_root is None


def test_native_install_wii_saves_root_is_none(tmp_path: Path) -> None:
    """Native Wii layout isn't pinned yet — install reports None."""
    _make_saves_dir(tmp_path, _NATIVE_SAVES)
    _write_config(tmp_path, _NATIVE_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert len(result) == 1
    assert result[0].wii_saves_root is None


def test_has_saves_searches_recursively_across_regions(tmp_path: Path) -> None:
    saves = _make_saves_dir(tmp_path, _NATIVE_SAVES)
    _plant_gci(saves, region="EUR", name="01-GM8P-PALsave.gci")
    _write_config(tmp_path, _NATIVE_CONFIG)
    result = discover_dolphin_installs(tmp_path)
    assert result[0].has_saves is True


# ---------------------------------------------------------------------------
# select_active_install
# ---------------------------------------------------------------------------


def _install(source, has_saves: bool) -> DolphinInstall:
    return DolphinInstall(
        source=source,
        saves_root=Path(f"/x/{source}/saves"),
        config_path=Path(f"/x/{source}/Dolphin.ini"),
        region_encoding="3-letter",
        settings=None,
        has_saves=has_saves,
    )


def test_select_returns_none_for_empty() -> None:
    assert select_active_install([]) is None


def test_select_returns_only_install() -> None:
    only = _install("native", has_saves=False)
    assert select_active_install([only]) is only


def test_select_picks_install_with_saves_when_others_dont() -> None:
    rd = _install("retrodeck-flatpak", has_saves=False)
    native = _install("native", has_saves=True)
    assert select_active_install([rd, native]) is native


def test_select_returns_none_when_two_installs_have_saves() -> None:
    rd = _install("retrodeck-flatpak", has_saves=True)
    native = _install("native", has_saves=True)
    assert select_active_install([rd, native]) is None


def test_select_falls_back_to_first_priority_when_none_have_saves() -> None:
    rd = _install("retrodeck-flatpak", has_saves=False)
    native = _install("native", has_saves=False)
    assert select_active_install([rd, native]) is rd
