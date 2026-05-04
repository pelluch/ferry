"""Tests for retroarch_paths' install discovery + active-install selection."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.retroarch_paths import (
    RetroArchInstall,
    discover_retroarch_installs,
    select_active_install,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RD_CONFIG = ".var/app/net.retrodeck.retrodeck/config/retroarch"
_LR_CONFIG = ".var/app/org.libretro.RetroArch/config/retroarch"
_NATIVE_CONFIG = ".config/retroarch"


def _make_cfg(home: Path, config_root_rel: str, *, body: str) -> Path:
    cfg_root = home / config_root_rel
    cfg_root.mkdir(parents=True, exist_ok=True)
    cfg = cfg_root / "retroarch.cfg"
    cfg.write_text(body)
    return cfg


def _retrodeck_cfg(home: Path, *, saves_path: Path | None = None) -> Path:
    saves = saves_path or (home / "retrodeck/saves")
    return _make_cfg(
        home,
        _RD_CONFIG,
        body=(
            f'savefile_directory = "{saves}"\n'
            'sort_savefiles_by_content_enable = "true"\n'
            'sort_savefiles_enable = "false"\n'
        ),
    )


def _libretro_cfg(home: Path) -> Path:
    return _make_cfg(home, _LR_CONFIG, body="")


def _native_cfg(home: Path, *, saves_path: Path | None = None) -> Path:
    saves = saves_path or (home / ".config/retroarch/saves")
    return _make_cfg(
        home,
        _NATIVE_CONFIG,
        body=(f'savefile_directory = "{saves}"\nsort_savefiles_enable = "true"\n'),
    )


def _plant_save(saves_dir: Path, name: str = "Mario.srm") -> None:
    saves_dir.mkdir(parents=True, exist_ok=True)
    (saves_dir / name).write_bytes(b"x")


# ---------------------------------------------------------------------------
# discover_retroarch_installs
# ---------------------------------------------------------------------------


def test_no_cfg_anywhere_returns_empty(tmp_path: Path) -> None:
    assert discover_retroarch_installs(tmp_path) == []


def test_single_native_install(tmp_path: Path) -> None:
    cfg = _native_cfg(tmp_path)
    result = discover_retroarch_installs(tmp_path)
    assert len(result) == 1
    install = result[0]
    assert install.source == "native"
    assert install.cfg_path == cfg
    assert install.savefile_directory == tmp_path / ".config/retroarch/saves"
    assert install.sort_savefiles_enable is True
    assert install.sort_savefiles_by_content_enable is False
    assert install.has_saves is False


def test_retrodeck_install_savefile_directory_outside_config_tree(tmp_path: Path) -> None:
    """RetroDECK overrides savefile_directory to point at ~/retrodeck/saves."""
    _retrodeck_cfg(tmp_path)
    result = discover_retroarch_installs(tmp_path)
    assert len(result) == 1
    install = result[0]
    assert install.source == "retrodeck-flatpak"
    assert install.savefile_directory == tmp_path / "retrodeck/saves"
    assert install.sort_savefiles_by_content_enable is True


def test_libretro_flatpak_falls_back_to_default_saves_dir(tmp_path: Path) -> None:
    """When the cfg doesn't set savefile_directory, fall back to <config>/saves."""
    _libretro_cfg(tmp_path)
    result = discover_retroarch_installs(tmp_path)
    assert len(result) == 1
    install = result[0]
    assert install.source == "libretro-flatpak"
    expected_saves = tmp_path / _LR_CONFIG / "saves"
    assert install.savefile_directory == expected_saves


def test_multiple_installs_returned_in_priority_order(tmp_path: Path) -> None:
    _native_cfg(tmp_path)
    _retrodeck_cfg(tmp_path)
    _libretro_cfg(tmp_path)
    result = discover_retroarch_installs(tmp_path)
    assert [i.source for i in result] == ["retrodeck-flatpak", "libretro-flatpak", "native"]


def test_has_saves_flag_reflects_savefile_directory_contents(tmp_path: Path) -> None:
    saves = tmp_path / "retrodeck/saves"
    _retrodeck_cfg(tmp_path, saves_path=saves)
    _plant_save(saves / "snes")
    result = discover_retroarch_installs(tmp_path)
    assert result[0].has_saves is True


def test_has_saves_false_when_directory_missing(tmp_path: Path) -> None:
    _native_cfg(tmp_path)
    result = discover_retroarch_installs(tmp_path)
    assert result[0].has_saves is False


def test_has_saves_ignores_non_save_files(tmp_path: Path) -> None:
    """Hidden metadata (`.directory`), logs, and stale standalone-emulator
    droppings shouldn't be counted as 'this install is active.' Only files
    with recognized save extensions count."""
    saves = tmp_path / ".config/retroarch/saves"
    saves.mkdir(parents=True)
    (saves / ".directory").write_text("[Desktop Entry]")
    (saves / "dolphin.log").write_text("not a save")
    (saves / "fst.bin").write_bytes(b"binary garbage")
    _native_cfg(tmp_path, saves_path=saves)

    result = discover_retroarch_installs(tmp_path)
    assert result[0].has_saves is False


def test_has_saves_recognizes_srm_sav_rtc_extensions(tmp_path: Path) -> None:
    saves = tmp_path / ".config/retroarch/saves"
    saves.mkdir(parents=True)
    (saves / "Mario.srm").write_bytes(b"x")
    _native_cfg(tmp_path, saves_path=saves)
    result = discover_retroarch_installs(tmp_path)
    assert result[0].has_saves is True

    # And .sav alone counts too.
    saves2 = tmp_path / "alt-saves"
    saves2.mkdir()
    (saves2 / "Sonic.sav").write_bytes(b"x")
    _make_cfg(
        tmp_path,
        ".var/app/org.libretro.RetroArch/config/retroarch",
        body=f'savefile_directory = "{saves2}"\n',
    )
    result = discover_retroarch_installs(tmp_path)
    libretro = next(i for i in result if i.source == "libretro-flatpak")
    assert libretro.has_saves is True


# ---------------------------------------------------------------------------
# select_active_install
# ---------------------------------------------------------------------------


def _install(source, has_saves: bool) -> RetroArchInstall:
    return RetroArchInstall(
        source=source,
        cfg_path=Path(f"/x/{source}.cfg"),
        config_root=Path(f"/x/{source}"),
        savefile_directory=Path(f"/x/{source}/saves"),
        sort_savefiles_enable=False,
        sort_savefiles_by_content_enable=False,
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
