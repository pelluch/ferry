"""Tests for ferry.services.save_backend.

Exercises the full sync algorithm end-to-end with respx-mocked RomM and
a tmp_path filesystem. Covers all four (local, server, prior) cases plus
conflict resolution paths.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import respx

from ferry.adapters.retroarch_paths import RetroArchInstall
from ferry.adapters.romm import RommApi, RommHttpAdapter
from ferry.config import RommConfig
from ferry.domain.state import LibraryState, RomState, SaveRecord, TransformedOutput
from ferry.services.save_backend import (
    RetroArchSaveBackend,
    SaveSyncResult,
    get_or_register_device,
)
from ferry.services.save_backend_base import index_server_saves

BASE_URL = "https://romm.example.tld"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_rom(
    rom_id: int = 1,
    *,
    platform: str = "snes",
    source_filename: str = "Mario.zip",
    saves: tuple[SaveRecord, ...] = (),
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug=platform,
        name=Path(source_filename).stem,
        source_filename=source_filename,
        source_md5="0" * 32,
        source_size=2048,
        source_updated_at="2026-04-01T00:00:00Z",
        transforms=(),
        outputs=(TransformedOutput(path=f"{platform}/{source_filename}", md5="1" * 32, size=4096),),
        primary_output_index=0,
        synced_at="2026-04-01T00:00:01Z",
        saves=saves,
    )


def _make_install(
    saves_dir: Path,
    *,
    sort_by_core: bool = True,
    sort_by_content: bool = False,
) -> RetroArchInstall:
    saves_dir.mkdir(parents=True, exist_ok=True)
    return RetroArchInstall(
        source="native",
        cfg_path=saves_dir.parent / "retroarch.cfg",
        config_root=saves_dir.parent,
        savefile_directory=saves_dir,
        sort_savefiles_enable=sort_by_core,
        sort_savefiles_by_content_enable=sort_by_content,
        has_saves=True,
    )


def _make_state(roms: list[RomState], *, device_id: str | None = "dev-1") -> LibraryState:
    return LibraryState(
        roms={r.rom_id: r for r in roms},
        device_id=device_id,
    )


def _server_save(
    *,
    save_id: int,
    rom_id: int,
    emulator: str = "retroarch-snes9x",
    slot: str = "default",
    file_name: str = "Mario.srm",
    file_size: int = 1024,
    md5: str = "deadbeef" * 4,
    updated_at: str = "2026-04-25T12:00:00Z",
) -> dict[str, Any]:
    return {
        "id": save_id,
        "rom_id": rom_id,
        "emulator": emulator,
        "slot": slot,
        "file_name": file_name,
        "file_size_bytes": file_size,
        "content_hash": md5,
        "updated_at": updated_at,
    }


def _make_backend(
    install: RetroArchInstall,
    *,
    device_id: str = "dev-1",
) -> tuple[RetroArchSaveBackend, RommHttpAdapter]:
    http = RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x"))
    api = RommApi(http)
    return (
        RetroArchSaveBackend(install=install, api=api, device_id=device_id),
        http,
    )


# ---------------------------------------------------------------------------
# get_or_register_device
# ---------------------------------------------------------------------------


def test_get_device_returns_cached_when_present(tmp_path: Path) -> None:
    state = LibraryState(device_id="cached-dev")
    http = RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x"))
    api = RommApi(http)
    device_id, new_state = get_or_register_device(api, state)
    assert device_id == "cached-dev"
    assert new_state is state  # identity preserved when no work done


@respx.mock
def test_get_device_registers_when_absent_and_caches() -> None:
    state = LibraryState()  # no device_id
    respx.post(f"{BASE_URL}/api/devices").mock(
        return_value=httpx.Response(
            201,
            json={"device_id": "uuid-new", "name": "deck", "created_at": "2026-04-25T12:00:00Z"},
        )
    )
    with RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x")) as http:
        api = RommApi(http)
        device_id, new_state = get_or_register_device(api, state, hostname="deck")
    assert device_id == "uuid-new"
    assert new_state.device_id == "uuid-new"
    assert new_state is not state  # new state object


# ---------------------------------------------------------------------------
# Sync — case A: local-only (upload new save)
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_uploads_local_save_with_no_server_record(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    (saves_dir / "snes9x" / "Mario.srm").write_bytes(b"battery")
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    upload_route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=_server_save(save_id=10, rom_id=1, file_name="Mario.srm"),
        )
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert upload_route.called
    assert result.uploaded == 1
    assert result.downloaded == 0
    assert result.failed == []
    assert 1 in result.updated_roms
    new_saves = result.updated_roms[1].saves
    assert len(new_saves) == 1
    assert new_saves[0].server_save_id == 10


# ---------------------------------------------------------------------------
# Sync — case B: server-only (download new save)
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_downloads_server_save_with_no_local(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True)
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=99,
                    rom_id=1,
                    emulator="retroarch-snes9x",
                    file_name="Mario.srm",
                    md5=_md5(b"server-payload"),
                    file_size=len(b"server-payload"),
                )
            ],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/99/content").mock(
        return_value=httpx.Response(200, content=b"server-payload")
    )
    respx.post(f"{BASE_URL}/api/saves/99/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert result.uploaded == 0
    assert (saves_dir / "snes9x" / "Mario.srm").read_bytes() == b"server-payload"
    new_saves = result.updated_roms[1].saves
    assert new_saves[0].server_save_id == 99
    assert new_saves[0].emulator == "retroarch-snes9x"


@respx.mock
def test_download_sends_optimistic_false_and_calls_confirm(tmp_path: Path) -> None:
    """v3.5 contract: download URL carries `optimistic=false` and the confirm
    POST fires after a successful local write."""
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True)
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=77, rom_id=1, file_name="Mario.srm")],
        )
    )
    download_route = respx.get(url__regex=rf"{BASE_URL}/api/saves/77/content.*").mock(
        return_value=httpx.Response(200, content=b"server-payload")
    )
    confirm_route = respx.post(f"{BASE_URL}/api/saves/77/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 77})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert download_route.called
    assert "optimistic=false" in str(download_route.calls.last.request.url)
    assert confirm_route.called
    body = confirm_route.calls.last.request.content
    assert b"dev-1" in body
    # SaveRecord written only because confirm succeeded.
    assert result.updated_roms[1].saves[0].server_save_id == 77


@respx.mock
def test_download_confirm_failure_leaves_no_save_record(tmp_path: Path) -> None:
    """If confirm_download fails, the local file stays on disk (we don't roll
    back the bytes), but the SaveRecord is NOT updated and the download is
    NOT counted as successful — `properly synced` requires confirm to land.
    Server's `device.last_synced_at` stays at its previous value, so the next
    sync will re-classify and re-try the download.
    """
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True)
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=88, rom_id=1, file_name="Mario.srm")],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/88/content").mock(
        return_value=httpx.Response(200, content=b"bytes")
    )
    confirm_route = respx.post(f"{BASE_URL}/api/saves/88/downloaded").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    # File IS on disk (atomic write happened before the confirm RPC).
    assert (saves_dir / "snes9x" / "Mario.srm").read_bytes() == b"bytes"
    # But ferry doesn't claim success — no SaveRecord written, no count bumped,
    # and the failure surfaces clearly.
    assert result.downloaded == 0
    assert result.updated_roms == {}
    assert confirm_route.called
    assert any("confirm failed" in f for f in result.failed)


@respx.mock
def test_download_failure_does_not_call_confirm(tmp_path: Path) -> None:
    """A failed GET must NOT trigger the confirm RPC — the bytes never made it
    to disk, so committing server-side last_synced_at would be a lie."""
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True)
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=66, rom_id=1, file_name="Mario.srm")],
        )
    )
    download_route = respx.get(f"{BASE_URL}/api/saves/66/content").mock(
        return_value=httpx.Response(500, json={"detail": "transient"})
    )
    confirm_route = respx.post(f"{BASE_URL}/api/saves/66/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 66})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert download_route.called
    # Confirm must NOT fire — no successful write happened.
    assert not confirm_route.called
    assert result.downloaded == 0
    assert any("download" in f for f in result.failed)
    assert result.updated_roms == {}


@respx.mock
def test_sync_skips_download_when_local_path_unresolvable(tmp_path: Path) -> None:
    """sort_by_core=true + emulator=retroarch (no core suffix) → can't pick subdir."""
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True, sort_by_content=False)
    rom = _make_rom(rom_id=1)
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=99, rom_id=1, emulator="retroarch")],
        )
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 0
    assert any("cannot determine local path" in f for f in result.failed)


# ---------------------------------------------------------------------------
# Sync — case C: both present, no prior record (newest-wins seed)
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_first_time_conflict_uploads_when_local_newer(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    save = saves_dir / "snes9x" / "Mario.srm"
    save.write_bytes(b"local")
    # Touch to ensure local mtime is well after server's updated_at.
    import os
    import time

    future = time.time() + 86400  # +1 day
    os.utime(save, (future, future))
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1)
    state = _make_state([rom])  # rom.saves is empty (no prior)

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(save_id=5, rom_id=1, updated_at="2026-04-01T00:00:00Z", md5="aa" * 16)
            ],
        )
    )
    upload_route = respx.put(f"{BASE_URL}/api/saves/5").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=5, rom_id=1))
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert upload_route.called
    assert result.uploaded == 1
    assert result.conflicts_resolved == 1


@respx.mock
def test_sync_first_time_conflict_within_tolerance_is_ambiguous(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    save = saves_dir / "snes9x" / "Mario.srm"
    save.write_bytes(b"local")

    server_ts = "2026-04-25T12:00:00Z"
    # Set local mtime within 60s tolerance of server.
    import os
    from datetime import UTC, datetime

    server_dt = datetime.fromisoformat(server_ts.replace("Z", "+00:00")).astimezone(UTC)
    local_mtime = server_dt.timestamp() + 30
    os.utime(save, (local_mtime, local_mtime))
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1)
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=5, rom_id=1, updated_at=server_ts, md5="bb" * 16)],
        )
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.uploaded == 0
    assert result.downloaded == 0
    assert len(result.ambiguous) == 1
    line = result.ambiguous[0]
    # New ambiguous-line shape: rom name, filename, key context, then reason.
    assert rom.name in line
    assert "Mario.srm" in line
    assert "rom_id=1" in line
    assert "emulator=retroarch-snes9x" in line
    assert "slot=default" in line
    assert "first sync — within tolerance" in line


@respx.mock
def test_sync_first_time_with_identical_bytes_is_skip(tmp_path: Path) -> None:
    """Local + server have same MD5 — no conflict, just skip."""
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    payload = b"battery"
    save = saves_dir / "snes9x" / "Mario.srm"
    save.write_bytes(payload)
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1)
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=5, rom_id=1, md5=_md5(payload), file_size=len(payload))],
        )
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.uploaded == 0
    assert result.downloaded == 0
    assert result.skipped == 1


# ---------------------------------------------------------------------------
# Sync — case D: both present, prior record (full diff)
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_with_prior_record_skips_when_nothing_changed(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    payload = b"unchanged"
    save = saves_dir / "snes9x" / "Mario.srm"
    save.write_bytes(payload)
    install = _make_install(saves_dir)
    md5 = _md5(payload)
    prior = SaveRecord(
        emulator="retroarch-snes9x",
        slot="default",
        save_filename="Mario.srm",
        last_sync_md5=md5,
        last_sync_server_size=len(payload),
        last_sync_server_updated_at="2026-04-25T12:00:00Z",
        last_synced_at="2026-04-25T12:00:01Z",
        server_save_id=5,
    )
    rom = _make_rom(rom_id=1, saves=(prior,))
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=5,
                    rom_id=1,
                    md5=md5,
                    file_size=len(payload),
                    updated_at="2026-04-25T12:00:00Z",
                )
            ],
        )
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.uploaded == 0
    assert result.downloaded == 0
    assert result.skipped == 1


@respx.mock
def test_sync_with_prior_uploads_when_only_local_changed(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    new_payload = b"new local content"
    save = saves_dir / "snes9x" / "Mario.srm"
    save.write_bytes(new_payload)
    install = _make_install(saves_dir)
    prior = SaveRecord(
        emulator="retroarch-snes9x",
        slot="default",
        save_filename="Mario.srm",
        last_sync_md5=_md5(b"old local content"),  # local has changed since
        last_sync_server_size=10,
        last_sync_server_updated_at="2026-04-25T12:00:00Z",
        last_synced_at="2026-04-25T12:00:01Z",
        server_save_id=5,
    )
    rom = _make_rom(rom_id=1, saves=(prior,))
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=5,
                    rom_id=1,
                    file_size=10,
                    updated_at="2026-04-25T12:00:00Z",  # unchanged on server
                    md5=_md5(b"old local content"),
                )
            ],
        )
    )
    upload_route = respx.put(f"{BASE_URL}/api/saves/5").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=5, rom_id=1))
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert upload_route.called
    assert result.uploaded == 1


# ---------------------------------------------------------------------------
# Sync — both gone, prior present → drop
# ---------------------------------------------------------------------------


def test_drop_prior_when_local_and_server_gone(tmp_path: Path) -> None:
    """Both sides deleted the save since last sync. The prior record clears."""
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir(parents=True)
    install = _make_install(saves_dir)
    prior = SaveRecord(
        emulator="retroarch-snes9x",
        slot="default",
        save_filename="Mario.srm",
        last_sync_md5="0" * 32,
        last_sync_server_size=10,
        last_sync_server_updated_at="2026-04-25T12:00:00Z",
        last_synced_at="2026-04-25T12:00:01Z",
        server_save_id=5,
    )
    rom = _make_rom(rom_id=1, saves=(prior,))
    state = _make_state([rom])

    with respx.mock:
        respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
        backend, http = _make_backend(install)
        with http:
            result = backend.sync(state)

    assert result.updated_roms[1].saves == ()  # prior dropped


# ---------------------------------------------------------------------------
# Failure surface
# ---------------------------------------------------------------------------


@respx.mock
def test_list_saves_failure_returns_failed_result(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    install = _make_install(saves_dir)
    state = _make_state([_make_rom()])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(500))

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert len(result.failed) == 1
    assert "could not list server saves" in result.failed[0]
    assert result.uploaded == 0
    assert result.downloaded == 0


@respx.mock
def test_upload_409_skips_with_warning_and_preserves_prior(tmp_path: Path) -> None:
    """v3.5 server-as-arbiter: a 409 from upload counts as `upload_conflicts`,
    not `uploaded` and not `failed`. The prior SaveRecord is preserved
    verbatim so the next sync re-classifies with fresh server state.
    """
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    save = saves_dir / "snes9x" / "Mario.srm"
    save.write_bytes(b"local-progress")
    install = _make_install(saves_dir)
    # Build a prior so we can verify it's preserved unchanged.
    prior = SaveRecord(
        emulator="retroarch-snes9x",
        slot="default",
        save_filename="Mario.srm",
        last_sync_md5="aa" * 16,
        last_sync_server_size=1024,
        last_sync_server_updated_at="2026-04-01T00:00:00Z",
        last_synced_at="2026-04-01T00:00:00Z",
        server_save_id=5,
    )
    rom = _make_rom(rom_id=1, saves=(prior,))
    state = _make_state([rom])

    # Server has a newer save than our prior — different updated_at, different md5.
    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=5,
                    rom_id=1,
                    file_name="Mario.srm",
                    md5="bb" * 16,
                    updated_at="2026-04-25T12:00:00Z",
                )
            ],
        )
    )
    upload_route = respx.put(f"{BASE_URL}/api/saves/5").mock(
        return_value=httpx.Response(409, json={"detail": "Slot has a newer save"})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert upload_route.called
    # Strict-mode confirmation: the upload was sent without overwrite=true.
    sent_url = str(upload_route.calls.last.request.url)
    assert "overwrite=" not in sent_url
    # Outcome accounting: not uploaded, not failed, surfaced as upload_conflicts.
    assert result.uploaded == 0
    assert result.upload_conflicts == 1
    assert result.failed == []
    # User-visible warning surfaces the situation.
    assert any("server has a newer save" in w for w in result.warnings)
    # Prior SaveRecord preserved exactly — next sync will re-classify.
    if 1 in result.updated_roms:
        # A prior-only rewrite is acceptable as long as the record is identical.
        assert result.updated_roms[1].saves == (prior,)


@respx.mock
def test_upload_failure_records_in_result(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    (saves_dir / "snes9x" / "Mario.srm").write_bytes(b"x")
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1)
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(500))

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.uploaded == 0
    assert len(result.failed) == 1
    assert "Mario" in result.failed[0]


# ---------------------------------------------------------------------------
# Walker warnings propagate through
# ---------------------------------------------------------------------------


@respx.mock
def test_download_strips_romm_datetime_tag_from_filename(tmp_path: Path) -> None:
    """RomM appends `[YYYY-MM-DD_HH-MM-SS]` on every upload; the local file
    must NOT carry that tag — RetroArch loads `<rom-stem>.srm`, not
    `<rom-stem> [TIMESTAMP].srm`. Verified against the actual filename pattern
    we observed on the user's RomM in live testing."""
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True)
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=42,
                    rom_id=1,
                    file_name="Mario [2026-04-24_15-51-34].srm",
                    md5=_md5(b"data"),
                    file_size=len(b"data"),
                )
            ],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=b"data")
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    # File on disk has the stripped name — RetroArch will find it.
    expected = saves_dir / "snes9x" / "Mario.srm"
    assert expected.exists()
    assert expected.read_bytes() == b"data"
    # And the tagged filename is NOT present.
    assert not (saves_dir / "snes9x" / "Mario [2026-04-24_15-51-34].srm").exists()
    # SaveRecord stores the local (stripped) filename for next-sync matching.
    assert result.updated_roms[1].saves[0].save_filename == "Mario.srm"


@respx.mock
def test_upload_response_record_strips_datetime_tag(tmp_path: Path) -> None:
    """Post-upload SaveRecord.save_filename must reflect what's on disk
    locally (no tag), even though the server returned the tagged filename."""
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    (saves_dir / "snes9x" / "Mario.srm").write_bytes(b"data")
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=_server_save(
                save_id=10,
                rom_id=1,
                file_name="Mario [2026-04-25_12-00-00].srm",  # server returns tagged
            ),
        )
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.uploaded == 1
    assert result.updated_roms[1].saves[0].save_filename == "Mario.srm"


@respx.mock
def test_download_uses_core_info_for_canonical_dir_casing(tmp_path: Path) -> None:
    """RetroArch creates `Snes9x/` (capitalized per .info corename); decky
    uploads with `retroarch-snes9x` (lowercase). The download path resolver
    must use the .info index to write to the correctly-cased dir, otherwise
    saves end up in `snes9x/` and `Snes9x/` parallel dirs."""
    saves_dir = tmp_path / "saves"

    info_dir = tmp_path / "cores"
    info_dir.mkdir()
    (info_dir / "snes9x_libretro.info").write_text('corename = "Snes9x"\n')

    install = RetroArchInstall(
        source="native",
        cfg_path=tmp_path / "retroarch.cfg",
        config_root=tmp_path,
        savefile_directory=saves_dir,
        sort_savefiles_enable=True,
        sort_savefiles_by_content_enable=False,
        has_saves=False,
        core_info_candidates=(info_dir,),
    )
    rom = _make_rom(rom_id=1, source_filename="Mario.zip")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=99, rom_id=1, file_name="Mario.srm")],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/99/content").mock(
        return_value=httpx.Response(200, content=b"data")
    )
    respx.post(f"{BASE_URL}/api/saves/99/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    # Capitalized — matches RA's actual on-disk layout.
    assert (saves_dir / "Snes9x" / "Mario.srm").exists()
    # Lowercase NOT present — would mean the .info lookup didn't take effect.
    assert not (saves_dir / "snes9x" / "Mario.srm").exists()


@respx.mock
def test_download_works_when_dest_and_tmp_are_different_filesystems(tmp_path: Path) -> None:
    """Regression: tempfile.TemporaryDirectory uses /tmp by default. When the
    saves dir is on a different mount, the previous tmp→dest move would fail
    with EXDEV. Now we download directly to dest via the existing .part-rename
    pattern in `RommHttpAdapter.download`. This test stands in for that fix —
    it doesn't actually exercise different filesystems (hard to do in pytest)
    but confirms the download path doesn't crash and writes to the right place
    even when /tmp would be a different fs in production."""
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True)
    rom = _make_rom(rom_id=1)
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=1, rom_id=1, file_name="Mario.srm")],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/1/content").mock(
        return_value=httpx.Response(200, content=b"data")
    )
    respx.post(f"{BASE_URL}/api/saves/1/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert (saves_dir / "snes9x" / "Mario.srm").exists()
    # No `.part` left behind (the http adapter cleans up on success).
    assert not (saves_dir / "snes9x" / "Mario.srm.part").exists()


@respx.mock
def test_sync_picks_latest_server_save_when_slot_has_history(tmp_path: Path) -> None:
    """RomM accumulates timestamped saves per slot. The diff must use the
    most recent one by updated_at, not whichever happens to come first in
    the list response."""
    saves_dir = tmp_path / "saves"
    install = _make_install(saves_dir, sort_by_core=True)
    rom = _make_rom(rom_id=1)
    state = _make_state([rom])

    # Server returns three timestamped saves for the same (rom_id, emulator, slot).
    # The most-recent one (by updated_at) should be the one ferry downloads.
    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=10,
                    rom_id=1,
                    file_name="Mario [2026-04-20_10-00-00].srm",
                    updated_at="2026-04-20T10:00:00Z",
                ),
                _server_save(
                    save_id=11,
                    rom_id=1,
                    file_name="Mario [2026-04-22_10-00-00].srm",
                    updated_at="2026-04-22T10:00:00Z",
                ),
                _server_save(
                    save_id=12,
                    rom_id=1,
                    file_name="Mario [2026-04-21_10-00-00].srm",
                    updated_at="2026-04-21T10:00:00Z",
                ),
            ],
        )
    )
    # Only the latest (id=11) should be downloaded.
    download_route = respx.get(f"{BASE_URL}/api/saves/11/content").mock(
        return_value=httpx.Response(200, content=b"latest")
    )
    respx.post(f"{BASE_URL}/api/saves/11/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 11})
    )

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert download_route.called
    assert result.downloaded == 1
    assert result.updated_roms[1].saves[0].server_save_id == 11


def test_index_server_saves_orders_by_instant_not_lexically() -> None:
    """Regression: mixed-offset timestamps must order by instant, not by string.

    `2026-05-05T11:00:00+02:00` (= 09:00 UTC) is EARLIER than
    `2026-05-05T10:00:00Z`, but lexical compare says the `+02:00` form is
    later. RomM happens to serve UTC consistently today; this guards against
    silent regression if it ever serves mixed offsets.
    """
    earlier = {
        "id": 100,
        "rom_id": 1,
        "emulator": "retroarch-snes9x",
        "slot": "default",
        "file_name": "earlier.srm",
        "updated_at": "2026-05-05T11:00:00+02:00",  # 09:00 UTC
    }
    later = {
        "id": 101,
        "rom_id": 1,
        "emulator": "retroarch-snes9x",
        "slot": "default",
        "file_name": "later.srm",
        "updated_at": "2026-05-05T10:00:00Z",  # 10:00 UTC
    }
    # Hand `earlier` to the indexer LAST so a buggy lexical max() picks it.
    indexed = index_server_saves(
        [later, earlier],
        emulator_matches=lambda emu: emu.startswith("retroarch-"),
        default_slot="default",
    )
    picked = indexed[(1, "retroarch-snes9x", "default")]
    assert picked["id"] == 101  # the truly-later one


@respx.mock
def test_walker_warnings_surface_in_result(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    (saves_dir / "snes9x" / "Unknown.srm").write_bytes(b"x")  # not in state
    install = _make_install(saves_dir)
    state = _make_state([_make_rom(rom_id=1, source_filename="Mario.zip")])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))

    backend, http = _make_backend(install)
    with http:
        result = backend.sync(state)

    assert any("Unknown.srm" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# SaveSyncResult helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# delete_for_rom
# ---------------------------------------------------------------------------


def test_delete_for_rom_trashes_local_save(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    save = saves_dir / "snes9x" / "Mario.srm"
    save.write_bytes(b"data")
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1)

    trash_dir = tmp_path / "trash" / "20260425T120000Z__rom1"
    trash_dir.mkdir(parents=True)

    backend, http = _make_backend(install)
    with http:
        count, warnings = backend.delete_for_rom(rom, trash_dir)

    assert count == 1
    assert warnings == []
    assert not save.exists()
    moved = trash_dir / "saves" / "snes9x" / "Mario.srm"
    assert moved.exists()
    assert moved.read_bytes() == b"data"


def test_delete_for_rom_no_matches_returns_zero(tmp_path: Path) -> None:
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    install = _make_install(saves_dir)
    rom = _make_rom(rom_id=1)
    trash_dir = tmp_path / "trash" / "rom1"
    trash_dir.mkdir(parents=True)

    backend, http = _make_backend(install)
    with http:
        count, warnings = backend.delete_for_rom(rom, trash_dir)

    assert count == 0
    # No warnings — empty saves dir is normal.
    assert warnings == []


def test_delete_for_rom_preserves_other_roms_saves(tmp_path: Path) -> None:
    """Trashing rom 1's saves must not touch rom 2's saves."""
    saves_dir = tmp_path / "saves"
    (saves_dir / "snes9x").mkdir(parents=True)
    rom1_save = saves_dir / "snes9x" / "Mario.srm"
    rom2_save = saves_dir / "snes9x" / "Sonic.srm"
    rom1_save.write_bytes(b"a")
    rom2_save.write_bytes(b"b")

    install = _make_install(saves_dir)
    rom1 = _make_rom(rom_id=1, source_filename="Mario.zip")
    # rom2 doesn't get passed to delete_for_rom — confirms its save isn't touched.

    trash_dir = tmp_path / "trash" / "rom1"
    trash_dir.mkdir(parents=True)

    backend, http = _make_backend(install)
    with http:
        # The walker indexes both roms via the call; only rom1's save trashed.
        backend._install = install  # type: ignore[attr-defined]
        # Workaround: delete_for_rom calls list_local_saves with [rom] which
        # only indexes that rom's stems. Rom 2's save should be unmatched and skipped.
        count, _ = backend.delete_for_rom(rom1, trash_dir)

    assert count == 1
    assert not rom1_save.exists()
    assert rom2_save.exists()


# ---------------------------------------------------------------------------
# SaveSyncResult helpers
# ---------------------------------------------------------------------------


def test_is_empty_true_for_no_op_run() -> None:
    assert SaveSyncResult().is_empty
    assert SaveSyncResult(skipped=10).is_empty  # only skips


def test_is_empty_false_when_anything_happened() -> None:
    assert not SaveSyncResult(uploaded=1).is_empty
    assert not SaveSyncResult(downloaded=1).is_empty
    assert not SaveSyncResult(failed=["x"]).is_empty
    assert not SaveSyncResult(ambiguous=["x"]).is_empty
