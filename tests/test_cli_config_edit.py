"""Tests for `ferry config edit`.

Each test uses a fake editor passed via $EDITOR. `true` exits 0 without
touching the file (good for "did we open it?" checks); a tiny shell snippet
appended via `EDITOR='sh -c ... --'` simulates a real edit.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ferry.cli import app


def test_edit_creates_template_when_config_is_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(cfg), "config", "edit"],
        env={"EDITOR": "true"},
    )
    assert result.exit_code == 0, result.output
    assert cfg.exists()
    content = cfg.read_text()
    assert "[romm]" in content
    assert "api_key" in content
    assert f"created template at {cfg}" in result.output


def test_edit_creates_parent_dir_when_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "nested" / "deep" / "config.toml"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(cfg), "config", "edit"],
        env={"EDITOR": "true"},
    )
    assert result.exit_code == 0, result.output
    assert cfg.exists()


def test_edit_leaves_existing_config_untouched_when_editor_is_noop(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[romm]\nurl = "https://romm.example.tld"\napi_key = "rmm_abcdef0123456789"\n')
    original = cfg.read_text()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(cfg), "config", "edit"],
        env={"EDITOR": "true"},
    )
    assert result.exit_code == 0, result.output
    assert cfg.read_text() == original
    # Existing files should NOT print a "created template" line.
    assert "created template" not in result.output


def test_edit_warns_when_post_edit_config_is_invalid(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[romm]\nurl = "https://romm.example.tld"\napi_key = "rmm_abcdef0123456789"\n')

    # Editor: nuke the api_key line so post-edit validation fails.
    fake_editor = tmp_path / "edit.sh"
    fake_editor.write_text("#!/bin/sh\nsed -i '/api_key/d' \"$1\"\n")
    fake_editor.chmod(0o755)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(cfg), "config", "edit"],
        env={"EDITOR": str(fake_editor)},
    )
    assert result.exit_code == 0, result.output
    assert "api_key" not in cfg.read_text()
    assert "warning" in result.stderr
    assert "missing RomM API key" in result.stderr


def test_edit_template_warning_mentions_api_key_hint(tmp_path: Path) -> None:
    """When ferry creates the template AND the user saves it without filling in
    [romm].api_key, the warning should specifically nudge them toward it."""
    cfg = tmp_path / "config.toml"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(cfg), "config", "edit"],
        env={"EDITOR": "true"},  # editor leaves stub unchanged
    )
    assert result.exit_code == 0, result.output
    assert "warning" in result.stderr
    assert "FERRY_ROMM_API_KEY" in result.stderr


def test_edit_uses_default_path_when_no_explicit_arg(tmp_path: Path, monkeypatch) -> None:
    """No --config flag → falls back to $XDG_CONFIG_HOME/ferry/config.toml.

    The autouse `_isolated_home` fixture sets HOME to a tmp dir and unsets
    XDG_CONFIG_HOME, so the resolved default is `<isolated_home>/.config/ferry/config.toml`.
    Override HOME to a known location so we can assert the path explicitly.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".config" / "ferry" / "config.toml"

    runner = CliRunner()
    result = runner.invoke(app, ["config", "edit"], env={"EDITOR": "true"})
    assert result.exit_code == 0, result.output
    assert expected.exists()


def test_edit_respects_ferry_config_env(tmp_path: Path) -> None:
    """No --config flag, FERRY_CONFIG set → uses the env-supplied path."""
    target = tmp_path / "via-env.toml"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["config", "edit"],
        env={"EDITOR": "true", "FERRY_CONFIG": str(target)},
    )
    assert result.exit_code == 0, result.output
    assert target.exists()


def test_config_edit_listed_in_top_level_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "config" in result.output


def test_config_subcommand_help_lists_edit() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0
    assert "edit" in result.output
