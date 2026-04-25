from pathlib import Path

from click.testing import CliRunner

from ferry.adapters.detect import detect_candidates
from ferry.cli import app

# ---------------------------------------------------------------------------
# detect_candidates() — pure detection logic
# ---------------------------------------------------------------------------


def test_empty_home_yields_no_candidates(tmp_path: Path) -> None:
    assert detect_candidates(home=tmp_path) == []


def test_detects_retrodeck_flatpak_from_roms_dir(tmp_path: Path) -> None:
    (tmp_path / "retrodeck/roms").mkdir(parents=True)
    candidates = detect_candidates(home=tmp_path)
    assert len(candidates) == 1
    assert candidates[0].preset == "retrodeck-flatpak"
    assert candidates[0].roms_base == tmp_path / "retrodeck/roms"
    assert candidates[0].bios_base == tmp_path / "retrodeck/bios"
    assert any("ROM dir" in s for s in candidates[0].signals)


def test_detects_retrodeck_flatpak_data_dir_alone(tmp_path: Path) -> None:
    """Even without the ROM dir, the flatpak app data dir is enough signal."""
    (tmp_path / ".var/app/net.retrodeck.retrodeck").mkdir(parents=True)
    candidates = detect_candidates(home=tmp_path)
    assert len(candidates) == 1
    assert candidates[0].preset == "retrodeck-flatpak"
    assert any("flatpak data dir" in s for s in candidates[0].signals)


def test_detects_emudeck(tmp_path: Path) -> None:
    (tmp_path / "Emulation/roms").mkdir(parents=True)
    (tmp_path / "Emulation/bios").mkdir(parents=True)
    candidates = detect_candidates(home=tmp_path)
    assert len(candidates) == 1
    assert candidates[0].preset == "emudeck"
    assert len(candidates[0].signals) == 2  # both ROM and BIOS dirs flagged


def test_esde_native_requires_both_roms_and_userdata(tmp_path: Path) -> None:
    (tmp_path / "ROMs").mkdir()
    # No userdata dir yet → not enough signal.
    assert detect_candidates(home=tmp_path) == []

    (tmp_path / "ES-DE/settings").mkdir(parents=True)
    (tmp_path / "ES-DE/settings/es_settings.xml").touch()
    candidates = detect_candidates(home=tmp_path)
    assert any(c.preset == "esde-native" for c in candidates)


def test_esde_native_has_no_bios_base(tmp_path: Path) -> None:
    (tmp_path / "ROMs").mkdir()
    (tmp_path / "ES-DE/settings").mkdir(parents=True)
    (tmp_path / "ES-DE/settings/es_settings.xml").touch()
    candidates = detect_candidates(home=tmp_path)
    native = next(c for c in candidates if c.preset == "esde-native")
    assert native.bios_base is None


def test_esde_flatpak_requires_both_roms_and_app_data(tmp_path: Path) -> None:
    (tmp_path / "ROMs").mkdir()
    assert detect_candidates(home=tmp_path) == []

    (tmp_path / ".var/app/org.es_de.frontend").mkdir(parents=True)
    candidates = detect_candidates(home=tmp_path)
    assert any(c.preset == "esde-flatpak" for c in candidates)


def test_esde_native_and_flatpak_can_coexist(tmp_path: Path) -> None:
    """Both layouts present → both candidates listed; user disambiguates."""
    (tmp_path / "ROMs").mkdir()
    (tmp_path / "ES-DE/settings").mkdir(parents=True)
    (tmp_path / "ES-DE/settings/es_settings.xml").touch()
    (tmp_path / ".var/app/org.es_de.frontend").mkdir(parents=True)
    presets = {c.preset for c in detect_candidates(home=tmp_path)}
    assert presets == {"esde-native", "esde-flatpak"}


def test_multiple_unrelated_presets_all_listed(tmp_path: Path) -> None:
    (tmp_path / "retrodeck/roms").mkdir(parents=True)
    (tmp_path / "Emulation/roms").mkdir(parents=True)
    presets = [c.preset for c in detect_candidates(home=tmp_path)]
    assert presets == ["retrodeck-flatpak", "emudeck"]  # order is stable


def test_loose_roms_dir_alone_not_enough_for_esde(tmp_path: Path) -> None:
    """Just having `~/ROMs` shouldn't trigger ES-DE detection."""
    (tmp_path / "ROMs").mkdir()
    # No ES-DE userdata, no flatpak data → no ES-DE candidate.
    presets = {c.preset for c in detect_candidates(home=tmp_path)}
    assert "esde-native" not in presets
    assert "esde-flatpak" not in presets


# ---------------------------------------------------------------------------
# `ferry detect` CLI command
# ---------------------------------------------------------------------------


def test_detect_no_candidates_emits_explicit_paths_template(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["detect"], env={})
    assert result.exit_code == 0, result.output
    assert "No candidates detected" in result.output
    assert "[destination]" in result.output
    assert 'roms_base = "/path/to/roms"' in result.output
    # Lists known presets so the user can pick one anyway if they know they have it.
    assert "retrodeck-flatpak" in result.output
    assert "esde-native" in result.output


def test_detect_single_candidate_emits_paste_ready_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "retrodeck/roms").mkdir(parents=True)
    (tmp_path / "retrodeck/bios").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(app, ["detect"], env={})
    assert result.exit_code == 0, result.output
    assert "Found 1 candidate" in result.output
    assert "retrodeck-flatpak" in result.output
    assert "(exists)" in result.output
    # The closing snippet should be ready to paste:
    assert 'preset = "retrodeck-flatpak"' in result.output


def test_detect_multiple_candidates_asks_user_to_pick(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "retrodeck/roms").mkdir(parents=True)
    (tmp_path / "Emulation/roms").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(app, ["detect"], env={})
    assert result.exit_code == 0, result.output
    assert "Found 2 candidates" in result.output
    assert "retrodeck-flatpak" in result.output
    assert "emudeck" in result.output
    assert "Pick one" in result.output
    # No specific preset committed when user must choose.
    assert 'preset = "<choice>"' in result.output


def test_detect_esde_native_shows_per_emulator_bios(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "ROMs").mkdir()
    (tmp_path / "ES-DE/settings").mkdir(parents=True)
    (tmp_path / "ES-DE/settings/es_settings.xml").touch()
    runner = CliRunner()
    result = runner.invoke(app, ["detect"], env={})
    assert result.exit_code == 0, result.output
    assert "esde-native" in result.output
    assert "per-emulator" in result.output
