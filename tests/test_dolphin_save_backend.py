"""Tests for ferry.services.dolphin_save_backend.

End-to-end exercises with respx-mocked RomM and a tmp_path filesystem.
DolphinTool is mocked at the read_header layer so tests don't shell out.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import respx

from ferry.adapters.dolphin_paths import DolphinInstall, RegionEncoding
from ferry.adapters.dolphin_tool import DiscHeader, DolphinTool
from ferry.adapters.romm import RommApi, RommHttpAdapter
from ferry.config import RommConfig
from ferry.domain.state import LibraryState, RomState, SaveRecord, TransformedOutput
from ferry.services.dolphin_save_backend import DolphinSaveBackend

BASE_URL = "https://romm.example.tld"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_install(
    saves_root: Path, *, region_encoding: RegionEncoding = "3-letter"
) -> DolphinInstall:
    saves_root.mkdir(parents=True, exist_ok=True)
    return DolphinInstall(
        source="native",
        saves_root=saves_root,
        config_path=saves_root.parent / "Config" / "Dolphin.ini",
        region_encoding=region_encoding,
        settings=None,
        has_saves=False,
    )


def _make_rom(
    rom_id: int = 1,
    *,
    platform: str = "ngc",
    output_path: str = "gc/Metroid Prime.rvz",
    saves: tuple[SaveRecord, ...] = (),
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug=platform,
        name=Path(output_path).stem,
        source_filename=Path(output_path).name.replace(".rvz", ".zip"),
        source_md5="0" * 32,
        source_size=2048,
        source_updated_at="2026-04-01T00:00:00Z",
        transforms=("unzip",),
        outputs=(TransformedOutput(path=output_path, md5="1" * 32, size=4096),),
        primary_output_index=0,
        synced_at="2026-04-01T00:00:01Z",
        saves=saves,
    )


def _plant_rom_file(roms_base: Path, output_path: str) -> Path:
    p = roms_base / output_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake rvz content")
    return p


def _plant_gci(card_dir: Path, filename: str, content: bytes = b"x" * 8256) -> Path:
    card_dir.mkdir(parents=True, exist_ok=True)
    p = card_dir / filename
    p.write_bytes(content)
    return p


def _make_tool(headers: dict[str, DiscHeader]) -> DolphinTool:
    """Mock tool whose read_header looks up paths by string."""
    tool = MagicMock(spec=DolphinTool)
    tool.read_header = MagicMock(side_effect=lambda p: headers.get(str(p)))
    return tool


def _make_state(roms: list[RomState], *, device_id: str | None = "dev-1") -> LibraryState:
    return LibraryState(roms={r.rom_id: r for r in roms}, device_id=device_id)


def _server_save(
    *,
    save_id: int,
    rom_id: int,
    emulator: str = "dolphin",
    slot: str = "MetroidPrime A",
    file_name: str = "01-GM8E-MetroidPrime A.gci",
    file_size: int = 8256,
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
    install: DolphinInstall,
    *,
    roms_base: Path,
    headers: dict[str, DiscHeader] | None = None,
    device_id: str = "dev-1",
) -> tuple[DolphinSaveBackend, RommHttpAdapter]:
    http = RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x"))
    api = RommApi(http)
    backend = DolphinSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=_make_tool(headers or {}),
        roms_base=roms_base,
    )
    return backend, http


# ---------------------------------------------------------------------------
# Sync — case A: local-only (upload new save)
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_uploads_local_gci_with_no_server_record(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=1, output_path="gc/Metroid.rvz")
    rp = _plant_rom_file(roms_base, "gc/Metroid.rvz")
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    upload_route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=_server_save(save_id=10, rom_id=1),
        )
    )

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert upload_route.called
    assert result.uploaded == 1
    assert result.downloaded == 0
    assert result.failed == []
    new_saves = result.updated_roms[1].saves
    assert len(new_saves) == 1
    assert new_saves[0].emulator == "dolphin"
    assert new_saves[0].slot == "MetroidPrime A"
    assert new_saves[0].server_save_id == 10


# ---------------------------------------------------------------------------
# Sync — case B: server-only (download)
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_downloads_server_gci_with_no_local(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=1, output_path="gc/Metroid.rvz")
    rp = _plant_rom_file(roms_base, "gc/Metroid.rvz")
    state = _make_state([rom])

    download_bytes = b"\x00" * 8256
    download_md5 = _md5(download_bytes)

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=42, rom_id=1, md5=download_md5)],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=download_bytes)
    )

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert result.failed == []
    dest = saves_root / "USA" / "Card A" / "01-GM8E-MetroidPrime A.gci"
    assert dest.is_file()
    assert dest.read_bytes() == download_bytes


@respx.mock
def test_sync_download_strips_romm_datetime_tag_from_filename(tmp_path: Path) -> None:
    """Server file_name has ` [YYYY-MM-DD_HH-MM-SS]`; we strip on download."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Metroid.rvz")
    rp = _plant_rom_file(roms_base, "gc/Metroid.rvz")
    state = _make_state([rom])

    download_bytes = b"\x00" * 8256

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=42,
                    rom_id=1,
                    file_name="01-GM8E-MetroidPrime A [2026-04-24_15-51-34].gci",
                    md5=_md5(download_bytes),
                )
            ],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=download_bytes)
    )

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    # Local file is at the stripped path so Dolphin reads it.
    assert (saves_root / "USA" / "Card A" / "01-GM8E-MetroidPrime A.gci").is_file()
    rec = result.updated_roms[1].saves[0]
    assert rec.save_filename == "01-GM8E-MetroidPrime A.gci"


# ---------------------------------------------------------------------------
# Filtering — non-dolphin server saves are ignored
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_ignores_retroarch_server_saves(tmp_path: Path) -> None:
    """Backend filters server saves to emulator == 'dolphin'. RetroArch
    saves on the same RomM are managed by the other backend."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=1, output_path="gc/Metroid.rvz")
    _plant_rom_file(roms_base, "gc/Metroid.rvz")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=99,
                    rom_id=1,
                    emulator="retroarch-snes9x",
                    slot="default",
                    file_name="Metroid.srm",
                ),
            ],
        )
    )

    backend, http = _make_backend(install, roms_base=roms_base)
    with http:
        result = backend.sync(state)

    # No dolphin work, no failures.
    assert result.uploaded == 0
    assert result.downloaded == 0
    assert result.failed == []
    assert result.updated_roms == {}


@respx.mock
def test_sync_for_rom_only_touches_target_rom(tmp_path: Path) -> None:
    """`sync_for_rom(rom, state)` narrows the walker, server fetch, and prior
    records to a single rom. Other ROMs in state are untouched even if they
    have local saves and matching server records."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    target = _make_rom(rom_id=1, output_path="gc/Metroid.rvz")
    other = _make_rom(rom_id=2, output_path="gc/Smash.rvz")
    rp_target = _plant_rom_file(roms_base, "gc/Metroid.rvz")
    _plant_rom_file(roms_base, "gc/Smash.rvz")
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")
    _plant_gci(saves_root / "USA" / "Card A", "01-GALE-smashbros.gci")
    state = _make_state([target, other])

    # Server has saves for both ROMs; the API filter scopes to one.
    list_route = respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    upload_route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=10, rom_id=1))
    )

    headers = {
        str(rp_target): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"),
    }
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync_for_rom(target, state)

    # API was called WITH rom_id filter — exactly one list call narrowed.
    assert list_route.call_count == 1
    assert "rom_id=1" in str(list_route.calls[0].request.url)
    # Only the target rom got uploaded; Smash's local save was ignored.
    assert upload_route.called
    assert upload_route.call_count == 1
    assert result.uploaded == 1
    # State update only reflects the target rom.
    assert set(result.updated_roms.keys()) == {1}


def test_sync_for_rom_skips_when_rom_not_in_state(tmp_path: Path) -> None:
    """If the rom isn't tracked (e.g. user launched something ferry doesn't
    know about yet), `sync_for_rom` returns an empty result silently — the
    launch wrapper proceeds with whatever's on disk."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    orphan = _make_rom(rom_id=99, output_path="gc/Unknown.rvz")
    state = _make_state([])  # state has no roms at all

    backend, _ = _make_backend(install, roms_base=roms_base, headers={})
    result = backend.sync_for_rom(orphan, state)
    assert result.uploaded == 0
    assert result.downloaded == 0
    assert result.failed == []
    assert result.updated_roms == {}


def test_index_dolphin_server_saves_filters_emulator() -> None:
    """Direct unit check on the indexer with Dolphin's emulator predicate —
    non-dolphin entries are dropped."""
    from ferry.services.save_backend_base import index_server_saves

    saves = [
        _server_save(save_id=1, rom_id=1, emulator="dolphin", slot="A"),
        _server_save(save_id=2, rom_id=1, emulator="retroarch-snes9x", slot="default"),
        _server_save(save_id=3, rom_id=2, emulator="dolphin", slot="B"),
    ]
    indexed = index_server_saves(
        saves, emulator_matches=lambda e: e == "dolphin", default_slot="default"
    )
    assert set(indexed.keys()) == {(1, "dolphin", "A"), (2, "dolphin", "B")}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@respx.mock
def test_list_saves_failure_returns_failed_result(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Metroid.rvz")
    rp = _plant_rom_file(roms_base, "gc/Metroid.rvz")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(503))

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)
    assert result.failed
    assert any("could not list server saves" in f for f in result.failed)


@respx.mock
def test_download_fails_when_disc_header_unreadable(tmp_path: Path) -> None:
    """Server has a save but ferry can't read the rom's disc header
    (rom file removed). Expect a `failed` entry, not a crash."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Phantom.rvz")  # don't plant the file
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=[_server_save(save_id=42, rom_id=1)])
    )

    backend, http = _make_backend(install, roms_base=roms_base, headers={})
    with http:
        result = backend.sync(state)
    assert result.downloaded == 0
    assert any("cannot read disc header" in f for f in result.failed)


@respx.mock
def test_download_fails_when_region_unsupported(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Korean.rvz")
    rp = _plant_rom_file(roms_base, "gc/Korean.rvz")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=[_server_save(save_id=42, rom_id=1)])
    )

    headers = {str(rp): DiscHeader(game_code="GZ2K", maker_code="01", region="NTSC-K")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)
    assert result.downloaded == 0
    assert any("unsupported region" in f for f in result.failed)


# ---------------------------------------------------------------------------
# delete_for_rom
# ---------------------------------------------------------------------------


def test_delete_for_rom_trashes_local_gci_files(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Smash.rvz")
    rp = _plant_rom_file(roms_base, "gc/Smash.rvz")
    card = saves_root / "USA" / "Card A"
    g1 = _plant_gci(card, "01-GALE-smashbros_personal_data.gci")
    g2 = _plant_gci(card, "01-GALE-SuperSmashBros0110290334.gci")

    headers = {str(rp): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")}
    backend, _ = _make_backend(install, roms_base=roms_base, headers=headers)

    trash_dir = tmp_path / "trash" / "rom1"
    trash_dir.mkdir(parents=True)
    count, warnings = backend.delete_for_rom(rom, trash_dir)
    assert count == 2
    assert warnings == []
    assert not g1.exists()
    assert not g2.exists()
    # Both moved into the trash's saves/ subdir, preserving the relative path
    # under saves_root.
    trashed_card = trash_dir / "saves" / "USA" / "Card A"
    assert (trashed_card / "01-GALE-smashbros_personal_data.gci").exists()
    assert (trashed_card / "01-GALE-SuperSmashBros0110290334.gci").exists()


def test_delete_for_rom_no_local_saves_returns_zero(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Metroid.rvz")
    rp = _plant_rom_file(roms_base, "gc/Metroid.rvz")

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, _ = _make_backend(install, roms_base=roms_base, headers=headers)
    trash_dir = tmp_path / "trash" / "rom1"
    trash_dir.mkdir(parents=True)
    count, warnings = backend.delete_for_rom(rom, trash_dir)
    assert count == 0
    assert warnings == []
    assert not (trash_dir / "saves").exists()


def test_delete_for_rom_preserves_other_roms_saves(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom_smash = _make_rom(rom_id=1, output_path="gc/Smash.rvz")
    rp_smash = _plant_rom_file(roms_base, "gc/Smash.rvz")
    _plant_rom_file(roms_base, "gc/Metroid.rvz")

    card = saves_root / "USA" / "Card A"
    smash_gci = _plant_gci(card, "01-GALE-smashbros.gci")
    metroid_gci = _plant_gci(card, "01-GM8E-MetroidPrime A.gci")

    headers = {
        str(rp_smash): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U"),
        # rom_metroid not in headers — but delete_for_rom only reads the rom passed in
    }
    headers[str(roms_base / "gc/Metroid.rvz")] = DiscHeader(
        game_code="GM8E", maker_code="01", region="NTSC-U"
    )
    backend, _ = _make_backend(install, roms_base=roms_base, headers=headers)
    trash_dir = tmp_path / "trash" / "rom_smash"
    trash_dir.mkdir(parents=True)
    backend.delete_for_rom(rom_smash, trash_dir)

    assert not smash_gci.exists()
    assert metroid_gci.exists()  # untouched


# ---------------------------------------------------------------------------
# Stale-prior regression: server has it, prior says we synced it,
# walker found nothing — re-download because the local file is gone.
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_redownloads_when_local_file_was_deleted_after_prior_sync(
    tmp_path: Path,
) -> None:
    """Eternal Darkness regression: ferry synced this save before (prior
    SaveRecord exists), the user/system removed the local .gci, and now
    the walker finds nothing. With path-aware classify, we detect the
    empty resolved path and download to restore."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(
        rom_id=42,
        output_path="gc/Eternal Darkness.rvz",
        saves=(
            SaveRecord(
                emulator="dolphin",
                slot="Eternal Darkness",
                save_filename="01-GEDE-Eternal Darkness.gci",
                last_sync_md5="6425447be942f496469609c4b173cb76",
                last_sync_server_size=122944,
                last_sync_server_updated_at="2026-05-05T13:24:40+00:00",
                last_synced_at="2026-05-06T04:24:21Z",
                server_save_id=35,
            ),
        ),
    )
    rp = _plant_rom_file(roms_base, "gc/Eternal Darkness.rvz")
    state = _make_state([rom])

    download_bytes = b"\x00" * 122944
    download_md5 = _md5(download_bytes)

    # Server returns the SAME save record (unchanged since prior).
    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=35,
                    rom_id=42,
                    slot="Eternal Darkness",
                    file_name="01-GEDE-Eternal Darkness.gci",
                    file_size=122944,
                    md5=download_md5,
                    updated_at="2026-05-05T13:24:40+00:00",
                )
            ],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/35/content").mock(
        return_value=httpx.Response(200, content=download_bytes)
    )

    headers = {str(rp): DiscHeader(game_code="GEDE", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1, "stale-prior + lost-local should re-download to restore"
    dest = saves_root / "USA" / "Card A" / "01-GEDE-Eternal Darkness.gci"
    assert dest.is_file()
    assert dest.read_bytes() == download_bytes
