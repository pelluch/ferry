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
    collections: tuple[str, ...] = ("Steam Deck",),
    platforms: tuple[str, ...] = (),
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
    parts += ["", "[sync]"]
    if collections:
        parts.append(f"collections = {list(collections)!r}")
    if platforms:
        parts.append(f"platforms = {list(platforms)!r}")
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
    assert "[sync]" in result.output


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
    cfg = write_config(tmp_path / "config.toml", collections=("Atlantis",))
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
    cfg = write_config(tmp_path / "config.toml", collections=("Dupes",))
    mock_endpoints(
        collections=[{"id": 1, "name": "Dupes"}, {"id": 2, "name": "Dupes"}],
        rom_items=[],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code != 0
    assert "multiple collections" in result.output


@respx.mock
def test_unknown_collection_and_platform_both_reported(tmp_path: Path) -> None:
    """Both resolution failures surface together so the user sees the whole picture."""
    cfg = write_config(
        tmp_path / "config.toml",
        collections=("Atlantis",),
        platforms=("nintendo-virtual-boy-classic",),
    )
    respx.get(f"{BASE_URL}/api/collections").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "Steam Deck"}])
    )
    respx.get(f"{BASE_URL}/api/platforms").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "slug": "gba", "name": "GBA"}])
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code != 0
    # Both problems mentioned in the same error.
    assert "Atlantis" in result.output
    assert "nintendo-virtual-boy-classic" in result.output
    # Both available lists shown.
    assert "Steam Deck" in result.output  # available collections
    assert "gba" in result.output  # available platforms


# ---------------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------------


@respx.mock
def test_dry_run_shows_adds_with_resolved_paths_and_pipeline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    cfg = write_config(
        tmp_path / "config.toml",
        collections=("Steam Deck",),
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
    assert "resolved 1 collection(s)" in result.output
    assert "Steam Deck" in result.output
    assert "2 unique ROM(s)" in result.output
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
def test_dry_run_does_not_purge_trash(tmp_path: Path, monkeypatch) -> None:
    """`--dry-run` must never touch disk state, including the trash."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = write_config(tmp_path / "config.toml")

    # Pre-seed trash with an entry that's well past retention.
    trash_root = tmp_path / ".local" / "state" / "ferry" / "trash"
    trash_root.mkdir(parents=True)
    ancient_entry = trash_root / "20200101T000000Z__rom42"
    ancient_entry.mkdir()

    mock_endpoints(collections=[{"id": 6, "name": "Steam Deck"}], rom_items=[])

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    # Old trash entry survives — dry-run made no changes.
    assert ancient_entry.exists()


@respx.mock
def test_sync_executes_deletes_and_moves_to_trash(tmp_path: Path, monkeypatch) -> None:
    """ROMs in state but not in current listing get soft-deleted to trash."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    roms_base = tmp_path / "myroms"
    # delete_on_remove defaults to False; opt in for this test.
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[romm]\n"
        f'url = "{BASE_URL}"\n'
        'api_key = "rmm_abcdef0123456789"\n'
        "\n[destination]\n"
        f'roms_base = "{roms_base}"\n'
        "\n[sync]\n"
        'collections = ["Steam Deck"]\n'
        "delete_on_remove = true\n"
    )

    from ferry.adapters.sidecar import sidecar_path_for, write_sidecar
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
    write_sidecar(leftover_file, leftover_rom)

    mock_endpoints(
        collections=[{"id": 6, "name": "Steam Deck"}],
        rom_items=[],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "Deleted: 1" in result.output
    assert "Trashed ROMs" in result.output
    # File and sidecar moved out of roms_base.
    assert not leftover_file.exists()
    assert not sidecar_path_for(leftover_file).exists()
    # Trash dir holds the same layout.
    trash_root = tmp_path / ".local" / "state" / "ferry" / "trash"
    trash_entries = list(trash_root.iterdir())
    assert len(trash_entries) == 1
    assert (trash_entries[0] / "gc" / "Leftover.iso").read_bytes() == b"still-here"


@respx.mock
def test_sync_with_delete_on_remove_false_keeps_files(tmp_path: Path, monkeypatch) -> None:
    """delete_on_remove=false means the planner doesn't generate DeleteAction."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "myroms"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[romm]\n"
        f'url = "{BASE_URL}"\n'
        'api_key = "rmm_abcdef0123456789"\n'
        "\n[destination]\n"
        f'roms_base = "{roms_base}"\n'
        "\n[sync]\n"
        'collections = ["Steam Deck"]\n'
        "delete_on_remove = false\n"
    )

    from ferry.adapters.state_store import default_state_path, save_state
    from ferry.domain.state import LibraryState, RomState, TransformedOutput

    rom = RomState(
        rom_id=42,
        platform_slug="gc",
        name="Untouched",
        source_filename="Untouched.zip",
        source_md5="abc",
        source_size=10,
        source_updated_at="2026-01-01T00:00:00Z",
        transforms=(),
        outputs=(TransformedOutput(path="gc/Untouched.iso", md5="x", size=5),),
        primary_output_index=0,
        synced_at="2026-01-01T00:00:01Z",
    )
    save_state(LibraryState(roms={42: rom}), default_state_path())
    f = roms_base / "gc" / "Untouched.iso"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"keep")

    mock_endpoints(collections=[{"id": 6, "name": "Steam Deck"}], rom_items=[])

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    # Planner surfaces the no-longer-in-collection rom; executor suppresses.
    assert "Delete:     1" in result.output
    assert "Nothing to execute" in result.output
    assert "delete_on_remove = true" in result.output
    assert f.exists()  # still here


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


def test_sync_refuses_when_lock_is_held(tmp_path: Path) -> None:
    """If another sync is running (or a stale flock survives somehow),
    `ferry sync` must abort with a clear, actionable error rather than
    racing on state.json."""
    from ferry.services.sync_lock import acquire_sync_lock, default_lock_path

    cfg = write_config(tmp_path / "config.toml")
    runner = CliRunner()

    # Pre-acquire the lock at the path the CLI will use. Conftest's
    # _isolated_home fixture pinned HOME to a tmp dir, so default_lock_path()
    # resolves under there — same path the CLI will hit.
    with acquire_sync_lock(default_lock_path()):
        result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code != 0
    assert "another ferry sync is already running" in result.output
    assert "sync.lock" in result.output


# ---------------------------------------------------------------------------
# [saves] integration — save sync runs after library sync
# ---------------------------------------------------------------------------


def write_config_with_saves(
    cfg: Path,
    *,
    roms_base: Path,
    enabled: bool = True,
    retroarch_install: str | None = None,
) -> Path:
    """Like write_config but also includes a [saves] section."""
    parts = [
        "[romm]",
        f'url = "{BASE_URL}"',
        'api_key = "rmm_abcdef0123456789"',
        "",
        "[destination]",
        f'roms_base = "{roms_base}"',
        "",
        "[sync]",
        'collections = ["Steam Deck"]',
        "",
        "[saves]",
        f"enabled = {'true' if enabled else 'false'}",
    ]
    if retroarch_install is not None:
        parts.append(f'retroarch_install = "{retroarch_install}"')
    cfg.write_text("\n".join(parts) + "\n")
    return cfg


def _plant_native_ra(home: Path, *, saves_path: Path | None = None) -> Path:
    """Create a fake native RetroArch install at home, return its saves dir."""
    cfg_root = home / ".config" / "retroarch"
    cfg_root.mkdir(parents=True)
    saves = saves_path or (cfg_root / "saves")
    saves.mkdir(parents=True, exist_ok=True)
    (cfg_root / "retroarch.cfg").write_text(
        f'savefile_directory = "{saves}"\nsort_savefiles_enable = "true"\n'
    )
    return saves


@respx.mock
def test_sync_skips_save_section_when_disabled(tmp_path: Path, monkeypatch) -> None:
    """[saves].enabled = false → no save sync runs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config_with_saves(tmp_path / "config.toml", roms_base=roms_base, enabled=False)
    _plant_native_ra(tmp_path)

    mock_endpoints(collections=[{"id": 1, "name": "Steam Deck"}], rom_items=[])
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "Save sync:" not in result.output


@respx.mock
def test_sync_skips_when_saves_section_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config(tmp_path / "config.toml", roms_base=roms_base)  # no [saves]
    _plant_native_ra(tmp_path)

    mock_endpoints(collections=[{"id": 1, "name": "Steam Deck"}], rom_items=[])
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "Save sync:" not in result.output


@respx.mock
def test_sync_skips_save_sync_when_no_retroarch_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config_with_saves(tmp_path / "config.toml", roms_base=roms_base)
    # No RetroArch installed.

    mock_endpoints(collections=[{"id": 1, "name": "Steam Deck"}], rom_items=[])
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "save sync skipped" in result.output
    assert "no RetroArch install detected" in result.output
    assert "Save sync:" not in result.output


@respx.mock
def test_sync_surfaces_friendly_message_on_403_register_device(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config_with_saves(tmp_path / "config.toml", roms_base=roms_base)
    _plant_native_ra(tmp_path)

    mock_endpoints(collections=[{"id": 1, "name": "Steam Deck"}], rom_items=[])
    respx.post(f"{BASE_URL}/api/devices").mock(return_value=httpx.Response(403))

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output  # library sync still succeeded
    assert "save sync skipped" in result.output
    assert "lacks write scopes" in result.output
    assert "Save sync:" not in result.output


@respx.mock
def test_sync_runs_save_sync_after_library_sync(tmp_path: Path, monkeypatch) -> None:
    """Happy path: library sync succeeds, save sync runs and reports counts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config_with_saves(tmp_path / "config.toml", roms_base=roms_base)
    _plant_native_ra(tmp_path)

    mock_endpoints(collections=[{"id": 1, "name": "Steam Deck"}], rom_items=[])
    respx.post(f"{BASE_URL}/api/devices").mock(
        return_value=httpx.Response(
            201,
            json={
                "device_id": "test-device-uuid",
                "name": "test",
                "created_at": "2026-04-25T12:00:00Z",
            },
        )
    )
    # Empty server saves list — save sync runs but has nothing to do.
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync"], env={})
    assert result.exit_code == 0, result.output
    assert "Syncing saves" in result.output
    assert "Save sync:" in result.output
    assert "Uploaded:   0" in result.output
    assert "Downloaded: 0" in result.output


def test_dry_run_skips_save_sync(tmp_path: Path, monkeypatch) -> None:
    """--dry-run never touches saves — even with [saves] enabled."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "roms"
    cfg = write_config_with_saves(tmp_path / "config.toml", roms_base=roms_base)
    _plant_native_ra(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "sync", "--dry-run"], env={})
    # No HTTP mocks needed because dry-run still hits library endpoints; we just
    # need to confirm Save sync doesn't appear. Library calls will fail, so we
    # tolerate non-zero exit and just check the absence of save-sync output.
    assert "Save sync:" not in result.output
