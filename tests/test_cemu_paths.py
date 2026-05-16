"""Tests for ferry.adapters.cemu.cemu_paths."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.cemu.cemu_paths import (
    CemuInstall,
    discover_cemu_installs,
    select_active_install,
)

_RETRODECK_SAVES = "retrodeck/saves/wiiu/cemu"
_RETRODECK_DATA = ".var/app/net.retrodeck.retrodeck/data/Cemu"


def _plant_game_save(home: Path, title_low: str) -> Path:
    """Create a per-game save folder under the RetroDECK wiiu/cemu tree."""
    folder = home / _RETRODECK_SAVES / "00050000" / title_low
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# discover_cemu_installs
# ---------------------------------------------------------------------------


def test_discover_empty_when_nothing_present(tmp_path: Path) -> None:
    assert discover_cemu_installs(tmp_path) == []


def test_discover_finds_retrodeck_install_with_saves(tmp_path: Path) -> None:
    _plant_game_save(tmp_path, "101c9400")
    (tmp_path / _RETRODECK_DATA).mkdir(parents=True)

    installs = discover_cemu_installs(tmp_path)
    assert len(installs) == 1
    install = installs[0]
    assert install.source == "retrodeck-flatpak"
    assert install.wiiu_saves_root == tmp_path / _RETRODECK_SAVES
    assert install.data_dir == tmp_path / _RETRODECK_DATA
    assert install.has_saves is True


def test_discover_data_dir_only_is_present_without_saves(tmp_path: Path) -> None:
    """Cemu installed but no game has written a save yet — still surfaced."""
    (tmp_path / _RETRODECK_DATA).mkdir(parents=True)

    installs = discover_cemu_installs(tmp_path)
    assert len(installs) == 1
    assert installs[0].has_saves is False


def test_discover_saves_root_only_is_present(tmp_path: Path) -> None:
    """Saves tree exists, data dir doesn't — still surfaced (either signal)."""
    _plant_game_save(tmp_path, "101c9400")

    installs = discover_cemu_installs(tmp_path)
    assert len(installs) == 1
    assert installs[0].has_saves is True


def test_has_saves_false_when_games_root_empty(tmp_path: Path) -> None:
    """`00050000/` exists but contains no per-game folders."""
    (tmp_path / _RETRODECK_SAVES / "00050000").mkdir(parents=True)
    (tmp_path / _RETRODECK_DATA).mkdir(parents=True)

    installs = discover_cemu_installs(tmp_path)
    assert len(installs) == 1
    assert installs[0].has_saves is False


def test_has_saves_ignores_loose_files_under_games_root(tmp_path: Path) -> None:
    """Only directories under `00050000/` count as games — a stray file doesn't."""
    games_root = tmp_path / _RETRODECK_SAVES / "00050000"
    games_root.mkdir(parents=True)
    (games_root / "stray.txt").write_text("noise")

    installs = discover_cemu_installs(tmp_path)
    assert len(installs) == 1
    assert installs[0].has_saves is False


# ---------------------------------------------------------------------------
# CemuInstall.games_root
# ---------------------------------------------------------------------------


def test_games_root_property(tmp_path: Path) -> None:
    install = CemuInstall(
        source="retrodeck-flatpak",
        wiiu_saves_root=tmp_path / "saves",
        data_dir=tmp_path / "data",
        has_saves=False,
    )
    assert install.games_root == tmp_path / "saves" / "00050000"


# ---------------------------------------------------------------------------
# select_active_install
# ---------------------------------------------------------------------------


def test_select_active_returns_sole_install(tmp_path: Path) -> None:
    _plant_game_save(tmp_path, "101c9400")
    installs = discover_cemu_installs(tmp_path)
    assert select_active_install(installs) is installs[0]


def test_select_active_none_when_no_installs() -> None:
    assert select_active_install([]) is None
