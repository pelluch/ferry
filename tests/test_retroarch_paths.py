"""Tests for ferry.adapters.retroarch_paths.discover_retroarch_saves."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.retroarch_paths import (
    RetroArchInstall,
    discover_retroarch_saves,
)


def _make_retrodeck(home: Path) -> Path:
    saves = home / ".var/app/net.retrodeck.retrodeck/config/retroarch/saves"
    saves.mkdir(parents=True)
    return saves


def _make_libretro(home: Path) -> Path:
    saves = home / ".var/app/org.libretro.RetroArch/config/retroarch/saves"
    saves.mkdir(parents=True)
    return saves


def _make_native(home: Path) -> Path:
    saves = home / ".config/retroarch/saves"
    saves.mkdir(parents=True)
    return saves


# ---------------------------------------------------------------------------
# Per-flavor detection
# ---------------------------------------------------------------------------


def test_retrodeck_flatpak_detected(tmp_path: Path) -> None:
    saves = _make_retrodeck(tmp_path)
    result = discover_retroarch_saves(tmp_path)
    assert result == RetroArchInstall(saves_dir=saves, source="retrodeck-flatpak")


def test_libretro_flatpak_detected(tmp_path: Path) -> None:
    saves = _make_libretro(tmp_path)
    result = discover_retroarch_saves(tmp_path)
    assert result == RetroArchInstall(saves_dir=saves, source="libretro-flatpak")


def test_native_detected(tmp_path: Path) -> None:
    saves = _make_native(tmp_path)
    result = discover_retroarch_saves(tmp_path)
    assert result == RetroArchInstall(saves_dir=saves, source="native")


# ---------------------------------------------------------------------------
# Priority resolution
# ---------------------------------------------------------------------------


def test_retrodeck_wins_over_libretro_when_both_present(tmp_path: Path) -> None:
    rd = _make_retrodeck(tmp_path)
    _make_libretro(tmp_path)
    result = discover_retroarch_saves(tmp_path)
    assert result is not None
    assert result.saves_dir == rd
    assert result.source == "retrodeck-flatpak"


def test_libretro_wins_over_native_when_both_present(tmp_path: Path) -> None:
    lr = _make_libretro(tmp_path)
    _make_native(tmp_path)
    result = discover_retroarch_saves(tmp_path)
    assert result is not None
    assert result.saves_dir == lr
    assert result.source == "libretro-flatpak"


def test_retrodeck_wins_over_all_when_all_present(tmp_path: Path) -> None:
    rd = _make_retrodeck(tmp_path)
    _make_libretro(tmp_path)
    _make_native(tmp_path)
    result = discover_retroarch_saves(tmp_path)
    assert result is not None
    assert result.saves_dir == rd
    assert result.source == "retrodeck-flatpak"


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_returns_none_when_nothing_present(tmp_path: Path) -> None:
    assert discover_retroarch_saves(tmp_path) is None


def test_config_root_present_but_saves_missing_returns_none(tmp_path: Path) -> None:
    """Fresh RetroArch install with no plays yet — the saves dir hasn't been
    created. Treat as 'no saves to sync' rather than detecting an empty install."""
    (tmp_path / ".config/retroarch").mkdir(parents=True)
    assert discover_retroarch_saves(tmp_path) is None


def test_saves_path_must_be_a_directory(tmp_path: Path) -> None:
    """Adversarial: a `saves` *file* at the expected location should not match."""
    parent = tmp_path / ".config/retroarch"
    parent.mkdir(parents=True)
    (parent / "saves").write_text("oops, file not directory")
    assert discover_retroarch_saves(tmp_path) is None


def test_default_home_is_path_home(monkeypatch, tmp_path: Path) -> None:
    """No-arg call uses Path.home() — verify by setting HOME and matching."""
    monkeypatch.setenv("HOME", str(tmp_path))
    saves = _make_native(tmp_path)
    result = discover_retroarch_saves()
    assert result is not None
    assert result.saves_dir == saves
