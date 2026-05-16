"""Tests for ferry.services.cemu_save_backend.

End-to-end exercises with respx-mocked RomM and a tmp_path filesystem.
Title-ID resolution is driven through a pre-seeded `WiiUTitleCache` so
the tests never shell out to Cemu — a cache hit short-circuits
`lookup_wiiu_title` before it would invoke the tool.

The folder↔zip transform mechanics are shared verbatim with the Wii
backend (`dolphin_archive`); the Cemu-specific surface tested here is
title resolution, the `cemu` emulator tag, and `_resolve_local_path`.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
import respx

from ferry.adapters.cemu.cemu_paths import CemuInstall
from ferry.adapters.cemu.cemu_tool import CemuTool, WiiUTitle, WiiUTitleCache
from ferry.adapters.dolphin.dolphin_archive import compute_content_hash, folder_content_hash
from ferry.adapters.romm import RommApi, RommHttpAdapter
from ferry.config import RommConfig
from ferry.domain.state import LibraryState, RomState, SaveRecord, TransformedOutput
from ferry.services.cemu_save_backend import CemuSaveBackend

BASE_URL = "https://romm.example.tld"

_BOTW_TITLE_ID = "00050000101C9400"
_BOTW_HIGH = "00050000"
_BOTW_LOW = "101c9400"
_ROM_BASE_NAME = "Breath of the Wild (USA)"
_BUNDLE_FILENAME = f"{_ROM_BASE_NAME}.zip"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_install(tmp_path: Path) -> CemuInstall:
    return CemuInstall(
        source="retrodeck-flatpak",
        wiiu_saves_root=tmp_path / "wiiu",
        data_dir=tmp_path / "data" / "Cemu",
        has_saves=False,
    )


def _make_rom(
    rom_id: int = 1,
    *,
    platform: str = "wiiu",
    output_path: str = "wiiu/Breath of the Wild (USA).wux",
    saves: tuple[SaveRecord, ...] = (),
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug=platform,
        name=Path(output_path).stem,
        source_filename=Path(output_path).name,
        source_md5="0" * 32,
        source_size=2048,
        source_updated_at="2026-04-01T00:00:00Z",
        transforms=(),
        outputs=(TransformedOutput(path=output_path, md5="1" * 32, size=4096),),
        primary_output_index=0,
        synced_at="2026-04-01T00:00:01Z",
        saves=saves,
    )


def _plant_rom_file(roms_base: Path, output_path: str) -> Path:
    p = roms_base / output_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake wux content")
    return p


def _plant_save_folder(
    wiiu_saves_root: Path,
    files: dict[str, bytes],
    *,
    title_id_high: str = _BOTW_HIGH,
    title_id_low: str = _BOTW_LOW,
) -> Path:
    """Plant *files* under `<root>/<HIGH>/<LOW>/`; returns the per-title folder."""
    folder = wiiu_saves_root / title_id_high / title_id_low
    folder.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        target = folder / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return folder


def _dummy_tool() -> CemuTool:
    """A CemuTool that's never actually invoked (cache hits short-circuit)."""
    return CemuTool(
        source="retrodeck-in-sandbox",
        label="test",
        argv_prefix=("sh", "-c", "<snippet>", "_"),
        cwd_via_snippet=True,
    )


def _seeded_cache(tmp_path: Path, rom_path: Path, title_id: str = _BOTW_TITLE_ID) -> WiiUTitleCache:
    """A title cache pre-seeded for *rom_path* so the backend resolves a
    title without invoking Cemu."""
    cache = WiiUTitleCache(tmp_path / "wiiu-titles.json")
    cache.put(rom_path, WiiUTitle(title_id=title_id))
    return cache


def _make_state(roms: list[RomState], *, device_id: str | None = "dev-1") -> LibraryState:
    return LibraryState(roms={r.rom_id: r for r in roms}, device_id=device_id)


def _server_save(
    *,
    save_id: int,
    rom_id: int,
    emulator: str = "cemu",
    slot: str = _ROM_BASE_NAME,
    file_name: str = _BUNDLE_FILENAME,
    file_size: int = 2048,
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
    install: CemuInstall,
    *,
    roms_base: Path,
    cache: WiiUTitleCache | None = None,
    device_id: str = "dev-1",
) -> tuple[CemuSaveBackend, RommHttpAdapter]:
    http = RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x"))
    api = RommApi(http)
    backend = CemuSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=_dummy_tool(),
        roms_base=roms_base,
        cache=cache,
    )
    return backend, http


def _build_bundle_zip(entries: dict[str, bytes], *, wrapper: str = _BOTW_LOW) -> bytes:
    """Wrapper-prefixed zip mirroring `archive_save_folder` output — the
    server-side download payload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name in sorted(entries):
            zf.writestr(f"{wrapper}/{name}" if wrapper else name, entries[name])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _record_belongs_to_backend
# ---------------------------------------------------------------------------


def test_record_belongs_accepts_cemu_for_wiiu_platform(tmp_path: Path) -> None:
    install = _make_install(tmp_path)
    backend, _ = _make_backend(install, roms_base=tmp_path)
    wiiu_rom = _make_rom(rom_id=1, platform="wiiu")
    snes_rom = _make_rom(rom_id=2, platform="snes", output_path="snes/Mario.smc")

    assert backend._record_belongs_to_backend(wiiu_rom, "cemu") is True
    # `cemu` tag on a non-Wii-U platform — defensive platform check rejects.
    assert backend._record_belongs_to_backend(snes_rom, "cemu") is False
    # Wrong emulator tag.
    assert backend._record_belongs_to_backend(wiiu_rom, "dolphin") is False
    assert backend._record_belongs_to_backend(wiiu_rom, "retroarch") is False


# ---------------------------------------------------------------------------
# _resolve_local_path
# ---------------------------------------------------------------------------


def test_resolve_local_path_returns_title_folder(tmp_path: Path) -> None:
    install = _make_install(tmp_path)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path / "roms", "wiiu/Breath of the Wild (USA).wux")
    backend, _ = _make_backend(
        install, roms_base=tmp_path / "roms", cache=_seeded_cache(tmp_path, rp)
    )

    dest = backend._resolve_local_path(rom, "cemu", _ROM_BASE_NAME, _BUNDLE_FILENAME)
    assert dest == install.wiiu_saves_root / _BOTW_HIGH / _BOTW_LOW


def test_resolve_local_path_failed_when_title_unresolvable(tmp_path: Path) -> None:
    install = _make_install(tmp_path)
    rom = _make_rom(rom_id=1)  # no rom file planted → title can't be resolved
    backend, _ = _make_backend(install, roms_base=tmp_path / "roms")

    from ferry.services.save_backend import SaveSyncResult

    result = SaveSyncResult()
    dest = backend._resolve_local_path(rom, "cemu", _ROM_BASE_NAME, _BUNDLE_FILENAME, result)
    assert dest is None
    assert len(result.failed) == 1
    assert "cannot extract Wii U title ID" in result.failed[0]


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_zips_save_folder_and_posts_zip(tmp_path: Path) -> None:
    install = _make_install(tmp_path)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path / "roms", "wiiu/Breath of the Wild (USA).wux")
    title_folder = _plant_save_folder(
        install.wiiu_saves_root,
        {"user/80000001/game_data.sav": b"progress", "meta/meta.xml": b"<menu/>"},
    )
    state = _make_state([rom])

    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json=_server_save(save_id=42, rom_id=1))

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(side_effect=_capture)

    backend, http = _make_backend(
        install, roms_base=tmp_path / "roms", cache=_seeded_cache(tmp_path, rp)
    )
    with http:
        result = backend.sync(state)

    assert result.uploaded == 1
    assert result.failed == []
    assert b"PK\x03\x04" in captured["body"]
    record = result.updated_roms[1].saves[0]
    assert record.emulator == "cemu"
    assert record.slot == _ROM_BASE_NAME
    assert record.save_filename == _BUNDLE_FILENAME
    assert record.last_sync_md5 == folder_content_hash(title_folder)


@respx.mock
def test_upload_temp_zip_is_cleaned_up(tmp_path: Path) -> None:
    import tempfile as _tempfile

    install = _make_install(tmp_path)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path / "roms", "wiiu/Breath of the Wild (USA).wux")
    _plant_save_folder(install.wiiu_saves_root, {"user/80000001/x.sav": b"x"})
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=42, rom_id=1))
    )

    pre = set(Path(_tempfile.gettempdir()).glob("ferry-cemu-upload-*"))
    backend, http = _make_backend(
        install, roms_base=tmp_path / "roms", cache=_seeded_cache(tmp_path, rp)
    )
    with http:
        backend.sync(state)
    assert set(Path(_tempfile.gettempdir()).glob("ferry-cemu-upload-*")) == pre


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@respx.mock
def test_download_extracts_zip_into_save_folder(tmp_path: Path) -> None:
    install = _make_install(tmp_path)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path / "roms", "wiiu/Breath of the Wild (USA).wux")
    state = _make_state([rom])

    payload = _build_bundle_zip(
        {"user/80000001/game_data.sav": b"server progress", "meta/meta.xml": b"<menu/>"}
    )
    server_hash = _content_hash_from_bytes(payload)
    server = _server_save(save_id=42, rom_id=1, file_size=len(payload), md5=server_hash)

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    backend, http = _make_backend(
        install, roms_base=tmp_path / "roms", cache=_seeded_cache(tmp_path, rp)
    )
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert result.failed == []
    title_folder = install.wiiu_saves_root / _BOTW_HIGH / _BOTW_LOW
    assert (title_folder / "user" / "80000001" / "game_data.sav").read_bytes() == b"server progress"
    assert (title_folder / "meta" / "meta.xml").read_bytes() == b"<menu/>"
    assert result.updated_roms[1].saves[0].last_sync_md5 == server_hash


@respx.mock
def test_download_failure_surfaces_no_save_record(tmp_path: Path) -> None:
    """Non-zip payload → OSError out of the IO context → failed, no record."""
    install = _make_install(tmp_path)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path / "roms", "wiiu/Breath of the Wild (USA).wux")
    state = _make_state([rom])

    server = _server_save(save_id=42, rom_id=1)
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=b"not a zip")
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    backend, http = _make_backend(
        install, roms_base=tmp_path / "roms", cache=_seeded_cache(tmp_path, rp)
    )
    with http:
        result = backend.sync(state)

    assert result.downloaded == 0
    assert result.failed
    assert 1 not in result.updated_roms


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@respx.mock
def test_round_trip_upload_then_redownload_is_byte_stable(tmp_path: Path) -> None:
    """Device A uploads, device B downloads + extracts; both compute the
    same content_hash. The whole-stack three-way invariant."""
    save_files = {
        "user/80000001/game_data.sav": b"shared progress",
        "user/common/option.sav": b"options",
        "meta/meta.xml": b"<menu/>",
    }

    install_a = CemuInstall(
        source="retrodeck-flatpak",
        wiiu_saves_root=tmp_path / "a" / "wiiu",
        data_dir=tmp_path / "a" / "data",
        has_saves=False,
    )
    rom_a = _make_rom(rom_id=1)
    rp_a = _plant_rom_file(tmp_path / "a" / "roms", "wiiu/Breath of the Wild (USA).wux")
    title_folder_a = _plant_save_folder(install_a.wiiu_saves_root, save_files)
    expected_hash = folder_content_hash(title_folder_a)

    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        body = request.content
        start = body.index(b"PK\x03\x04")
        end = body.index(b"\r\n--", start)
        captured["bytes"] = body[start:end]
        return httpx.Response(
            200,
            json=_server_save(
                save_id=42, rom_id=1, file_size=len(captured["bytes"]), md5=expected_hash
            ),
        )

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(side_effect=_capture)

    cache_a = WiiUTitleCache(tmp_path / "a" / "titles.json")
    cache_a.put(rp_a, WiiUTitle(title_id=_BOTW_TITLE_ID))
    backend_a, http_a = _make_backend(install_a, roms_base=tmp_path / "a" / "roms", cache=cache_a)
    with http_a:
        result_a = backend_a.sync(_make_state([rom_a]))
    assert result_a.uploaded == 1

    # Device B: empty saves dir; downloads the captured zip.
    respx.reset()
    install_b = CemuInstall(
        source="retrodeck-flatpak",
        wiiu_saves_root=tmp_path / "b" / "wiiu",
        data_dir=tmp_path / "b" / "data",
        has_saves=False,
    )
    rom_b = _make_rom(rom_id=1)
    rp_b = _plant_rom_file(tmp_path / "b" / "roms", "wiiu/Breath of the Wild (USA).wux")
    server = _server_save(save_id=42, rom_id=1, file_size=len(captured["bytes"]), md5=expected_hash)
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=captured["bytes"])
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    cache_b = WiiUTitleCache(tmp_path / "b" / "titles.json")
    cache_b.put(rp_b, WiiUTitle(title_id=_BOTW_TITLE_ID))
    backend_b, http_b = _make_backend(install_b, roms_base=tmp_path / "b" / "roms", cache=cache_b)
    with http_b:
        result_b = backend_b.sync(_make_state([rom_b]))

    assert result_b.downloaded == 1
    title_folder_b = install_b.wiiu_saves_root / _BOTW_HIGH / _BOTW_LOW
    for name, content in save_files.items():
        assert (title_folder_b / name).read_bytes() == content
    assert folder_content_hash(title_folder_b) == expected_hash


# ---------------------------------------------------------------------------
# delete_for_rom
# ---------------------------------------------------------------------------


def test_delete_for_rom_trashes_wiiu_save_folder(tmp_path: Path) -> None:
    install = _make_install(tmp_path)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path / "roms", "wiiu/Breath of the Wild (USA).wux")
    title_folder = _plant_save_folder(
        install.wiiu_saves_root, {"user/80000001/game_data.sav": b"x", "meta/meta.xml": b"y"}
    )
    backend, _ = _make_backend(
        install, roms_base=tmp_path / "roms", cache=_seeded_cache(tmp_path, rp)
    )

    trash = tmp_path / "trash"
    count, warnings = backend.delete_for_rom(rom, trash)

    assert count == 1
    assert warnings == []
    assert not title_folder.exists()
    relocated = trash / "saves" / _BOTW_HIGH / _BOTW_LOW
    assert (relocated / "user" / "80000001" / "game_data.sav").read_bytes() == b"x"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content_hash_from_bytes(zip_bytes: bytes) -> str:
    """RomM-style content_hash of an in-memory zip (no disk round-trip)."""
    file_hashes = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith("/"):
                continue
            file_hashes.append(f"{name}:{hashlib.md5(zf.read(name)).hexdigest()}")
    return hashlib.md5("\n".join(file_hashes).encode()).hexdigest()


def test_helper_content_hash_from_bytes_matches_compute_content_hash(tmp_path: Path) -> None:
    payload = _build_bundle_zip({"user/x.sav": b"a", "meta/meta.xml": b"b"})
    archive = tmp_path / "out.zip"
    archive.write_bytes(payload)
    assert _content_hash_from_bytes(payload) == compute_content_hash(archive)
