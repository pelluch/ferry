"""End-to-end CLI tests for `ferry sync --dry-run`."""

from pathlib import Path

import httpx
import respx
from click.testing import CliRunner

from ferry.cli import app

BASE_URL = "https://romm.example.tld"


def write_config(
    cfg: Path,
    *,
    collection: str = "Steam Deck",
    include_destination: bool = True,
) -> Path:
    parts = [
        "[romm]",
        f'url = "{BASE_URL}"',
        'api_key = "rmm_abcdef0123456789"',
    ]
    if include_destination:
        parts += ["", "[destination]", 'preset = "esde-native"']
    parts += ["", "[sync]", f'collection = "{collection}"']
    cfg.write_text("\n".join(parts) + "\n")
    return cfg


def mock_endpoints(
    *,
    collections: list[dict],
    rom_items: list[dict],
) -> None:
    respx.get(f"{BASE_URL}/api/collections").mock(
        return_value=httpx.Response(200, json=collections)
    )
    respx.get(f"{BASE_URL}/api/roms").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": rom_items,
                "total": len(rom_items),
                "limit": 10000,
                "offset": 0,
            },
        )
    )


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_sync_without_dry_run_refuses(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code != 0
    assert "later checkpoint" in result.output


def test_sync_without_sync_section_errors(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[romm]\nurl = "{BASE_URL}"\napi_key = "rmm_abcdef0123456789"\n')
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code != 0
    assert "[sync].collection" in result.output


def test_sync_without_destination_errors(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml", include_destination=False)
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code != 0
    assert "[destination]" in result.output


# ---------------------------------------------------------------------------
# Collection resolution
# ---------------------------------------------------------------------------


@respx.mock
def test_unknown_collection_lists_available(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml", collection="Atlantis")
    mock_endpoints(
        collections=[{"id": 1, "name": "Steam Deck"}, {"id": 2, "name": "Quick Picks"}],
        rom_items=[],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code != 0
    assert "Atlantis" in result.output
    assert "Steam Deck" in result.output  # available list
    assert "Quick Picks" in result.output


@respx.mock
def test_ambiguous_collection_name_errors(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml", collection="Dupes")
    mock_endpoints(
        collections=[{"id": 1, "name": "Dupes"}, {"id": 2, "name": "Dupes"}],
        rom_items=[],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code != 0
    assert "multiple collections" in result.output


# ---------------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------------


@respx.mock
def test_dry_run_shows_adds_for_first_run(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml", collection="Steam Deck")
    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[
            {
                "id": 101,
                "name": "Pikmin",
                "platform_slug": "gc",
                "updated_at": "2026-04-25T12:00:00Z",
                "fs_name": "Pikmin.zip",
            },
            {
                "id": 102,
                "name": "Custom Robo",
                "platform_slug": "gc",
                "updated_at": "2026-04-25T12:00:00Z",
                "fs_name": "Custom Robo.zip",
            },
        ],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    assert "resolved collection: Steam Deck" in result.output
    assert "2 ROM(s) returned" in result.output
    assert "Add:        2" in result.output
    assert "Update:     0" in result.output
    assert "Delete:     0" in result.output
    assert "+ Custom Robo (gc, rom_id=102)" in result.output
    assert "+ Pikmin (gc, rom_id=101)" in result.output
    assert "(dry run — no files modified)" in result.output


@respx.mock
def test_dry_run_says_nothing_to_do_when_no_changes(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml")
    mock_endpoints(collections=[{"id": 6, "name": "Steam Deck"}], rom_items=[])
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    assert "Nothing to do" in result.output


@respx.mock
def test_dry_run_truncates_long_section_without_full_flag(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml")
    items = [
        {
            "id": i,
            "name": f"Game {i:03d}",
            "platform_slug": "gc",
            "updated_at": "2026-04-25T12:00:00Z",
            "fs_name": f"Game{i}.zip",
        }
        for i in range(50)
    ]
    mock_endpoints(collections=[{"id": 6, "name": "Steam Deck"}], rom_items=items)
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    assert "Add:        50" in result.output
    assert "and 30 more" in result.output  # 50 items, default cap 20


@respx.mock
def test_full_flag_shows_every_entry(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "config.toml")
    items = [
        {
            "id": i,
            "name": f"Game {i:03d}",
            "platform_slug": "gc",
            "updated_at": "2026-04-25T12:00:00Z",
            "fs_name": f"Game{i}.zip",
        }
        for i in range(30)
    ]
    mock_endpoints(collections=[{"id": 6, "name": "Steam Deck"}], rom_items=items)
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run", "--full"], env={})
    assert result.exit_code == 0, result.output
    assert "and 10 more" not in result.output  # no truncation
    # Every game name should appear.
    for i in range(30):
        assert f"Game {i:03d}" in result.output
