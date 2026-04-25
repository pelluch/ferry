from pathlib import Path

import httpx
import respx
from click.testing import CliRunner

from ferry import __version__
from ferry.cli import app


def write_config(path: Path, *, api_key: str = "rmm_abcdef0123456789") -> Path:
    path.write_text(f'[romm]\nurl = "https://romm.example.tld"\napi_key = "{api_key}"\n')
    return path


def mock_romm_endpoints(
    *,
    me: dict | None = None,
    collections: list | None = None,
    me_status: int = 200,
    collections_status: int = 200,
) -> None:
    """Register respx routes for the two endpoints `ferry ping` hits."""
    me_payload = (
        me
        if me is not None
        else {
            "id": 1,
            "username": "pablo",
            "oauth_scopes": ["roms.read", "collections.read"],
        }
    )
    cols_payload = (
        collections
        if collections is not None
        else [
            {"id": 10, "name": "Steam Deck", "rom_count": 234},
            {"id": 11, "name": "Quick Picks", "rom_count": 12},
        ]
    )
    respx.get("https://romm.example.tld/api/users/me").mock(
        return_value=httpx.Response(me_status, json=me_payload)
    )
    respx.get("https://romm.example.tld/api/collections").mock(
        return_value=httpx.Response(collections_status, json=cols_payload)
    )


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


@respx.mock
def test_ping_reports_user_collections_and_masked_key(tmp_path: Path) -> None:
    mock_romm_endpoints()
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "ping"], env={"FERRY_ROMM_API_KEY": ""})
    assert result.exit_code == 0, result.output
    assert __version__ in result.output
    assert "https://romm.example.tld" in result.output
    assert "rmm_…789" in result.output  # masked
    assert "rmm_abcdef0123456789" not in result.output  # never the full key
    assert "config.toml" in result.output  # source label
    assert "connected as pablo" in result.output
    assert "Steam Deck" in result.output
    assert "Quick Picks" in result.output
    assert "234 ROMs" in result.output


@respx.mock
def test_ping_env_var_takes_precedence(tmp_path: Path) -> None:
    mock_romm_endpoints()
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


@respx.mock
def test_ping_auth_error_surfaces_friendly_hint(tmp_path: Path) -> None:
    mock_romm_endpoints(me_status=401, me={"detail": "Invalid token"})
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "ping"], env={})
    assert result.exit_code != 0
    assert "401" in result.output
    assert "API key" in result.output  # the friendly hint we add for auth errors


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
