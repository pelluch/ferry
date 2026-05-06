"""End-to-end CLI tests for `ferry install-launch-hooks` /
`uninstall-launch-hooks`, focused on drift detection + snapshot handling.

The pure XML-generation logic (managed block content) is exercised
indirectly here and would benefit from dedicated tests on
services.launch_hooks.render_managed_block; that module is unchanged
in this checkpoint so we don't add coverage for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ferry.cli import app
from ferry.services.launch_hooks import (
    default_snapshot_path,
    default_wrapper_path,
    detect_drift,
    extract_managed_block,
    read_snapshot,
)

BASE_URL = "https://romm.example.tld"

# A minimal bundled es_systems.xml — one system with a single command.
# The managed-block generator wraps each <command> with pre/post calls;
# we only care about presence/absence + drift detection here, not the
# exact wrapper text.
_BUNDLED_XML = (
    '<?xml version="1.0"?>\n'
    "<systemList>\n"
    "  <system>\n"
    "    <name>gba</name>\n"
    "    <fullname>Game Boy Advance</fullname>\n"
    "    <path>%ROMPATH%/gba</path>\n"
    "    <extension>.gba .GBA</extension>\n"
    "    <command>retroarch -L core %ROM%</command>\n"
    "    <platform>gba</platform>\n"
    "    <theme>gba</theme>\n"
    "  </system>\n"
    "</systemList>\n"
)


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[romm]\nurl = "' + BASE_URL + '"\napi_key = "rmm_abcdef0123456789"\n\n'
        '[destination]\npreset = "esde-native"\n\n'
        '[sync]\ncollections = ["Steam Deck"]\n'
    )
    return cfg


def _setup_native_esde(home: Path) -> Path:
    """Plant a native-ES-DE bundled file under a fake /usr tree, return its path."""
    bundled = home / "fake-usr/share/es-de/resources/systems/linux/es_systems.xml"
    bundled.parent.mkdir(parents=True)
    bundled.write_text(_BUNDLED_XML)
    return bundled


@pytest.fixture
def patched_native_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect esde discovery's native-bundled probe to a tmp_path tree.

    The retrodeck-flatpak probe stays as-is (points at real /var/lib/flatpak,
    which doesn't exist on most CI hosts), so discovery only finds the
    native install — sufficient for these tests.
    """
    bundled = _setup_native_esde(Path.home())
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


def test_install_writes_wrapper_managed_block_and_snapshot(
    tmp_path: Path, patched_native_profiles: Path
) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})
    assert result.exit_code == 0, result.output

    wrapper = default_wrapper_path()
    snapshot_path = default_snapshot_path()
    custom = Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml"
    assert wrapper.is_file()
    assert custom.is_file()
    assert snapshot_path.is_file()

    snapshot = read_snapshot(snapshot_path)
    assert snapshot is not None
    assert snapshot.bundled_path == patched_native_profiles
    assert snapshot.custom_systems_path == custom

    # Right after install, drift must be clean.
    drift = detect_drift(snapshot)
    assert drift.is_clean

    # User-visible echoes
    assert "wrote wrapper script" in result.output
    assert "system(s) wrapped" in result.output
    assert "wrote drift snapshot" in result.output


def test_install_dry_run_writes_nothing(tmp_path: Path, patched_native_profiles: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "install-launch-hooks", "--dry-run"], env={})
    assert result.exit_code == 0, result.output

    assert not default_wrapper_path().is_file()
    assert not default_snapshot_path().is_file()
    assert not (Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml").is_file()

    assert "Would write wrapper script" in result.output
    assert "Would write snapshot" in result.output
    assert "Drift status:" in result.output
    assert "no snapshot — clean install" in result.output
    assert "(dry run — no files modified)" in result.output


def test_install_dry_run_after_first_install_reports_clean(
    tmp_path: Path, patched_native_profiles: Path
) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    result = runner.invoke(app, ["--config", str(cfg), "install-launch-hooks", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    assert "Drift status: ✓ snapshot matches disk" in result.output


def test_install_refuses_when_local_drift_without_force(
    tmp_path: Path, patched_native_profiles: Path
) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    # Simulate a hand-edit inside the managed block.
    custom = Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml"
    text = custom.read_text()
    custom.write_text(
        text.replace(
            "<extension>.gba .GBA</extension>",
            "<extension>.gba .GBA .foo</extension>",
        )
    )

    result = runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})
    assert result.exit_code != 0
    assert "edited since" in result.output
    assert "--force" in result.output

    # Edit must still be on disk (we refused to clobber).
    assert ".foo" in custom.read_text()


def test_install_with_force_clobbers_local_edits(
    tmp_path: Path, patched_native_profiles: Path
) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    custom = Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml"
    text = custom.read_text()
    custom.write_text(text.replace(".gba .GBA", ".gba .GBA .foo"))

    result = runner.invoke(app, ["--config", str(cfg), "install-launch-hooks", "--force"], env={})
    assert result.exit_code == 0, result.output
    assert ".foo" not in custom.read_text()
    # Snapshot SHA must be updated to the new (clobbered) state.
    snap = read_snapshot(default_snapshot_path())
    assert snap is not None
    assert detect_drift(snap).is_clean


def test_install_dry_run_flags_local_drift_but_does_not_error(
    tmp_path: Path, patched_native_profiles: Path
) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    custom = Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml"
    text = custom.read_text()
    custom.write_text(text.replace(".gba .GBA", ".gba .GBA .foo"))

    result = runner.invoke(app, ["--config", str(cfg), "install-launch-hooks", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    assert "REFUSE to overwrite without --force" in result.output


def test_install_after_upstream_drift_writes_fresh_snapshot(
    tmp_path: Path, patched_native_profiles: Path
) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})

    # Simulate a RetroDECK update that touched the bundled file.
    patched_native_profiles.write_text(
        _BUNDLED_XML.replace("Game Boy Advance", "Game Boy Advance (updated)")
    )
    snap_before = read_snapshot(default_snapshot_path())
    assert snap_before is not None
    assert detect_drift(snap_before).upstream_drift

    result = runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})
    assert result.exit_code == 0, result.output

    snap_after = read_snapshot(default_snapshot_path())
    assert snap_after is not None
    assert snap_after.bundled_sha256 != snap_before.bundled_sha256
    assert detect_drift(snap_after).is_clean


def test_uninstall_removes_snapshot(tmp_path: Path, patched_native_profiles: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["--config", str(cfg), "install-launch-hooks"], env={})
    assert default_snapshot_path().is_file()

    result = runner.invoke(app, ["--config", str(cfg), "uninstall-launch-hooks"], env={})
    assert result.exit_code == 0, result.output

    assert not default_snapshot_path().is_file()
    assert not default_wrapper_path().is_file()
    custom = Path.home() / "ES-DE" / "custom_systems" / "es_systems.xml"
    assert extract_managed_block(custom) is None
    assert "removed drift snapshot" in result.output


def test_uninstall_with_no_snapshot_says_nothing_to_remove(
    tmp_path: Path, patched_native_profiles: Path
) -> None:
    cfg = _write_config(tmp_path)
    # Plant a native install but never run install-launch-hooks.
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "uninstall-launch-hooks"], env={})
    assert result.exit_code == 0, result.output
    assert "drift snapshot" in result.output
    assert "nothing to remove" in result.output
