"""Tests for ferry.services.wii_save_backend.

End-to-end exercises with respx-mocked RomM and a tmp_path filesystem.
DolphinTool is mocked at the read_header layer so tests don't shell out.

The load-bearing integration test is `test_round_trip_upload_then_redownload_is_byte_stable`
— it proves that machine A uploads a save, machine B downloads + extracts,
and a re-walk on either side produces classify="skip" rather than spurious
re-uploads. That's the whole point of the `compute_content_hash` mirror
(ck1) + `folder_content_hash` (ck2) + transform hooks (ck3) stack.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from ferry.adapters.dolphin.dolphin_paths import DolphinInstall
from ferry.adapters.dolphin.dolphin_tool import DiscHeader, DolphinTool
from ferry.adapters.dolphin.wii_archive import (
    archive_save_folder,
    compute_content_hash,
    folder_content_hash,
)
from ferry.adapters.romm import RommApi, RommHttpAdapter
from ferry.config import RommConfig
from ferry.domain.state import LibraryState, RomState, SaveRecord, TransformedOutput
from ferry.services.wii_save_backend import WiiSaveBackend

BASE_URL = "https://romm.example.tld"

# Real Wii title_id: Metroid Prime 3 — Corruption (USA)
_TITLE_ID = 0x00010000524D3345
_TID_HIGH = "00010000"
_TID_LOW = "524d3345"
_SAVE_FILENAME = f"{_TID_HIGH}-{_TID_LOW}.zip"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_install(
    wii_saves_root: Path | None,
    *,
    saves_root: Path | None = None,
) -> DolphinInstall:
    """Build a DolphinInstall fixture; only `wii_saves_root` matters here."""
    saves_root = saves_root or (wii_saves_root.parent if wii_saves_root else Path("/nonexistent"))
    return DolphinInstall(
        source="retrodeck-flatpak",
        saves_root=saves_root,
        config_path=saves_root.parent / "Dolphin.ini",
        region_encoding="2-letter",
        settings=None,
        has_saves=False,
        wii_saves_root=wii_saves_root,
    )


def _make_rom(
    rom_id: int = 1,
    *,
    platform: str = "wii",
    output_path: str = "wii/Metroid Prime 3 (USA).rvz",
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
    p.write_bytes(b"fake wii rvz")
    return p


def _plant_save_folder(
    wii_saves_root: Path,
    files: dict[str, bytes],
    *,
    title_id_high: str = _TID_HIGH,
    title_id_low: str = _TID_LOW,
) -> Path:
    folder = wii_saves_root / title_id_high / title_id_low / "data"
    folder.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        target = folder / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return folder


def _make_tool(headers: dict[str, DiscHeader]) -> DolphinTool:
    tool = MagicMock(spec=DolphinTool)
    tool.read_header = MagicMock(side_effect=lambda p: headers.get(str(p)))
    return tool


def _wii_header(title_id: int = _TITLE_ID) -> DiscHeader:
    return DiscHeader(game_code="RM3E", maker_code="01", region="NTSC-U", title_id=title_id)


def _make_state(roms: list[RomState], *, device_id: str | None = "dev-1") -> LibraryState:
    return LibraryState(roms={r.rom_id: r for r in roms}, device_id=device_id)


def _server_save(
    *,
    save_id: int,
    rom_id: int,
    emulator: str = "dolphin",
    slot: str = "default",
    file_name: str = _SAVE_FILENAME,
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
    install: DolphinInstall,
    *,
    roms_base: Path,
    headers: dict[str, DiscHeader] | None = None,
    device_id: str = "dev-1",
) -> tuple[WiiSaveBackend, RommHttpAdapter]:
    http = RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x"))
    api = RommApi(http)
    backend = WiiSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=_make_tool(headers or {}),
        roms_base=roms_base,
    )
    return backend, http


def _build_in_memory_zip(entries: dict[str, bytes]) -> bytes:
    """Build a zip with the given entries; matches what the upload flow
    would produce locally. Used as the server-side download payload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_rejects_install_without_wii_saves_root(tmp_path: Path) -> None:
    install = _make_install(wii_saves_root=None, saves_root=tmp_path / "GC")
    http = RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x"))
    api = RommApi(http)
    with pytest.raises(ValueError, match="wii_saves_root"):
        WiiSaveBackend(
            install=install,
            api=api,
            device_id="dev-1",
            tool=_make_tool({}),
            roms_base=tmp_path,
        )


# ---------------------------------------------------------------------------
# _record_belongs_to_backend — predicate widening
# ---------------------------------------------------------------------------


def test_record_belongs_filters_to_wii_platform(tmp_path: Path) -> None:
    """GC and Wii roms with the same `dolphin` emulator tag — predicate
    accepts only the Wii platform."""
    install = _make_install(wii_saves_root=tmp_path / "wii_root")
    backend, _ = _make_backend(install, roms_base=tmp_path)
    gc_rom = _make_rom(rom_id=1, platform="ngc", output_path="gc/Metroid.rvz")
    wii_rom = _make_rom(rom_id=2, platform="wii")

    assert backend._record_belongs_to_backend(wii_rom, "dolphin") is True
    assert backend._record_belongs_to_backend(gc_rom, "dolphin") is False
    assert backend._record_belongs_to_backend(wii_rom, "retroarch") is False


# ---------------------------------------------------------------------------
# _resolve_local_path
# ---------------------------------------------------------------------------


def test_resolve_local_path_returns_save_folder(tmp_path: Path) -> None:
    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")

    backend, _ = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    dest = backend._resolve_local_path(rom, "dolphin", "default", _SAVE_FILENAME)
    assert dest == wii_root / _TID_HIGH / _TID_LOW / "data"


def test_resolve_local_path_failed_when_disc_header_missing(tmp_path: Path) -> None:
    install = _make_install(wii_saves_root=tmp_path / "wii_root")
    rom = _make_rom(rom_id=1)  # no rom file planted → header lookup will fail
    backend, _ = _make_backend(install, roms_base=tmp_path)

    from ferry.services.save_backend import SaveSyncResult

    result = SaveSyncResult()
    dest = backend._resolve_local_path(rom, "dolphin", "default", _SAVE_FILENAME, result)
    assert dest is None
    assert len(result.failed) == 1
    assert "cannot read disc header" in result.failed[0]


def test_resolve_local_path_failed_when_header_lacks_title_id(tmp_path: Path) -> None:
    """A GC disc tagged Wii by user error — header parses but no title_id."""
    install = _make_install(wii_saves_root=tmp_path / "wii_root")
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    gc_header = DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U", title_id=None)
    backend, _ = _make_backend(install, roms_base=tmp_path, headers={str(rp): gc_header})

    from ferry.services.save_backend import SaveSyncResult

    result = SaveSyncResult()
    dest = backend._resolve_local_path(rom, "dolphin", "default", _SAVE_FILENAME, result)
    assert dest is None
    assert len(result.failed) == 1
    assert "no title_id" in result.failed[0]


# ---------------------------------------------------------------------------
# Upload — the transform hook materializes a zip
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_zips_save_folder_and_posts_zip(tmp_path: Path) -> None:
    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    save_folder = _plant_save_folder(
        wii_root, {"save.bin": b"main save bytes", "banner.bin": b"banner bytes"}
    )

    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json=_server_save(save_id=42, rom_id=1))

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(side_effect=_capture)

    backend, http = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    state = _make_state([rom])
    with http:
        result = backend.sync(state)

    assert result.uploaded == 1
    assert result.failed == []
    # The captured multipart body contains a valid zip; extracting the
    # boundary-delimited content and reading it as a zip should yield
    # the same content_hash the walker computed locally.
    assert b"PK\x03\x04" in captured["body"]  # zip magic bytes present
    expected_hash = folder_content_hash(save_folder)
    assert result.updated_roms[1].saves[0].last_sync_md5 == expected_hash


@respx.mock
def test_upload_temp_zip_is_cleaned_up_after_post(tmp_path: Path) -> None:
    """The `tempfile.TemporaryDirectory` in `_pre_upload_archive`
    cleans up on context exit — no /tmp leakage after a sync."""
    import tempfile as _tempfile

    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    _plant_save_folder(wii_root, {"save.bin": b"x"})

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=42, rom_id=1))
    )

    pre_dirs = set(Path(_tempfile.gettempdir()).glob("ferry-wii-upload-*"))
    backend, http = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    state = _make_state([rom])
    with http:
        backend.sync(state)
    post_dirs = set(Path(_tempfile.gettempdir()).glob("ferry-wii-upload-*"))

    assert pre_dirs == post_dirs  # no leaks


# ---------------------------------------------------------------------------
# Download — the transform hook extracts into the save folder
# ---------------------------------------------------------------------------


@respx.mock
def test_download_extracts_zip_into_save_folder(tmp_path: Path) -> None:
    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    state = _make_state([rom])

    payload = _build_in_memory_zip(
        {"save.bin": b"server save bytes", "banner.bin": b"server banner"}
    )
    server_content_hash = compute_content_hash_from_bytes(payload)
    server = _server_save(save_id=42, rom_id=1, file_size=len(payload), md5=server_content_hash)

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    backend, http = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert result.failed == []
    save_folder = wii_root / _TID_HIGH / _TID_LOW / "data"
    assert (save_folder / "save.bin").read_bytes() == b"server save bytes"
    assert (save_folder / "banner.bin").read_bytes() == b"server banner"

    record = result.updated_roms[1].saves[0]
    # `_local_md5_from_download` prefers server.content_hash → matches
    # what the walker would compute on the extracted folder.
    assert record.last_sync_md5 == server_content_hash
    assert record.last_sync_md5 == folder_content_hash(save_folder)


@respx.mock
def test_download_temp_zip_is_cleaned_up_after_extract(tmp_path: Path) -> None:
    import tempfile as _tempfile

    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    state = _make_state([rom])

    payload = _build_in_memory_zip({"save.bin": b"x"})
    server = _server_save(save_id=42, rom_id=1, file_size=len(payload))
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    pre_dirs = set(Path(_tempfile.gettempdir()).glob("ferry-wii-download-*"))
    backend, http = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    with http:
        backend.sync(state)
    post_dirs = set(Path(_tempfile.gettempdir()).glob("ferry-wii-download-*"))

    assert pre_dirs == post_dirs  # no leaks


@respx.mock
def test_download_failure_surfaces_no_save_record(tmp_path: Path) -> None:
    """If the response payload isn't a valid zip, extraction raises an
    OSError out of the IO context; the base class routes it into
    `result.failed` and skips writing a SaveRecord (v3.5 server-as-arbiter
    contract — bytes on disk but no claim of "synced")."""
    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    state = _make_state([rom])

    server = _server_save(save_id=42, rom_id=1)
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=b"not a zip file")
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    backend, http = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    with http:
        result = backend.sync(state)

    assert result.downloaded == 0
    assert result.failed  # at least one failure recorded
    assert 1 not in result.updated_roms  # no SaveRecord written


# ---------------------------------------------------------------------------
# _local_md5_from_download — both branches
# ---------------------------------------------------------------------------


@respx.mock
def test_local_md5_falls_back_to_folder_hash_when_server_omits_content_hash(
    tmp_path: Path,
) -> None:
    """RomM 4.8.1 path: server.content_hash isn't set on PUT-modified
    saves. Backend's hook recomputes `folder_content_hash` post-extract."""
    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    state = _make_state([rom])

    payload = _build_in_memory_zip({"save.bin": b"server"})
    # Server response with NO content_hash key — simulate 4.8.1 PUT bug.
    server = {
        "id": 42,
        "rom_id": 1,
        "emulator": "dolphin",
        "slot": "default",
        "file_name": _SAVE_FILENAME,
        "file_size_bytes": len(payload),
        "updated_at": "2026-04-25T12:00:00Z",
    }
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    backend, http = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    save_folder = wii_root / _TID_HIGH / _TID_LOW / "data"
    record = result.updated_roms[1].saves[0]
    assert record.last_sync_md5 == folder_content_hash(save_folder)


# ---------------------------------------------------------------------------
# Round-trip property — the architectural promise
# ---------------------------------------------------------------------------


@respx.mock
def test_round_trip_upload_then_redownload_is_byte_stable(tmp_path: Path) -> None:
    """The whole-stack property: device A uploads a save, device B
    downloads it, and the extracted folder hashes equal on both sides.
    On A: walker_md5 == server.content_hash — classify on next sync
    yields skip (no spurious re-upload). On B: post-extract
    folder_content_hash == record.last_sync_md5 — same.
    """
    wii_root_a = tmp_path / "device_a" / "wii_root"
    wii_root_b = tmp_path / "device_b" / "wii_root"
    save_files = {"save.bin": b"shared progress", "banner.bin": b"banner data"}

    # Device A: has the save folder; uploads.
    install_a = _make_install(wii_saves_root=wii_root_a)
    rom_a = _make_rom(rom_id=1)
    rp_a = _plant_rom_file(tmp_path / "device_a" / "roms", "wii/Metroid Prime 3 (USA).rvz")
    save_folder_a = _plant_save_folder(wii_root_a, save_files)
    expected_hash = folder_content_hash(save_folder_a)

    captured_zip: dict = {}

    def _capture_upload(request: httpx.Request) -> httpx.Response:
        # Pull the zip blob out of the multipart body. The body looks
        # roughly like `--<boundary>\r\nContent-Disposition: ... \r\n\r\n<zip>\r\n--`.
        body = request.content
        start = body.index(b"PK\x03\x04")
        # End at the next boundary marker — find the trailing `\r\n--`.
        end = body.index(b"\r\n--", start)
        captured_zip["bytes"] = body[start:end]
        return httpx.Response(
            200,
            json=_server_save(
                save_id=42, rom_id=1, file_size=len(captured_zip["bytes"]), md5=expected_hash
            ),
        )

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(side_effect=_capture_upload)

    backend_a, http_a = _make_backend(
        install_a, roms_base=tmp_path / "device_a" / "roms", headers={str(rp_a): _wii_header()}
    )
    with http_a:
        result_a = backend_a.sync(_make_state([rom_a]))
    assert result_a.uploaded == 1
    assert result_a.updated_roms[1].saves[0].last_sync_md5 == expected_hash

    # Device B: empty saves dir; downloads the captured zip; should
    # extract to a folder whose hash matches what A computed.
    respx.reset()
    install_b = _make_install(wii_saves_root=wii_root_b)
    rom_b = _make_rom(rom_id=1)
    rp_b = _plant_rom_file(tmp_path / "device_b" / "roms", "wii/Metroid Prime 3 (USA).rvz")
    server = _server_save(
        save_id=42, rom_id=1, file_size=len(captured_zip["bytes"]), md5=expected_hash
    )
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=captured_zip["bytes"])
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    backend_b, http_b = _make_backend(
        install_b, roms_base=tmp_path / "device_b" / "roms", headers={str(rp_b): _wii_header()}
    )
    with http_b:
        result_b = backend_b.sync(_make_state([rom_b]))

    assert result_b.downloaded == 1
    save_folder_b = wii_root_b / _TID_HIGH / _TID_LOW / "data"
    assert (save_folder_b / "save.bin").read_bytes() == save_files["save.bin"]
    assert (save_folder_b / "banner.bin").read_bytes() == save_files["banner.bin"]
    assert folder_content_hash(save_folder_b) == expected_hash
    assert result_b.updated_roms[1].saves[0].last_sync_md5 == expected_hash


# ---------------------------------------------------------------------------
# delete_for_rom — Wii folder gets trashed
# ---------------------------------------------------------------------------


def test_delete_for_rom_trashes_wii_save_files(tmp_path: Path) -> None:
    """`_saves_root` returns wii_saves_root, so the trash relpath
    structure is `<trash>/saves/<HIGH>/<LOW>/data/...`."""
    wii_root = tmp_path / "wii_root"
    install = _make_install(wii_saves_root=wii_root)
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(tmp_path, "wii/Metroid Prime 3 (USA).rvz")
    save_folder = _plant_save_folder(wii_root, {"save.bin": b"x", "banner.bin": b"y"})

    backend, _ = _make_backend(install, roms_base=tmp_path, headers={str(rp): _wii_header()})
    trash = tmp_path / "trash"
    count, warnings = backend.delete_for_rom(rom, trash)

    # Walker emits one LocalSave per Wii title (slot=default, local_path=folder),
    # so delete_for_rom moves the folder once.
    assert count == 1
    assert warnings == []
    # The save folder is gone from disk.
    assert not save_folder.exists()
    # And lives under <trash>/saves at its relpath.
    relocated = trash / "saves" / _TID_HIGH / _TID_LOW / "data"
    assert relocated.exists()


# ---------------------------------------------------------------------------
# Helpers used by tests above
# ---------------------------------------------------------------------------


def compute_content_hash_from_bytes(zip_bytes: bytes) -> str:
    """Compute RomM-style content_hash directly from in-memory zip bytes
    (mirrors `compute_content_hash` but without the disk round-trip).
    Lets test cases assert end-to-end hash equivalence without writing
    a temp file."""
    file_hashes = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith("/"):
                continue
            content = zf.read(name)
            file_hashes.append(f"{name}:{hashlib.md5(content).hexdigest()}")
    combined = "\n".join(file_hashes)
    return hashlib.md5(combined.encode()).hexdigest()


# Sanity-check the helper to keep parity with `compute_content_hash`.
def test_helper_compute_content_hash_from_bytes_matches_compute_content_hash(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "save.bin").write_bytes(b"main save bytes")
    (src / "banner.bin").write_bytes(b"banner bytes")
    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)

    assert compute_content_hash_from_bytes(archive.read_bytes()) == compute_content_hash(archive)
