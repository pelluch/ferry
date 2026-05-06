"""End-to-end CLI tests for `ferry reconcile`."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import respx
from click.testing import CliRunner

from ferry.adapters.sidecar import sidecar_path_for
from ferry.adapters.state_store import default_state_path, load_state
from ferry.cli import app

BASE_URL = "https://romm.example.tld"


def _write_config(cfg: Path, *, roms_base: Path) -> Path:
    cfg.write_text(
        f'[romm]\nurl = "{BASE_URL}"\napi_key = "rmm_abcdef0123456789"\n\n'
        f'[destination]\nroms_base = "{roms_base}"\n\n'
        '[sync]\ncollections = ["Steam Deck"]\n'
    )
    return cfg


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _platform_payload(platform_id: int, slug: str) -> dict:
    return {"id": platform_id, "slug": slug, "name": slug.upper()}


def _rom_payload(
    rom_id: int,
    name: str,
    *,
    fs_name: str,
    platform_slug: str,
    files: list[dict],
    updated_at: str = "2026-01-01T00:00:00Z",
    fs_size_bytes: int = 1024,
) -> dict:
    return {
        "id": rom_id,
        "name": name,
        "platform_slug": platform_slug,
        "fs_name": fs_name,
        "fs_size_bytes": fs_size_bytes,
        "updated_at": updated_at,
        "files": files,
    }


def _file_payload(file_id: int, file_name: str, md5: str | None) -> dict:
    return {
        "id": file_id,
        "file_name": file_name,
        "file_size_bytes": 100,
        "md5_hash": md5,
    }


def _mock_platforms(platforms: list[dict]) -> None:
    respx.get(f"{BASE_URL}/api/platforms").mock(return_value=httpx.Response(200, json=platforms))


def _mock_roms_for_platform(platform_id: int, rom_items: list[dict]) -> None:
    respx.get(
        f"{BASE_URL}/api/roms",
        params={"platform_ids": [platform_id]},
    ).mock(
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


def test_reconcile_without_destination_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[romm]\nurl = "{BASE_URL}"\napi_key = "rmm_abc"\n')
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code != 0
    assert "[destination]" in result.output


def test_reconcile_no_orphans_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    roms_base.mkdir()
    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code == 0, result.output
    assert "No orphans found" in result.output


# ---------------------------------------------------------------------------
# Confident adoption
# ---------------------------------------------------------------------------


@respx.mock
def test_reconcile_adopts_confident_pass_through_orphan(tmp_path: Path, monkeypatch) -> None:
    """Cartridge `.gba` matches RomM by name+hash → adopted: sidecar +
    state.json updated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Pikmin.gba"
    target.parent.mkdir(parents=True)
    payload = b"GBA content " * 50
    target.write_bytes(payload)
    md5 = _md5(payload)

    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(7, "gba")])
    _mock_roms_for_platform(
        7,
        [
            _rom_payload(
                101,
                "Pikmin",
                fs_name="Pikmin.gba",
                platform_slug="gba",
                files=[_file_payload(1, "Pikmin.gba", md5)],
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code == 0, result.output
    assert "Confident:  1" in result.output
    assert "Adopted 1 ROM(s)" in result.output

    # Sidecar at canonical location.
    canonical = sidecar_path_for(target, roms_base=roms_base)
    assert canonical.exists()
    # State.json has the new entry.
    state = load_state(default_state_path())
    assert 101 in state.roms
    assert state.roms[101].name == "Pikmin"
    assert state.roms[101].outputs[0].path == "gba/Pikmin.gba"


@respx.mock
def test_reconcile_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Pikmin.gba"
    target.parent.mkdir(parents=True)
    payload = b"x"
    target.write_bytes(payload)
    md5 = _md5(payload)
    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(7, "gba")])
    _mock_roms_for_platform(
        7,
        [
            _rom_payload(
                101,
                "Pikmin",
                fs_name="Pikmin.gba",
                platform_slug="gba",
                files=[_file_payload(1, "Pikmin.gba", md5)],
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile", "--dry-run"], env={})
    assert result.exit_code == 0, result.output
    assert "Confident:  1" in result.output
    assert "(dry run — no sidecars or state written)" in result.output

    # No sidecar written.
    assert not sidecar_path_for(target, roms_base=roms_base).exists()
    # No state changes.
    state = load_state(default_state_path())
    assert 101 not in state.roms


# ---------------------------------------------------------------------------
# Non-confident classifications
# ---------------------------------------------------------------------------


@respx.mock
def test_reconcile_lists_name_only_without_adopting(tmp_path: Path, monkeypatch) -> None:
    """User has a different revision: filename matches RomM but hash differs.
    Listed but not adopted (default mode)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Pikmin.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"local-version")  # different from RomM's md5
    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(7, "gba")])
    _mock_roms_for_platform(
        7,
        [
            _rom_payload(
                101,
                "Pikmin",
                fs_name="Pikmin.gba",
                platform_slug="gba",
                files=[_file_payload(1, "Pikmin.gba", md5="00" * 16)],
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code == 0, result.output
    assert "Name-only:  1" in result.output
    assert "Confident:  0" in result.output
    assert "Adopted" not in result.output
    # Nothing written.
    assert not sidecar_path_for(target, roms_base=roms_base).exists()


@respx.mock
def test_reconcile_lists_no_match_for_files_not_in_romm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Stranger.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unknown")
    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(7, "gba")])
    _mock_roms_for_platform(7, [])  # empty platform

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code == 0, result.output
    assert "No match:   1" in result.output
    assert "Stranger.gba" in result.output


@respx.mock
def test_reconcile_skips_files_without_matching_romm_platform(tmp_path: Path, monkeypatch) -> None:
    """Local dir name doesn't resolve to any RomM platform → all NoMatch."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    (roms_base / "weird-platform").mkdir(parents=True)
    (roms_base / "weird-platform" / "x.bin").write_bytes(b"x")
    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(7, "gba")])  # only gba; no weird-platform

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code == 0, result.output
    assert "No match:   1" in result.output


# ---------------------------------------------------------------------------
# --platform scoping
# ---------------------------------------------------------------------------


@respx.mock
def test_reconcile_platform_filter_scopes_walk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    (roms_base / "gba").mkdir(parents=True)
    (roms_base / "snes").mkdir(parents=True)
    (roms_base / "gba" / "A.gba").write_bytes(b"a")
    (roms_base / "snes" / "B.smc").write_bytes(b"b")
    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(7, "gba"), _platform_payload(8, "snes")])
    _mock_roms_for_platform(7, [])

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile", "--platform", "gba"], env={})
    assert result.exit_code == 0, result.output
    # Saw the gba file but not the snes file.
    assert "A.gba" in result.output
    assert "B.smc" not in result.output


# ---------------------------------------------------------------------------
# Multi-file zip (DOS-style) confident match
# ---------------------------------------------------------------------------


@respx.mock
def test_reconcile_adopts_multi_file_zip_via_largest_inner_hash(
    tmp_path: Path, monkeypatch
) -> None:
    """The DOS-game case: zip contains many files; ferry hashes the largest
    inner file (matching RomM) and adopts the orphan zip."""
    import zipfile

    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    target = roms_base / "dos" / "USNavyFighters.zip"
    target.parent.mkdir(parents=True)
    big_inner = b"BIN content" * 5000
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("USNFGOLD/run.bat", b"x")
        z.writestr("USNFGOLD/USNF.EXE", b"exe")
        z.writestr("cd/USNFGOLD.bin", big_inner)  # largest
    inner_md5 = _md5(big_inner)

    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(99, "dos")])
    _mock_roms_for_platform(
        99,
        [
            _rom_payload(
                202,
                "U.S. Navy Fighters Gold",
                fs_name="USNavyFighters.zip",
                platform_slug="dos",
                files=[_file_payload(1, "USNavyFighters.zip", inner_md5)],
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code == 0, result.output
    assert "Confident:  1" in result.output
    assert "Adopted 1 ROM(s)" in result.output
    assert sidecar_path_for(target, roms_base=roms_base).exists()


@respx.mock
def test_reconcile_adopts_unzipped_rom_via_stem_match(tmp_path: Path, monkeypatch) -> None:
    """Live regression: GC `.rvz` matched server `.zip` by stem + hash
    (RomM hashes the zip's largest inner file, which is the .rvz). Was
    previously classified Hash-only because filenames differ; should
    now be Confident and adopted."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gc" / "Eternal Darkness - Sanity's Requiem (USA).rvz"
    target.parent.mkdir(parents=True)
    payload = b"RVZ DISC IMAGE" * 5000
    target.write_bytes(payload)
    md5 = _md5(payload)

    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    _mock_platforms([_platform_payload(99, "gc")])
    _mock_roms_for_platform(
        99,
        [
            _rom_payload(
                19412,
                "Eternal Darkness: Sanity's Requiem",
                fs_name="Eternal Darkness - Sanity's Requiem (USA).zip",
                platform_slug="gc",
                files=[_file_payload(1, "Eternal Darkness - Sanity's Requiem (USA).zip", md5)],
            )
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert result.exit_code == 0, result.output
    assert "Confident:  1" in result.output
    assert "Hash-only:  0" in result.output
    assert "Adopted 1 ROM(s)" in result.output

    state = load_state(default_state_path())
    assert 19412 in state.roms


@respx.mock
def test_reconcile_skips_already_tracked_files(tmp_path: Path, monkeypatch) -> None:
    """Walker excludes files already in state → reconcile is idempotent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Tracked.gba"
    target.parent.mkdir(parents=True)
    payload = b"tracked"
    target.write_bytes(payload)
    md5 = _md5(payload)
    cfg = _write_config(tmp_path / "config.toml", roms_base=roms_base)

    # First run: adopt.
    _mock_platforms([_platform_payload(7, "gba")])
    _mock_roms_for_platform(
        7,
        [
            _rom_payload(
                101,
                "Tracked",
                fs_name="Tracked.gba",
                platform_slug="gba",
                files=[_file_payload(1, "Tracked.gba", md5)],
            )
        ],
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert "Adopted 1 ROM(s)" in result.output

    # Second run: should find no orphans (the file is now tracked).
    result2 = runner.invoke(app, ["--config", str(cfg), "reconcile"], env={})
    assert "No orphans found" in result2.output
