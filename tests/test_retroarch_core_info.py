"""Tests for parse_core_info + CoreInfoIndex."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.retroarch_core_info import CoreInfoIndex, parse_core_info
from ferry.adapters.retroarch_paths import RetroArchInstall

# ---------------------------------------------------------------------------
# parse_core_info
# ---------------------------------------------------------------------------


def test_parse_core_info_basic() -> None:
    text = (
        "# Comment line\n"
        'display_name = "Nintendo - SNES (Snes9x)"\n'
        'corename = "Snes9x"\n'
        'supported_extensions = "smc|sfc"\n'
    )
    parsed = parse_core_info(text)
    assert parsed["display_name"] == "Nintendo - SNES (Snes9x)"
    assert parsed["corename"] == "Snes9x"
    assert parsed["supported_extensions"] == "smc|sfc"


def test_parse_core_info_unquoted_values() -> None:
    text = "firmware_count = 2\ncorename = Snes9x\n"
    parsed = parse_core_info(text)
    assert parsed["firmware_count"] == "2"
    assert parsed["corename"] == "Snes9x"


def test_parse_core_info_skips_comments_and_blanks() -> None:
    text = '\n\n# header\ncorename = "X"\n# trailing\n\n'
    parsed = parse_core_info(text)
    assert parsed == {"corename": "X"}


def test_parse_core_info_handles_single_quotes() -> None:
    text = "corename = 'Snes9x'\n"
    parsed = parse_core_info(text)
    assert parsed["corename"] == "Snes9x"


def test_parse_core_info_empty_returns_empty() -> None:
    assert parse_core_info("") == {}


def test_parse_core_info_value_with_spaces() -> None:
    """Common case: `corename = "Genesis Plus GX"` — embedded spaces."""
    parsed = parse_core_info('corename = "Genesis Plus GX"\n')
    assert parsed["corename"] == "Genesis Plus GX"


# ---------------------------------------------------------------------------
# CoreInfoIndex
# ---------------------------------------------------------------------------


def _make_install(core_info_dirs: tuple[Path, ...]) -> RetroArchInstall:
    return RetroArchInstall(
        source="native",
        cfg_path=Path("/x/retroarch.cfg"),
        config_root=Path("/x"),
        savefile_directory=Path("/x/saves"),
        sort_savefiles_enable=True,
        sort_savefiles_by_content_enable=False,
        has_saves=False,
        core_info_candidates=core_info_dirs,
    )


def _plant_info(dir_: Path, core_so: str, *, corename: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{core_so}.info").write_text(f'corename = "{corename}"\n')


# Forward / reverse mapping


def test_forward_returns_corename_for_known_prefix(tmp_path: Path) -> None:
    info_dir = tmp_path / "info"
    _plant_info(info_dir, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((info_dir,)))
    assert index.forward("snes9x") == "Snes9x"


def test_reverse_returns_prefix_for_known_corename(tmp_path: Path) -> None:
    info_dir = tmp_path / "info"
    _plant_info(info_dir, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((info_dir,)))
    assert index.reverse("Snes9x") == "snes9x"


def test_round_trip_many_cores(tmp_path: Path) -> None:
    """Simulate a realistic libretro info dir."""
    info_dir = tmp_path / "info"
    cores = {
        "snes9x_libretro": "Snes9x",
        "mgba_libretro": "mGBA",
        "mupen64plus_next_libretro": "Mupen64Plus-Next",
        "genesis_plus_gx_libretro": "Genesis Plus GX",
        "desmume_libretro": "DeSmuME",
    }
    for core_so, corename in cores.items():
        _plant_info(info_dir, core_so, corename=corename)
    index = CoreInfoIndex(_make_install((info_dir,)))

    for core_so, corename in cores.items():
        prefix = core_so[: -len("_libretro")]
        assert index.forward(prefix) == corename
        assert index.reverse(corename) == prefix


# Identity fallback


def test_unknown_prefix_falls_back_to_identity(tmp_path: Path) -> None:
    info_dir = tmp_path / "info"
    _plant_info(info_dir, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((info_dir,)))
    assert index.forward("totally_unknown") == "totally_unknown"


def test_unknown_corename_falls_back_to_identity(tmp_path: Path) -> None:
    info_dir = tmp_path / "info"
    _plant_info(info_dir, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((info_dir,)))
    assert index.reverse("UnknownCore") == "UnknownCore"


# Discovery / candidate ordering


def test_no_candidates_falls_back_to_identity(tmp_path: Path) -> None:
    index = CoreInfoIndex(_make_install(()))
    assert index.forward("snes9x") == "snes9x"
    assert index.reverse("Snes9x") == "Snes9x"
    assert index.has_data() is False


def test_first_existing_candidate_wins(tmp_path: Path) -> None:
    """If two candidate dirs both have .info files, only the first is scanned."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    _plant_info(first, "snes9x_libretro", corename="FirstSnes")
    _plant_info(second, "snes9x_libretro", corename="SecondSnes")
    index = CoreInfoIndex(_make_install((first, second)))
    assert index.forward("snes9x") == "FirstSnes"


def test_skips_missing_candidates_until_match_found(tmp_path: Path) -> None:
    missing = tmp_path / "missing"  # never created
    real = tmp_path / "real"
    _plant_info(real, "mgba_libretro", corename="mGBA")
    index = CoreInfoIndex(_make_install((missing, real)))
    assert index.forward("mgba") == "mGBA"


def test_skips_candidate_with_no_info_files(tmp_path: Path) -> None:
    """Empty dir or dir with non-.info files isn't accepted as the cores dir."""
    empty = tmp_path / "empty"
    empty.mkdir()
    real = tmp_path / "real"
    _plant_info(real, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((empty, real)))
    assert index.forward("snes9x") == "Snes9x"


# Parsing edge cases


def test_skips_non_libretro_filename(tmp_path: Path) -> None:
    """Files not ending in `_libretro.info` are ignored."""
    info_dir = tmp_path / "info"
    info_dir.mkdir()
    (info_dir / "weird.info").write_text('corename = "Whatever"\n')
    _plant_info(info_dir, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((info_dir,)))
    # `snes9x` resolves; `weird` is ignored.
    assert index.forward("snes9x") == "Snes9x"
    assert index.forward("weird") == "weird"  # identity fallback


def test_info_file_without_corename_is_skipped(tmp_path: Path) -> None:
    info_dir = tmp_path / "info"
    info_dir.mkdir()
    (info_dir / "missing_corename_libretro.info").write_text('display_name = "Has no corename"\n')
    _plant_info(info_dir, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((info_dir,)))
    # missing_corename → identity fallback (not in index).
    assert index.forward("missing_corename") == "missing_corename"
    # snes9x still works.
    assert index.forward("snes9x") == "Snes9x"


def test_caching_avoids_re_scan(tmp_path: Path) -> None:
    """Multiple lookups don't re-read the cores dir."""
    info_dir = tmp_path / "info"
    _plant_info(info_dir, "snes9x_libretro", corename="Snes9x")
    index = CoreInfoIndex(_make_install((info_dir,)))
    # First call triggers scan.
    assert index.forward("snes9x") == "Snes9x"
    # Now mutate the file — second call should NOT pick up the change.
    (info_dir / "snes9x_libretro.info").write_text('corename = "Different"\n')
    assert index.forward("snes9x") == "Snes9x"  # still cached
