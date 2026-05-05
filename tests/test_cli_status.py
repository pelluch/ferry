"""Tests for `ferry status` — read-only state-vs-disk introspection."""

from pathlib import Path

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
        write_sidecar(primary, rom)

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
