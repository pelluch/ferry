from pathlib import Path

from click.testing import CliRunner

from ferry import __version__
from ferry.cli import app


def write_config(path: Path, *, api_key: str = "rmm_abcdef0123456789") -> Path:
    path.write_text(f'[romm]\nurl = "https://romm.example.tld"\napi_key = "{api_key}"\n')
    return path


def test_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ferry" in result.output
    assert "ping" in result.output
    assert "--config" in result.output


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_ping_with_config_reports_loaded_values(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "ping"], env={"FERRY_ROMM_API_KEY": ""})
    assert result.exit_code == 0, result.output
    assert __version__ in result.output
    assert "https://romm.example.tld" in result.output
    assert "rmm_…789" in result.output  # masked
    assert "rmm_abcdef0123456789" not in result.output  # never the full key
    assert "config.toml" in result.output  # source label
    assert "checkpoint 3" in result.output


def test_ping_env_var_takes_precedence(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml", api_key="rmm_from_toml_abc")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(cfg), "ping"],
        env={"FERRY_ROMM_API_KEY": "rmm_from_env_xyz"},
    )
    assert result.exit_code == 0, result.output
    assert "rmm_…xyz" in result.output
    assert "FERRY_ROMM_API_KEY env var" in result.output


def test_ping_missing_config_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.toml"), "ping"], env={})
    assert result.exit_code != 0
    assert "not found" in result.output


def test_ping_invalid_config_exits_nonzero(tmp_path: Path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text('[romm]\nurl = "not-a-url"\napi_key = "rmm_xyz1234567"\n')
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(bad), "ping"], env={})
    assert result.exit_code != 0
    assert "http://" in result.output
