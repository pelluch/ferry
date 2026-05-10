"""Tests for ferry.services.gamecube_save_backend (v3.7 ck2).

End-to-end exercises with respx-mocked RomM and a tmp_path filesystem.
DolphinTool is mocked at the read_header layer so tests don't shell out.

v3.7 schema: server records are wrapper-prefixed zip bundles of all
matched GCIs (Card A + Card B) per rom. Walker emits ONE LocalSave per
rom (slot=`<rom_base_name>`, save_filename=`<rom_base_name>.zip`).
Backend transform hooks zip-on-upload and extract-on-download (all
extracted GCIs land in Card A). Round-trip preserves the three-way
content_hash invariant with RomM and Argosy.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import respx

from ferry.adapters.dolphin.dolphin_paths import DolphinInstall, RegionEncoding
from ferry.adapters.dolphin.dolphin_tool import DiscHeader, DolphinTool
from ferry.adapters.dolphin.wii_archive import (
    compute_content_hash,
    files_content_hash,
)
from ferry.adapters.romm import RommApi, RommHttpAdapter
from ferry.config import RommConfig
from ferry.domain.state import LibraryState, RomState, SaveRecord, TransformedOutput
from ferry.services.gamecube_save_backend import GameCubeSaveBackend

BASE_URL = "https://romm.example.tld"

# Default rom for tests: output_path stem = "Metroid Prime (USA)" so
# slot/filename are derived predictably.
_ROM_BASE_NAME = "Metroid Prime (USA)"
_BUNDLE_FILENAME = f"{_ROM_BASE_NAME}.zip"
_DOLPHIN_TAG = "dolphin"


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
    output_path: str = "gc/Metroid Prime (USA).rvz",
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
    emulator: str = _DOLPHIN_TAG,
    slot: str = _ROM_BASE_NAME,
    file_name: str = _BUNDLE_FILENAME,
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
) -> tuple[GameCubeSaveBackend, RommHttpAdapter]:
    http = RommHttpAdapter(RommConfig(url=BASE_URL, api_key="rmm_x"))
    api = RommApi(http)
    backend = GameCubeSaveBackend(
        install=install,
        api=api,
        device_id=device_id,
        tool=_make_tool(headers or {}),
        roms_base=roms_base,
    )
    return backend, http


def _build_bundle_zip(entries: dict[str, bytes], *, wrapper: str = _ROM_BASE_NAME) -> bytes:
    """Build a wrapper-prefixed bundle zip mirroring `archive_files` output.

    Used as the server-side download payload. Pass an empty wrapper for
    a flat zip (defensive test of the no-wrapper extract branch).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name in sorted(entries):
            arcname = f"{wrapper}/{name}" if wrapper else name
            zf.writestr(arcname, entries[name])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Upload — bundle the matched GCIs into one zip and POST
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_bundles_matched_gcis_and_posts_zip(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    g1 = _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci", b"a" * 100)
    g2 = _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime B.gci", b"b" * 200)
    state = _make_state([rom])

    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json=_server_save(save_id=42, rom_id=1))

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(side_effect=_capture)

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.uploaded == 1
    assert result.failed == []
    assert b"PK\x03\x04" in captured["body"]
    expected_hash = files_content_hash([g1, g2], wrapper=_ROM_BASE_NAME)
    assert result.updated_roms[1].saves[0].last_sync_md5 == expected_hash
    assert result.updated_roms[1].saves[0].slot == _ROM_BASE_NAME
    assert result.updated_roms[1].saves[0].save_filename == _BUNDLE_FILENAME


@respx.mock
def test_upload_includes_card_b_gcis(tmp_path: Path) -> None:
    """v3.7 widens upload scope to Card A + Card B (Argosy parity).
    The bundle's hash includes both cards' contents."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    g_a = _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci", b"a-bytes")
    g_b = _plant_gci(saves_root / "USA" / "Card B", "01-GM8E-MetroidPrime B.gci", b"b-bytes")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=42, rom_id=1))
    )

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.uploaded == 1
    expected = files_content_hash([g_a, g_b], wrapper=_ROM_BASE_NAME)
    assert result.updated_roms[1].saves[0].last_sync_md5 == expected


@respx.mock
def test_upload_temp_zip_is_cleaned_up(tmp_path: Path) -> None:
    import tempfile as _tempfile

    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-foo.gci")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=42, rom_id=1))
    )

    pre_dirs = set(Path(_tempfile.gettempdir()).glob("ferry-gc-upload-*"))
    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        backend.sync(state)
    post_dirs = set(Path(_tempfile.gettempdir()).glob("ferry-gc-upload-*"))
    assert pre_dirs == post_dirs


# ---------------------------------------------------------------------------
# Download — extract zip and route GCIs to <region>/Card A/
# ---------------------------------------------------------------------------


@respx.mock
def test_download_extracts_bundle_into_card_a(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    state = _make_state([rom])

    payload = _build_bundle_zip(
        {
            "01-GM8E-MetroidPrime A.gci": b"server-A",
            "01-GM8E-MetroidPrime B.gci": b"server-B",
        }
    )
    server_hash = compute_content_hash_from_bytes(payload)
    server = _server_save(save_id=42, rom_id=1, file_size=len(payload), md5=server_hash)

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert result.failed == []
    card_a = saves_root / "USA" / "Card A"
    assert (card_a / "01-GM8E-MetroidPrime A.gci").read_bytes() == b"server-A"
    assert (card_a / "01-GM8E-MetroidPrime B.gci").read_bytes() == b"server-B"

    record = result.updated_roms[1].saves[0]
    assert record.last_sync_md5 == server_hash


@respx.mock
def test_download_strips_romm_datetime_tag_from_filename(tmp_path: Path) -> None:
    """Server file_name has ` [YYYY-MM-DD_HH-MM-SS]`; strip on download.
    Local filename inside the bundle is unaffected — datetime tag is on
    the OUTER zip name only."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    state = _make_state([rom])

    payload = _build_bundle_zip({"01-GM8E-foo.gci": b"\x00" * 8256})

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=42,
                    rom_id=1,
                    file_name=f"{_ROM_BASE_NAME} [2026-04-24_15-51-34].zip",
                    file_size=len(payload),
                    md5=compute_content_hash_from_bytes(payload),
                )
            ],
        )
    )
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1
    assert (saves_root / "USA" / "Card A" / "01-GM8E-foo.gci").is_file()
    rec = result.updated_roms[1].saves[0]
    assert rec.save_filename == _BUNDLE_FILENAME  # datetime tag stripped


@respx.mock
def test_download_fails_when_disc_header_unreadable(tmp_path: Path) -> None:
    """Server has a bundle but ferry can't read the rom's disc header
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


@respx.mock
def test_download_fails_loudly_on_corrupt_zip(tmp_path: Path) -> None:
    """Non-zip bytes from the server → OSError → failed (no SaveRecord).
    v3.5 server-as-arbiter contract: bytes can be on disk (atomic .part
    rename) but no claim of `synced` is recorded."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=[_server_save(save_id=42, rom_id=1)])
    )
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=b"not a zip file at all")
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 0
    assert result.failed
    assert 1 not in result.updated_roms


# ---------------------------------------------------------------------------
# Round-trip — A uploads, B downloads, both hashes match
# ---------------------------------------------------------------------------


@respx.mock
def test_round_trip_upload_then_redownload_is_byte_stable(tmp_path: Path) -> None:
    """The whole-stack property: device A uploads a bundle, device B
    downloads + extracts, both compute identical content_hash. classify
    on either side after sync yields skip rather than spurious
    re-upload."""
    saves_root_a = tmp_path / "device_a" / "GC"
    saves_root_b = tmp_path / "device_b" / "GC"
    save_files = {
        "01-GM8E-MetroidPrime A.gci": b"shared-progress",
        "01-GM8E-MetroidPrime B.gci": b"second-slot",
    }

    install_a = _make_install(saves_root_a)
    rom_a = _make_rom(rom_id=1)
    rp_a = _plant_rom_file(tmp_path / "device_a" / "roms", "gc/Metroid Prime (USA).rvz")
    planted_a: list[Path] = []
    for name, content in save_files.items():
        planted_a.append(_plant_gci(saves_root_a / "USA" / "Card A", name, content))
    expected_hash = files_content_hash(planted_a, wrapper=_ROM_BASE_NAME)

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

    headers_a = {str(rp_a): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend_a, http_a = _make_backend(
        install_a, roms_base=tmp_path / "device_a" / "roms", headers=headers_a
    )
    with http_a:
        result_a = backend_a.sync(_make_state([rom_a]))
    assert result_a.uploaded == 1
    assert result_a.updated_roms[1].saves[0].last_sync_md5 == expected_hash

    # Device B: empty saves dir; downloads the captured zip.
    respx.reset()
    install_b = _make_install(saves_root_b)
    rom_b = _make_rom(rom_id=1)
    rp_b = _plant_rom_file(tmp_path / "device_b" / "roms", "gc/Metroid Prime (USA).rvz")
    server = _server_save(save_id=42, rom_id=1, file_size=len(captured["bytes"]), md5=expected_hash)
    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[server]))
    respx.get(f"{BASE_URL}/api/saves/42/content").mock(
        return_value=httpx.Response(200, content=captured["bytes"])
    )
    respx.post(f"{BASE_URL}/api/saves/42/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )

    headers_b = {str(rp_b): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend_b, http_b = _make_backend(
        install_b, roms_base=tmp_path / "device_b" / "roms", headers=headers_b
    )
    with http_b:
        result_b = backend_b.sync(_make_state([rom_b]))

    assert result_b.downloaded == 1
    card_b = saves_root_b / "USA" / "Card A"  # extract destination is Card A
    for name, content in save_files.items():
        assert (card_b / name).read_bytes() == content
    redownloaded_paths = sorted((card_b / name) for name in save_files)
    assert files_content_hash(redownloaded_paths, wrapper=_ROM_BASE_NAME) == expected_hash
    assert result_b.updated_roms[1].saves[0].last_sync_md5 == expected_hash


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

    rom = _make_rom(rom_id=1)
    _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
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

    assert result.uploaded == 0
    assert result.downloaded == 0
    assert result.failed == []
    assert result.updated_roms == {}


@respx.mock
def test_sync_ignores_dolphin_wii_server_saves(tmp_path: Path) -> None:
    """v3.7 ck1: Wii records carry `dolphin_wii`. GC backend only owns
    the bare `dolphin` tag — Wii records routed through the Wii backend."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1)
    _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[_server_save(save_id=99, rom_id=1, emulator="dolphin_wii")],
        )
    )

    backend, http = _make_backend(install, roms_base=roms_base)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 0
    assert result.failed == []
    assert result.updated_roms == {}


@respx.mock
def test_sync_for_rom_only_touches_target_rom(tmp_path: Path) -> None:
    """`sync_for_rom(rom, state)` narrows the walker, server fetch, and prior
    records to a single rom."""
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

    list_route = respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    upload_route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=_server_save(save_id=10, rom_id=1, slot="Metroid"))
    )

    headers = {
        str(rp_target): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"),
    }
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync_for_rom(target, state)

    assert list_route.call_count == 1
    assert "rom_id=1" in str(list_route.calls[0].request.url)
    assert upload_route.called
    assert upload_route.call_count == 1
    assert result.uploaded == 1
    assert set(result.updated_roms.keys()) == {1}


def test_sync_for_rom_skips_when_rom_not_in_state(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    orphan = _make_rom(rom_id=99, output_path="gc/Unknown.rvz")
    state = _make_state([])

    backend, _ = _make_backend(install, roms_base=roms_base, headers={})
    result = backend.sync_for_rom(orphan, state)
    assert result.uploaded == 0
    assert result.downloaded == 0
    assert result.failed == []
    assert result.updated_roms == {}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@respx.mock
def test_list_saves_failure_returns_failed_result(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")
    state = _make_state([rom])

    respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(503))

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)
    assert result.failed
    assert any("could not list server saves" in f for f in result.failed)


# ---------------------------------------------------------------------------
# delete_for_rom — re-matches and trashes individual GCIs across both cards
# ---------------------------------------------------------------------------


def test_delete_for_rom_trashes_local_gci_files_across_cards(tmp_path: Path) -> None:
    """Override re-runs `match_rom_gcis`, so both Card A and Card B
    files for this rom get moved. Base class default would have moved
    the entire saves_root sentinel — this regression check pins that."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Smash.rvz")
    rp = _plant_rom_file(roms_base, "gc/Smash.rvz")
    g_a1 = _plant_gci(saves_root / "USA" / "Card A", "01-GALE-personal.gci")
    g_a2 = _plant_gci(saves_root / "USA" / "Card A", "01-GALE-replay-1.gci")
    g_b = _plant_gci(saves_root / "USA" / "Card B", "01-GALE-overflow.gci")
    # Unrelated rom's GCI — must NOT be trashed.
    other = _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")

    headers = {str(rp): DiscHeader(game_code="GALE", maker_code="01", region="NTSC-U")}
    backend, _ = _make_backend(install, roms_base=roms_base, headers=headers)

    trash_dir = tmp_path / "trash" / "rom1"
    trash_dir.mkdir(parents=True)
    count, warnings = backend.delete_for_rom(rom, trash_dir)
    assert count == 3  # both Card A files + Card B file
    assert warnings == []
    assert not g_a1.exists()
    assert not g_a2.exists()
    assert not g_b.exists()
    assert other.exists()  # unrelated rom untouched
    # Trashed at <trash>/saves/<region>/<card>/<filename> preserving relpath.
    trashed_card_a = trash_dir / "saves" / "USA" / "Card A"
    trashed_card_b = trash_dir / "saves" / "USA" / "Card B"
    assert (trashed_card_a / "01-GALE-personal.gci").exists()
    assert (trashed_card_a / "01-GALE-replay-1.gci").exists()
    assert (trashed_card_b / "01-GALE-overflow.gci").exists()


def test_delete_for_rom_no_local_saves_returns_zero(tmp_path: Path) -> None:
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1)
    rp = _plant_rom_file(roms_base, "gc/Metroid Prime (USA).rvz")

    headers = {str(rp): DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")}
    backend, _ = _make_backend(install, roms_base=roms_base, headers=headers)
    trash_dir = tmp_path / "trash" / "rom1"
    trash_dir.mkdir(parents=True)
    count, warnings = backend.delete_for_rom(rom, trash_dir)
    assert count == 0
    assert warnings == []
    assert not (trash_dir / "saves").exists()


def test_delete_for_rom_disc_header_unreadable_warns_no_crash(tmp_path: Path) -> None:
    """Defensive: rom file gone → can't read header → can't match GCIs.
    Surface as warning instead of trashing things."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"
    rom = _make_rom(rom_id=1, output_path="gc/Phantom.rvz")  # don't plant

    backend, _ = _make_backend(install, roms_base=roms_base, headers={})
    trash_dir = tmp_path / "trash" / "rom1"
    trash_dir.mkdir(parents=True)
    count, warnings = backend.delete_for_rom(rom, trash_dir)
    assert count == 0
    assert any("cannot read disc header" in w for w in warnings)


# ---------------------------------------------------------------------------
# Stale-prior regression: server has it, prior says we synced it,
# walker found nothing — re-download because the local file is gone.
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_redownloads_when_local_file_was_deleted_after_prior_sync(
    tmp_path: Path,
) -> None:
    """Eternal Darkness regression: ferry synced this save before, the
    user/system removed the local .gci, and now the walker finds
    nothing. With path-aware classify, we detect the empty resolved
    path and download to restore. Server payload is the v3.7 bundle
    zip; restored file lands in Card A.

    Crucially: `<region>/Card A/` is populated with UNRELATED ROMs'
    GCIs, mirroring the real-world live-test case. The base probe
    `dir.stat()` would succeed and report "local fine" — only the
    GC backend's override, which checks `match_rom_gcis` directly,
    correctly reports lost-local.
    """
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(
        rom_id=42,
        output_path="gc/Eternal Darkness (USA).rvz",
        saves=(
            SaveRecord(
                emulator="dolphin",
                slot="Eternal Darkness (USA)",
                save_filename="Eternal Darkness (USA).zip",
                last_sync_md5="prior-md5-placeholder",
                last_sync_server_size=122944,
                last_sync_server_updated_at="2026-05-05T13:24:40+00:00",
                last_synced_at="2026-05-06T04:24:21Z",
                server_save_id=35,
            ),
        ),
    )
    rp = _plant_rom_file(roms_base, "gc/Eternal Darkness (USA).rvz")
    # Plant unrelated GCIs in Card A to make the SHARED directory exist
    # and be non-empty — pins the regression: base `dir.stat()` probe
    # would report "exists" and skip download. The override re-runs
    # match_rom_gcis (filtered by GEDE prefix) → no matches for Eternal
    # Darkness even though the dir is full of other games.
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")
    _plant_gci(saves_root / "USA" / "Card A", "01-GALE-smashbros.gci")
    state = _make_state([rom])

    payload = _build_bundle_zip(
        {"01-GEDE-Eternal Darkness.gci": b"\x00" * 122944},
        wrapper="Eternal Darkness (USA)",
    )
    payload_hash = compute_content_hash_from_bytes(payload)

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=35,
                    rom_id=42,
                    slot="Eternal Darkness (USA)",
                    file_name="Eternal Darkness (USA).zip",
                    file_size=len(payload),
                    md5=payload_hash,
                    updated_at="2026-05-05T13:24:40+00:00",
                )
            ],
        )
    )
    respx.post(f"{BASE_URL}/api/saves/35/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 35})
    )
    respx.get(f"{BASE_URL}/api/saves/35/content").mock(
        return_value=httpx.Response(200, content=payload)
    )

    headers = {str(rp): DiscHeader(game_code="GEDE", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1, "stale-prior + lost-local should re-download to restore"
    dest = saves_root / "USA" / "Card A" / "01-GEDE-Eternal Darkness.gci"
    assert dest.is_file()
    # Other ROMs' GCIs still present — extract didn't clobber the directory.
    assert (saves_root / "USA" / "Card A" / "01-GM8E-MetroidPrime A.gci").is_file()
    assert (saves_root / "USA" / "Card A" / "01-GALE-smashbros.gci").is_file()


@respx.mock
def test_sync_redownloads_with_no_prior_when_card_a_has_other_roms_gcis(
    tmp_path: Path,
) -> None:
    """Same as the prior-record case but with NO prior SaveRecord —
    the user wiped state.json entirely. Server still has the bundle;
    the lost-local probe must still trigger a download.

    Mirrors the user's live-test scenario: deleted local GCI, tried
    state.json edits, nothing restored — because the base probe saw
    the populated Card A and reported "local fine"."""
    saves_root = tmp_path / "GC"
    install = _make_install(saves_root)
    roms_base = tmp_path / "roms"

    rom = _make_rom(rom_id=42, output_path="gc/Eternal Darkness (USA).rvz")  # no prior saves
    rp = _plant_rom_file(roms_base, "gc/Eternal Darkness (USA).rvz")
    # Card A populated with OTHER ROMs' GCIs.
    _plant_gci(saves_root / "USA" / "Card A", "01-GM8E-MetroidPrime A.gci")
    state = _make_state([rom])

    payload = _build_bundle_zip(
        {"01-GEDE-Eternal Darkness.gci": b"\x00" * 122944},
        wrapper="Eternal Darkness (USA)",
    )
    payload_hash = compute_content_hash_from_bytes(payload)

    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(
            200,
            json=[
                _server_save(
                    save_id=70,
                    rom_id=42,
                    slot="Eternal Darkness (USA)",
                    file_name="Eternal Darkness (USA).zip",
                    file_size=len(payload),
                    md5=payload_hash,
                )
            ],
        )
    )
    respx.post(f"{BASE_URL}/api/saves/70/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 70})
    )
    respx.get(f"{BASE_URL}/api/saves/70/content").mock(
        return_value=httpx.Response(200, content=payload)
    )

    headers = {str(rp): DiscHeader(game_code="GEDE", maker_code="01", region="NTSC-U")}
    backend, http = _make_backend(install, roms_base=roms_base, headers=headers)
    with http:
        result = backend.sync(state)

    assert result.downloaded == 1, (
        "no-prior + lost-local + populated Card A should still download to restore"
    )
    assert (saves_root / "USA" / "Card A" / "01-GEDE-Eternal Darkness.gci").is_file()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_content_hash_from_bytes(zip_bytes: bytes) -> str:
    """Mirror of `compute_content_hash` for in-memory zips. Used to
    pre-compute server.content_hash without the disk round-trip."""
    file_hashes = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith("/"):
                continue
            content = zf.read(name)
            file_hashes.append(f"{name}:{hashlib.md5(content).hexdigest()}")
    combined = "\n".join(file_hashes)
    return hashlib.md5(combined.encode()).hexdigest()


def test_helper_compute_content_hash_from_bytes_matches_compute_content_hash(
    tmp_path: Path,
) -> None:
    payload = _build_bundle_zip({"a.gci": b"first", "b.gci": b"second"})
    archive = tmp_path / "out.zip"
    archive.write_bytes(payload)
    assert compute_content_hash_from_bytes(payload) == compute_content_hash(archive)
