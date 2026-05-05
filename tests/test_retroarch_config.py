"""Tests for ferry.adapters.retroarch_config.parse_retroarch_cfg."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.retroarch_config import RetroArchSaveSettings, parse_retroarch_cfg


def _write_cfg(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Defaults / missing keys
# ---------------------------------------------------------------------------


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert parse_retroarch_cfg(tmp_path / "nope.cfg") is None


def test_empty_cfg_returns_settings_with_defaults(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "retroarch.cfg", "")
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result == RetroArchSaveSettings(
        cfg_path=cfg,
        savefile_directory=None,
        sort_savefiles_enable=False,
        sort_savefiles_by_content_enable=False,
        libretro_directory=None,
        libretro_info_path=None,
    )


def test_only_unrelated_keys_uses_defaults(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "ra.cfg", 'video_driver = "vulkan"\nfullscreen = "true"\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory is None
    assert result.sort_savefiles_enable is False
    assert result.sort_savefiles_by_content_enable is False


# ---------------------------------------------------------------------------
# savefile_directory parsing
# ---------------------------------------------------------------------------


def test_absolute_savefile_directory(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "ra.cfg", 'savefile_directory = "/data/saves"\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory == Path("/data/saves")


def test_tilde_savefile_directory_expands_against_home(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "ra.cfg", 'savefile_directory = "~/retrodeck/saves"\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory == tmp_path / "retrodeck/saves"


def test_empty_savefile_directory_yields_none(tmp_path: Path) -> None:
    """An empty string means 'use the default' — surface as None."""
    cfg = _write_cfg(tmp_path / "ra.cfg", 'savefile_directory = ""\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory is None


def test_default_value_yields_none(tmp_path: Path) -> None:
    """RetroArch occasionally serializes the literal string `default`."""
    cfg = _write_cfg(tmp_path / "ra.cfg", 'savefile_directory = "default"\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory is None


# ---------------------------------------------------------------------------
# sort_* booleans
# ---------------------------------------------------------------------------


def test_sort_savefiles_enable_true(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "ra.cfg", 'sort_savefiles_enable = "true"\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.sort_savefiles_enable is True


def test_sort_savefiles_by_content_enable_true(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "ra.cfg", 'sort_savefiles_by_content_enable = "true"\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.sort_savefiles_by_content_enable is True


def test_sort_falsey_strings_remain_false(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path / "ra.cfg",
        'sort_savefiles_enable = "false"\nsort_savefiles_by_content_enable = "0"\n',
    )
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.sort_savefiles_enable is False
    assert result.sort_savefiles_by_content_enable is False


# ---------------------------------------------------------------------------
# Real-world cfg shape
# ---------------------------------------------------------------------------


def test_retrodeck_style_cfg(tmp_path: Path) -> None:
    """Mirrors what RetroDECK writes to its bundled retroarch.cfg."""
    cfg = _write_cfg(
        tmp_path / "ra.cfg",
        'video_driver = "vulkan"\n'
        'savefile_directory = "/home/deck/retrodeck/saves"\n'
        'savestate_directory = "/home/deck/retrodeck/states"\n'
        'sort_savefiles_by_content_enable = "true"\n'
        'sort_savefiles_enable = "false"\n',
    )
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory == Path("/home/deck/retrodeck/saves")
    assert result.sort_savefiles_by_content_enable is True
    assert result.sort_savefiles_enable is False


def test_native_style_cfg(tmp_path: Path) -> None:
    """Mirrors what an Arch AUR-installed RetroArch typically has."""
    cfg = _write_cfg(
        tmp_path / "ra.cfg",
        'savefile_directory = "~/.config/retroarch/saves"\n'
        'sort_savefiles_enable = "true"\n'
        'sort_savefiles_by_content_enable = "false"\n',
    )
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory == tmp_path / ".config/retroarch/saves"
    assert result.sort_savefiles_enable is True
    assert result.sort_savefiles_by_content_enable is False


# ---------------------------------------------------------------------------
# Robustness against weird input
# ---------------------------------------------------------------------------


def test_comments_are_ignored(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path / "ra.cfg",
        '# savefile_directory = "/should-be-ignored"\nsavefile_directory = "/real"\n',
    )
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory == Path("/real")


def test_blank_lines_are_ignored(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "ra.cfg", '\n\nsavefile_directory = "/x"\n\n')
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.savefile_directory == Path("/x")


def test_unquoted_values_still_parse(tmp_path: Path) -> None:
    """Older retroarch.cfg formats sometimes lack quotes."""
    cfg = _write_cfg(
        tmp_path / "ra.cfg",
        "sort_savefiles_enable = true\nsort_savefiles_by_content_enable = true\n",
    )
    result = parse_retroarch_cfg(cfg, home=tmp_path)
    assert result is not None
    assert result.sort_savefiles_enable is True
    assert result.sort_savefiles_by_content_enable is True
