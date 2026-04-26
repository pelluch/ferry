"""End-to-end CLI tests for `ferry sync` (both --dry-run and execution paths)."""

import hashlib
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import respx
from click.testing import CliRunner

from ferry.adapters.state_store import load_state
from ferry.cli import app

BASE_URL = "https://romm.example.tld"


def write_config(
    cfg: Path,
    *,
    collection: str = "Steam Deck",
    include_destination: bool = True,
    roms_base: Path | None = None,
    transforms_section: str | None = None,
) -> Path:
    parts = [
        "[romm]",
        f'url = "{BASE_URL}"',
        'api_key = "rmm_abcdef0123456789"',
    ]
    if include_destination:
        if roms_base is None:
            parts += ["", "[destination]", 'preset = "esde-native"']
        else:
            parts += ["", "[destination]", f'roms_base = "{roms_base}"']
    parts += ["", "[sync]", f'collection = "{collection}"']
    if transforms_section:
        parts += ["", transforms_section]
    cfg.write_text("\n".join(parts) + "\n")
    return cfg


def make_zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


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
def test_dry_run_shows_adds_with_resolved_paths_and_pipeline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    cfg = write_config(
        tmp_path / "config.toml",
        collection="Steam Deck",
        roms_base=roms_base,
        transforms_section='[transforms.gc]\npipeline = ["unzip"]',
    )
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
    # New format: name + slug → resolved path + pipeline summary.
    assert f"+ Pikmin (gc, rom_id=101) → {roms_base}/gc/Pikmin.zip [unzip]" in result.output
    assert (
        f"+ Custom Robo (gc, rom_id=102) → {roms_base}/gc/Custom Robo.zip [unzip]" in result.output
    )
    assert "(dry run — no files modified)" in result.output


@respx.mock
def test_dry_run_uses_platform_mapping_for_non_canonical_slug(tmp_path: Path, monkeypatch) -> None:
    """RomM's `game-boy-advance` slug should resolve to ES-DE's `gba` directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)
    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[
            {
                "id": 1,
                "name": "Advance Wars",
                "platform_slug": "game-boy-advance",
                "updated_at": "2026-04-25T12:00:00Z",
                "fs_name": "Advance Wars.zip",
            }
        ],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    # Resolved to /ROMs/gba/ — not /ROMs/game-boy-advance/.
    assert f"{roms_base}/gba/Advance Wars.zip" in result.output
    assert "game-boy-advance/" not in result.output


@respx.mock
def test_dry_run_passthrough_format_when_no_pipeline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)  # no transforms_section
    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[
            {
                "id": 1,
                "name": "Direct ISO",
                "platform_slug": "gc",
                "updated_at": "2026-04-25T12:00:00Z",
                "fs_name": "Direct.iso",
            }
        ],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    # No `[unzip]` suffix when the pipeline is empty.
    assert f"+ Direct ISO (gc, rom_id=1) → {roms_base}/gc/Direct.iso\n" in result.output
    assert "[unzip]" not in result.output


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


# ---------------------------------------------------------------------------
# `ferry sync` execution mode (no --dry-run)
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_executes_and_lands_files(tmp_path: Path, monkeypatch) -> None:
    """Full stack: collections → roms → download → unzip → state + sidecar."""
    monkeypatch.setenv("HOME", str(tmp_path))  # state.json + scratch under tmp
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    roms_base = tmp_path / "myroms"
    cfg = write_config(
        tmp_path / "config.toml",
        roms_base=roms_base,
        transforms_section='[transforms.gc]\npipeline = ["unzip"]',
    )

    payload = make_zip_bytes({"Pikmin.iso": b"iso-content"})
    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[
            {
                "id": 101,
                "name": "Pikmin",
                "platform_slug": "gc",
                "fs_name": "Pikmin.zip",
                "updated_at": "2026-04-25T12:00:00Z",
            }
        ],
    )
    import urllib.parse

    encoded = urllib.parse.quote("Pikmin.zip", safe="")
    respx.get(f"{BASE_URL}/api/roms/101/content/{encoded}").mock(
        return_value=httpx.Response(200, content=payload)
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "Synced:  1" in result.output
    assert "Failed:  0" in result.output

    # File extracted to the right place.
    assert (roms_base / "gc" / "Pikmin.iso").read_bytes() == b"iso-content"

    # State persisted under XDG_STATE_HOME (default ~/.local/state).
    state_path = tmp_path / ".local" / "state" / "ferry" / "state.json"
    assert state_path.exists()
    state = load_state(state_path)
    assert 101 in state.roms
    assert state.roms[101].name == "Pikmin"


@respx.mock
def test_sync_idempotent_second_run_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    """Run sync twice; second run should report nothing to do."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    roms_base = tmp_path / "myroms"
    cfg = write_config(
        tmp_path / "config.toml",
        roms_base=roms_base,
        transforms_section='[transforms.gc]\npipeline = ["unzip"]',
    )

    payload = make_zip_bytes({"A.iso": b"a"})
    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[
            {
                "id": 1,
                "name": "A",
                "platform_slug": "gc",
                "fs_name": "A.zip",
                "updated_at": "2026-04-25T12:00:00Z",
            }
        ],
    )
    import urllib.parse

    encoded = urllib.parse.quote("A.zip", safe="")
    respx.get(f"{BASE_URL}/api/roms/1/content/{encoded}").mock(
        return_value=httpx.Response(200, content=payload)
    )

    runner = CliRunner()
    first = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert first.exit_code == 0, first.output
    assert "Synced:  1" in first.output

    second = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert second.exit_code == 0, second.output
    assert "Nothing to do" in second.output


@respx.mock
def test_sync_surfaces_pending_deletes_without_executing_them(tmp_path: Path, monkeypatch) -> None:
    """ROMs in state but not in current listing are surfaced; files stay on disk."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    roms_base = tmp_path / "myroms"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)

    # Pre-seed a state with a rom that's "missing" from the current listing.
    from ferry.adapters.state_store import default_state_path, save_state
    from ferry.domain.state import LibraryState, RomState, TransformedOutput

    leftover_rom = RomState(
        rom_id=999,
        platform_slug="gc",
        name="Leftover",
        source_filename="Leftover.zip",
        source_md5="abc",
        source_size=100,
        source_updated_at="2026-01-01T00:00:00Z",
        transforms=("unzip",),
        outputs=(TransformedOutput(path="gc/Leftover.iso", md5="def", size=50),),
        primary_output_index=0,
        synced_at="2026-01-01T00:00:01Z",
    )
    state_path = default_state_path()
    save_state(LibraryState(roms={999: leftover_rom}), state_path)
    leftover_file = roms_base / "gc" / "Leftover.iso"
    leftover_file.parent.mkdir(parents=True)
    leftover_file.write_bytes(b"still-here")

    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[],  # nothing in collection now
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "Pending deletes: 1" in result.output
    assert "delete-on-remove not yet implemented" in result.output
    # File still on disk.
    assert leftover_file.exists()


@respx.mock
def test_sync_recovers_state_from_sidecars_when_state_json_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """User deleted state.json but kept ROM files + sidecars → recovery on next sync."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    roms_base = tmp_path / "ROMs"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)

    # Simulate a previous sync: ROM file exists with a sidecar; no state.json.
    from ferry.adapters.sidecar import write_sidecar
    from ferry.domain.state import RomState, TransformedOutput

    primary = roms_base / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"pretend-iso")
    rom_state = RomState(
        rom_id=101,
        platform_slug="gc",
        name="Pikmin",
        source_filename="Pikmin.zip",
        source_md5="abc",
        source_size=100,
        source_updated_at="2026-04-25T12:00:00Z",
        transforms=("unzip",),
        outputs=(TransformedOutput(path="gc/Pikmin.iso", md5="def", size=11),),
        primary_output_index=0,
        synced_at="2026-04-25T12:01:00Z",
    )
    write_sidecar(primary, rom_state)

    # RomM still has the same rom with the same updated_at → no work.
    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[
            {
                "id": 101,
                "name": "Pikmin",
                "platform_slug": "gc",
                "fs_name": "Pikmin.zip",
                "updated_at": "2026-04-25T12:00:00Z",
            }
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "recovered 1 ROM(s) from on-disk sidecars" in result.output
    assert "Nothing to do" in result.output
    # State.json now exists (saved after recovery).
    state_path = tmp_path / ".local" / "state" / "ferry" / "state.json"
    assert state_path.exists()


@respx.mock
def test_sync_per_rom_failure_continues_with_rest(tmp_path: Path, monkeypatch) -> None:
    """A failed download for one ROM doesn't abort the whole sync."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    roms_base = tmp_path / "myroms"
    cfg = write_config(
        tmp_path / "config.toml",
        roms_base=roms_base,
        transforms_section='[transforms.gc]\npipeline = ["unzip"]',
    )

    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[
            {
                "id": 1,
                "name": "Good",
                "platform_slug": "gc",
                "fs_name": "Good.zip",
                "updated_at": "2026-04-25T12:00:00Z",
            },
            {
                "id": 2,
                "name": "Bad",
                "platform_slug": "gc",
                "fs_name": "Bad.zip",
                "updated_at": "2026-04-25T12:00:00Z",
            },
        ],
    )
    import urllib.parse

    respx.get(f"{BASE_URL}/api/roms/1/content/{urllib.parse.quote('Good.zip', safe='')}").mock(
        return_value=httpx.Response(200, content=make_zip_bytes({"Good.iso": b"g"}))
    )
    respx.get(f"{BASE_URL}/api/roms/2/content/{urllib.parse.quote('Bad.zip', safe='')}").mock(
        return_value=httpx.Response(500)
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "Synced:  1" in result.output
    assert "Failed:  1" in result.output
    assert "Bad" in result.output  # listed in failures
    # Good ROM landed.
    assert (roms_base / "gc" / "Good.iso").read_bytes() == b"g"
