"""Tests for `ferry status` — read-only state-vs-disk introspection."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from ferry.adapters.sidecar import write_sidecar
from ferry.adapters.state_store import default_state_path, save_state
from ferry.cli import app
from ferry.domain.state import LibraryState, RomState, TransformedOutput

BASE_URL = "https://romm.example.tld"


def write_config(cfg: Path, *, roms_base: Path | None = None) -> Path:
    parts = [
        "[romm]",
        f'url = "{BASE_URL}"',
        'api_key = "rmm_abcdef0123456789"',
        "",
        "[destination]",
    ]
    parts.append(f'roms_base = "{roms_base}"' if roms_base else 'preset = "esde-native"')
    parts += ["", "[sync]", 'collections = ["Steam Deck"]']
    cfg.write_text("\n".join(parts) + "\n")
    return cfg


def make_rom(rom_id: int, *, platform: str = "gba", name: str | None = None) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug=platform,
        name=name or f"Game {rom_id}",
        source_filename=f"Game{rom_id}.zip",
        source_md5="abc",
        source_size=100,
        source_updated_at="2026-01-01T00:00:00Z",
        transforms=("unzip",),
        outputs=(TransformedOutput(path=f"{platform}/Game{rom_id}.gba", md5="d", size=10),),
        primary_output_index=0,
        synced_at="2026-01-01T00:00:01Z",
    )


def test_status_with_empty_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "0 ROM(s) tracked" in result.output
    assert "first sync will populate" in result.output
    assert "rmm_…789" in result.output  # masked api key


def test_status_with_populated_state_all_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)

    # Save state with 2 GBA roms.
    rom1 = make_rom(1, name="A")
    rom2 = make_rom(2, name="B")
    save_state(LibraryState(roms={1: rom1, 2: rom2}), default_state_path())

    # Place primaries + sidecars.
    for rom in (rom1, rom2):
        primary = roms_base / rom.primary_output.path
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_bytes(b"x")
        write_sidecar(primary, rom, roms_base=roms_base)

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "2 ROM(s) tracked" in result.output
    assert "✓ gba" in result.output
    assert "Issues:" not in result.output


def test_status_flags_missing_primary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)

    rom = make_rom(1, name="Ghost")
    save_state(LibraryState(roms={1: rom}), default_state_path())
    # Don't create the primary file on disk.

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "✗ gba" in result.output
    assert "1 missing on disk" in result.output
    assert "Issues:" in result.output
    assert "re-download" in result.output


def test_status_flags_missing_sidecar(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)

    rom = make_rom(1)
    save_state(LibraryState(roms={1: rom}), default_state_path())
    primary = roms_base / rom.primary_output.path
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"x")
    # Skip writing sidecar.

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "1 missing sidecars" in result.output
    assert "regenerate" in result.output


def test_status_summarizes_trash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = write_config(tmp_path / "config.toml")
    trash_root = tmp_path / ".local" / "state" / "ferry" / "trash"
    entry = trash_root / "20260420T120000Z__rom42"
    entry.mkdir(parents=True)
    (entry / "Game.gba").write_bytes(b"x" * 1024)

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "1 entry" in result.output
    assert "1.0 KB" in result.output


def test_status_resolves_platform_dir_in_output(tmp_path: Path, monkeypatch) -> None:
    """Non-canonical RomM slug shows arrow to ES-DE dir name."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)

    rom = make_rom(1, platform="game-boy-advance")
    save_state(LibraryState(roms={1: rom}), default_state_path())

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    # Slug → resolved dir mapping is surfaced.
    assert "game-boy-advance → gba/" in result.output


def test_status_reports_no_retroarch_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "[saves]" in result.output
    assert "retroarch:   (not detected)" in result.output


def _write_ra_cfg(home: Path, config_root_rel: str, body: str) -> Path:
    cfg_root = home / config_root_rel
    cfg_root.mkdir(parents=True, exist_ok=True)
    cfg = cfg_root / "retroarch.cfg"
    cfg.write_text(body)
    return cfg


def test_status_reports_retrodeck_install_with_external_savefile_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """RetroDECK overrides savefile_directory; status should show that path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    saves_dir = tmp_path / "retrodeck/saves"
    saves_dir.mkdir(parents=True)
    _write_ra_cfg(
        tmp_path,
        ".var/app/net.retrodeck.retrodeck/config/retroarch",
        f'savefile_directory = "{saves_dir}"\n'
        'sort_savefiles_by_content_enable = "true"\n'
        'sort_savefiles_enable = "false"\n',
    )
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "retroarch:   retrodeck-flatpak" in result.output
    assert str(saves_dir) in result.output
    assert "by-content" in result.output


def test_status_reports_native_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    saves_dir = tmp_path / ".config/retroarch/saves"
    saves_dir.mkdir(parents=True)
    _write_ra_cfg(
        tmp_path,
        ".config/retroarch",
        'sort_savefiles_enable = "true"\n',
    )
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "retroarch:   native" in result.output
    assert "by-core" in result.output


def test_status_flags_ambiguity_when_multiple_installs_have_saves(
    tmp_path: Path, monkeypatch
) -> None:
    """Two installs both have files in their savefile_directory — surface
    the ambiguity rather than silently picking one."""
    monkeypatch.setenv("HOME", str(tmp_path))

    # RetroDECK config with savefile_directory pointing at ~/retrodeck/saves
    rd_saves = tmp_path / "retrodeck/saves"
    rd_saves.mkdir(parents=True)
    (rd_saves / "Mario.srm").write_bytes(b"x")
    _write_ra_cfg(
        tmp_path,
        ".var/app/net.retrodeck.retrodeck/config/retroarch",
        f'savefile_directory = "{rd_saves}"\n',
    )

    # Native config with default <config>/saves/ as savefile_directory
    native_saves = tmp_path / ".config/retroarch/saves"
    native_saves.mkdir(parents=True)
    (native_saves / "Sonic.srm").write_bytes(b"y")
    _write_ra_cfg(
        tmp_path,
        ".config/retroarch",
        f'savefile_directory = "{native_saves}"\n',
    )

    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "AMBIGUOUS" in result.output
    assert "retrodeck-flatpak" in result.output
    assert "native" in result.output
    assert "[saves.retroarch_install]" in result.output


def test_status_honors_configured_retroarch_install(tmp_path: Path, monkeypatch) -> None:
    """`[saves].retroarch_install` overrides auto-selection in status output."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Plant both: RetroDECK with active saves, native without.
    rd_saves = tmp_path / "retrodeck/saves"
    rd_saves.mkdir(parents=True)
    (rd_saves / "Mario.srm").write_bytes(b"x")
    _write_ra_cfg(
        tmp_path,
        ".var/app/net.retrodeck.retrodeck/config/retroarch",
        f'savefile_directory = "{rd_saves}"\n',
    )
    native_saves = tmp_path / ".config/retroarch/saves"
    native_saves.mkdir(parents=True)
    _write_ra_cfg(tmp_path, ".config/retroarch", "")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[romm]\nurl = "{BASE_URL}"\napi_key = "rmm_abcdef0123456789"\n'
        '[destination]\npreset = "esde-native"\n'
        '[sync]\ncollections = ["Steam Deck"]\n'
        '[saves]\nretroarch_install = "native"\n'
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "retroarch:   native" in result.output
    assert "selected via [saves].retroarch_install" in result.output
    assert "AMBIGUOUS" not in result.output


def test_status_warns_on_configured_install_mismatch(tmp_path: Path, monkeypatch) -> None:
    """Configured value present but no install matches → warn + fall back."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_ra_cfg(tmp_path, ".config/retroarch", "")  # only native

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[romm]\nurl = "{BASE_URL}"\napi_key = "rmm_abcdef0123456789"\n'
        '[destination]\npreset = "esde-native"\n'
        '[sync]\ncollections = ["Steam Deck"]\n'
        '[saves]\nretroarch_install = "retrodeck-flatpak"\n'
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    assert "no discovered install matches" in result.output
    # Falls back to auto-selection, which finds native.
    assert "retroarch:   native" in result.output


def test_status_picks_install_with_active_saves_when_other_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Two installs detected, only one has saves — that's the active one."""
    monkeypatch.setenv("HOME", str(tmp_path))

    # RetroDECK exists but no saves
    _write_ra_cfg(
        tmp_path,
        ".var/app/net.retrodeck.retrodeck/config/retroarch",
        f'savefile_directory = "{tmp_path / "retrodeck/saves"}"\n',
    )

    # Native install with actual saves
    native_saves = tmp_path / ".config/retroarch/saves"
    native_saves.mkdir(parents=True)
    (native_saves / "Mario.srm").write_bytes(b"x")
    _write_ra_cfg(tmp_path, ".config/retroarch", "")

    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "retroarch:   native" in result.output
    assert "out of 2 detected" in result.output
    assert "AMBIGUOUS" not in result.output


# ---------------------------------------------------------------------------
# Dolphin install discovery in status (v3)
# ---------------------------------------------------------------------------


_RD_DOLPHIN_SAVES = "retrodeck/saves/gc/dolphin"
_RD_DOLPHIN_CONFIG = ".var/app/net.retrodeck.retrodeck/config/dolphin-emu/Dolphin.ini"
_NATIVE_DOLPHIN_SAVES = ".local/share/dolphin-emu/GC"
_NATIVE_DOLPHIN_CONFIG = ".local/share/dolphin-emu/Config/Dolphin.ini"


def _setup_dolphin(
    home: Path,
    saves_rel: str,
    config_rel: str,
    *,
    ini_body: str = "",
    region: str | None = None,
) -> Path:
    """Set up a Dolphin install: create saves_root and write Dolphin.ini.
    If `region` is given, plant a `.gci` under `<saves_root>/<region>/Card A/`."""
    saves_root = home / saves_rel
    saves_root.mkdir(parents=True, exist_ok=True)
    config_path = home / config_rel
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(ini_body)
    if region is not None:
        card = saves_root / region / "Card A"
        card.mkdir(parents=True, exist_ok=True)
        (card / "01-GM8E-Test.gci").write_bytes(b"x" * 8256)
    return saves_root


def test_status_reports_no_dolphin_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "dolphin:     (not detected)" in result.output


def test_status_reports_retrodeck_dolphin_in_gci_folder_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    saves = _setup_dolphin(
        tmp_path,
        _RD_DOLPHIN_SAVES,
        _RD_DOLPHIN_CONFIG,
        ini_body="[Core]\nSlotA = 8\n",
    )
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "dolphin:     retrodeck-flatpak" in result.output
    assert str(saves) in result.output
    assert "gci_folder" in result.output


def test_status_warns_when_dolphin_in_raw_memcard_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup_dolphin(
        tmp_path,
        _NATIVE_DOLPHIN_SAVES,
        _NATIVE_DOLPHIN_CONFIG,
        ini_body="[Core]\nSlotA = 1\n",
    )
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "dolphin:     native" in result.output
    assert "RAW MEMCARD MODE" in result.output


def test_status_flags_dolphin_ambiguity_when_multiple_installs_have_saves(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup_dolphin(
        tmp_path,
        _RD_DOLPHIN_SAVES,
        _RD_DOLPHIN_CONFIG,
        ini_body="[Core]\nSlotA = 8\n",
        region="US",  # RetroDECK uses 2-letter regions
    )
    _setup_dolphin(
        tmp_path,
        _NATIVE_DOLPHIN_SAVES,
        _NATIVE_DOLPHIN_CONFIG,
        ini_body="[Core]\nSlotA = 8\n",
        region="USA",  # native uses 3-letter
    )
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "dolphin:     AMBIGUOUS" in result.output
    assert "[saves.dolphin_install]" in result.output


def test_status_honors_configured_dolphin_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup_dolphin(
        tmp_path,
        _RD_DOLPHIN_SAVES,
        _RD_DOLPHIN_CONFIG,
        ini_body="[Core]\nSlotA = 8\n",
        region="US",
    )
    _setup_dolphin(
        tmp_path,
        _NATIVE_DOLPHIN_SAVES,
        _NATIVE_DOLPHIN_CONFIG,
        ini_body="[Core]\nSlotA = 8\n",
    )

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[romm]\nurl = "{BASE_URL}"\napi_key = "rmm_abcdef0123456789"\n'
        '[destination]\npreset = "esde-native"\n'
        '[sync]\ncollections = ["Steam Deck"]\n'
        '[saves]\ndolphin_install = "native"\n'
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "dolphin:     native" in result.output
    assert "selected via [saves].dolphin_install" in result.output


# ---------------------------------------------------------------------------
# Launch-hook drift surfacing in `ferry status`
# ---------------------------------------------------------------------------


def _bundled_xml(extra: str = "") -> str:
    return (
        '<?xml version="1.0"?>\n<systemList>\n'
        "  <system>\n"
        "    <name>gba</name>\n"
        "    <fullname>Game Boy Advance</fullname>\n"
        "    <path>%ROMPATH%/gba</path>\n"
        f"    <extension>.gba .GBA{extra}</extension>\n"
        "    <command>retroarch -L core %ROM%</command>\n"
        "    <platform>gba</platform>\n"
        "    <theme>gba</theme>\n"
        "  </system>\n"
        "</systemList>\n"
    )


@pytest.fixture
def patched_native_profiles_for_status(monkeypatch, tmp_path: Path) -> Path:
    bundled = (
        Path.home()
        / "fake-usr"
        / "share"
        / "es-de"
        / "resources"
        / "systems"
        / "linux"
        / "es_systems.xml"
    )
    bundled.parent.mkdir(parents=True)
    bundled.write_text(_bundled_xml())
    fake_profiles = (
        (
            "retrodeck-flatpak",
            tmp_path / "no-such-flatpak",
            ".var/app/net.retrodeck.retrodeck/config/ES-DE/custom_systems/es_systems.xml",
        ),
        (
            "native",
            bundled,
            "ES-DE/custom_systems/es_systems.xml",
        ),
    )
    monkeypatch.setattr("ferry.adapters.esde_paths._PROFILES", fake_profiles)
    return bundled


def test_status_hooks_not_installed_when_no_snapshot_no_block(
    tmp_path: Path, patched_native_profiles_for_status: Path
) -> None:
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "hooks:     not installed" in result.output


def test_status_hooks_clean_after_install(
    tmp_path: Path, patched_native_profiles_for_status: Path
) -> None:
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "hooks:     ✓ installed and in sync" in result.output


def test_status_hooks_upstream_drift(
    tmp_path: Path, patched_native_profiles_for_status: Path
) -> None:
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    # Simulate upstream change.
    patched_native_profiles_for_status.write_text(_bundled_xml(extra=" .added"))

    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "⚠ bundled changed" in result.output
    assert "re-run `ferry install-launch-hooks`" in result.output


def test_status_hooks_local_drift(tmp_path: Path, patched_native_profiles_for_status: Path) -> None:
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    custom = Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml"
    text = custom.read_text()
    custom.write_text(text.replace(".gba .GBA", ".gba .GBA .foo"))

    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "⚠ managed block edited locally" in result.output
    assert "--force" in result.output


def test_status_hooks_both_drift_dimensions(
    tmp_path: Path, patched_native_profiles_for_status: Path
) -> None:
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    patched_native_profiles_for_status.write_text(_bundled_xml(extra=" .added"))
    custom = Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml"
    text = custom.read_text()
    custom.write_text(text.replace(".gba .GBA", ".gba .GBA .foo"))

    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "bundled changed AND managed block edited" in result.output


def test_status_hooks_block_present_but_no_snapshot(
    tmp_path: Path, patched_native_profiles_for_status: Path
) -> None:
    """Pre-ck3c upgrade case: managed block exists but no snapshot file."""
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    # Remove the snapshot file but leave block in place.
    from ferry.services.launch_hooks import default_snapshot_path

    default_snapshot_path().unlink()

    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "managed block present but no drift snapshot" in result.output


# ---------------------------------------------------------------------------
# [orphan saves] section
# ---------------------------------------------------------------------------


def test_status_orphan_saves_clean_when_no_unmatched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    saves_dir = tmp_path / ".config/retroarch/saves"
    saves_dir.mkdir(parents=True)
    _write_ra_cfg(tmp_path, ".config/retroarch", 'sort_savefiles_enable = "true"\n')
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "[orphan saves]" in result.output
    assert "✓ all local saves match a tracked ROM" in result.output


def test_status_orphan_saves_lists_real_orphans(tmp_path: Path, monkeypatch) -> None:
    """RA save with no matching tracked ROM → listed as a real orphan."""
    monkeypatch.setenv("HOME", str(tmp_path))
    saves_dir = tmp_path / ".config/retroarch/saves"
    saves_dir.mkdir(parents=True)
    (saves_dir / "Frogger.srm").write_bytes(b"x")
    _write_ra_cfg(tmp_path, ".config/retroarch", 'sort_savefiles_enable = "true"\n')
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "1 unmatched" in result.output
    assert "Frogger.srm" in result.output


def test_status_orphan_saves_hides_retrodeck_backups_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    """RetroDECK `_YYYYMMDD_HHMMSS` SRMs are classified as backup-noise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    saves_dir = tmp_path / ".config/retroarch/saves"
    saves_dir.mkdir(parents=True)
    (saves_dir / "Frogger.srm").write_bytes(b"x")
    (saves_dir / "Super Mario 64 (USA)_20260424_054107.srm").write_bytes(b"y")
    _write_ra_cfg(tmp_path, ".config/retroarch", 'sort_savefiles_enable = "true"\n')
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "1 unmatched" in result.output  # only the real orphan counts as unmatched
    assert "1 RetroDECK backup" in result.output
    assert "Frogger.srm" in result.output
    assert "Super Mario 64 (USA)_20260424_054107.srm" not in result.output
    assert "pass --show-all" in result.output


def test_status_orphan_saves_shows_backups_with_show_all(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    saves_dir = tmp_path / ".config/retroarch/saves"
    saves_dir.mkdir(parents=True)
    (saves_dir / "Super Mario 64 (USA)_20260424_054107.srm").write_bytes(b"y")
    _write_ra_cfg(tmp_path, ".config/retroarch", 'sort_savefiles_enable = "true"\n')
    cfg = write_config(tmp_path / "config.toml")

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status", "--show-all"], env={})
    assert result.exit_code == 0, result.output
    assert "Super Mario 64 (USA)_20260424_054107.srm" in result.output
    assert "RetroDECK backup" in result.output


def test_status_orphan_saves_no_install_detected(tmp_path: Path, monkeypatch) -> None:
    """Without an RA install at all, the section says so but doesn't fail."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "status"], env={})
    assert result.exit_code == 0, result.output
    assert "[orphan saves]" in result.output
    assert "(no install detected)" in result.output
