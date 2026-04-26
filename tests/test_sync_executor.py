"""End-to-end tests for the sync executor — full stack from RomM bytes to disk."""

import hashlib
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import respx

from ferry.adapters.romm import RommApi, RommHttpAdapter
from ferry.adapters.sidecar import sidecar_path_for
from ferry.adapters.state_store import load_state, save_state
from ferry.config import RommConfig
from ferry.config.schema import Config, TransformsConfig
from ferry.domain.destination import Destination
from ferry.domain.state import LibraryState
from ferry.domain.sync_plan import AddAction, DeleteAction, SyncPlan, UpdateAction
from ferry.services.sync_executor import (
    default_scratch_root,
    execute_plan,
)

BASE_URL = "https://romm.example.tld"


def make_zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def make_config(tmp_path: Path, *, transforms: dict[str, tuple[str, ...]] | None = None) -> Config:
    return Config(
        romm=RommConfig(url=BASE_URL, api_key="rmm_testkey_abcdef", allow_insecure_ssl=False),
        destination=Destination(
            roms_base=tmp_path / "ROMs",
            bios_base=None,
            preset="esde-native",
        ),
        transforms=TransformsConfig(pipelines=transforms or {}),
    )


def romm_rom(
    rom_id: int,
    *,
    name: str = "Test Game",
    platform_slug: str = "gc",
    fs_name: str = "Test Game.zip",
    updated_at: str = "2026-04-25T12:00:00Z",
    md5_hash: str | None = None,
) -> dict:
    return {
        "id": rom_id,
        "name": name,
        "platform_slug": platform_slug,
        "fs_name": fs_name,
        "updated_at": updated_at,
        "md5_hash": md5_hash,
    }


def mock_download(rom_id: int, fs_name: str, payload: bytes) -> respx.MockRouter:
    import urllib.parse

    encoded = urllib.parse.quote(fs_name, safe="")
    return respx.get(f"{BASE_URL}/api/roms/{rom_id}/content/{encoded}").mock(
        return_value=httpx.Response(200, content=payload)
    )


# ---------------------------------------------------------------------------
# AddAction — happy path
# ---------------------------------------------------------------------------


@respx.mock
def test_add_action_with_unzip_extracts_and_records_state(tmp_path: Path) -> None:
    payload = make_zip_bytes({"Game.iso": b"iso-bytes"})
    rom = romm_rom(101, name="Pikmin", fs_name="Pikmin.zip")
    mock_download(101, "Pikmin.zip", payload)

    config = make_config(tmp_path, transforms={"gc": ("unzip",)})
    state_path = tmp_path / "state.json"
    state = LibraryState()

    plan = SyncPlan(
        to_add=[
            AddAction(
                rom_id=101,
                name="Pikmin",
                platform_slug="gc",
                rom_data=rom,
                reason="new",
            )
        ],
        to_update=[],
        to_delete=[],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        result = execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
        )

    assert len(result.succeeded) == 1
    assert result.failed == []
    assert result.deleted == []

    # File extracted to the right place.
    assert (tmp_path / "ROMs" / "gc" / "Game.iso").read_bytes() == b"iso-bytes"
    # Source zip is gone (cleaned up after success).
    assert not (tmp_path / "scratch" / "101").exists()

    # State persisted.
    persisted = load_state(state_path)
    assert 101 in persisted.roms
    saved = persisted.roms[101]
    assert saved.platform_slug == "gc"
    assert saved.name == "Pikmin"
    assert saved.source_filename == "Pikmin.zip"
    assert saved.source_md5 == md5_of(payload)
    assert saved.source_size == len(payload)
    assert saved.source_updated_at == "2026-04-25T12:00:00Z"
    assert saved.transforms == ("unzip",)
    assert len(saved.outputs) == 1
    assert saved.outputs[0].path == "gc/Game.iso"
    assert saved.outputs[0].md5 == md5_of(b"iso-bytes")
    assert saved.outputs[0].size == 9
    assert saved.primary_output_index == 0

    # Sidecar written next to the primary.
    sidecar = sidecar_path_for(tmp_path / "ROMs" / "gc" / "Game.iso")
    assert sidecar.exists()


@respx.mock
def test_add_action_with_no_transforms_passes_source_through(tmp_path: Path) -> None:
    """A platform without [transforms] entries lands the file as-is."""
    payload = b"raw bytes, no archive"
    rom = romm_rom(202, name="DemoRom", fs_name="DemoRom.iso", platform_slug="gc")
    mock_download(202, "DemoRom.iso", payload)

    config = make_config(tmp_path, transforms={})  # no pipelines
    state = LibraryState()
    state_path = tmp_path / "state.json"

    plan = SyncPlan(
        to_add=[
            AddAction(
                rom_id=202,
                name="DemoRom",
                platform_slug="gc",
                rom_data=rom,
                reason="new",
            )
        ],
        to_update=[],
        to_delete=[],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        result = execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
        )
    assert len(result.succeeded) == 1
    landed = tmp_path / "ROMs" / "gc" / "DemoRom.iso"
    assert landed.read_bytes() == payload


# ---------------------------------------------------------------------------
# Multi-output zip → primary picker
# ---------------------------------------------------------------------------


@respx.mock
def test_multi_output_zip_picks_cue_as_primary(tmp_path: Path) -> None:
    """When multiple files come out of unzip, the primary is the .cue."""
    payload = make_zip_bytes(
        {"CD1.cue": b"c1", "CD1.bin": b"b1", "CD2.cue": b"c2", "CD2.bin": b"b2"}
    )
    rom = romm_rom(303, name="MultiDisc", fs_name="MultiDisc.zip", platform_slug="psx")
    mock_download(303, "MultiDisc.zip", payload)

    config = make_config(tmp_path, transforms={"psx": ("unzip",)})
    state = LibraryState()
    state_path = tmp_path / "state.json"

    plan = SyncPlan(
        to_add=[
            AddAction(
                rom_id=303,
                name="MultiDisc",
                platform_slug="psx",
                rom_data=rom,
                reason="new",
            )
        ],
        to_update=[],
        to_delete=[],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
        )

    persisted = load_state(state_path)
    saved = persisted.roms[303]
    primary = saved.outputs[saved.primary_output_index]
    assert primary.path.endswith(".cue")
    # Sidecar is at the primary, not at the bins.
    primary_abs = tmp_path / "ROMs" / "psx" / Path(primary.path).name
    assert sidecar_path_for(primary_abs).exists()


# ---------------------------------------------------------------------------
# UpdateAction — orphan cleanup
# ---------------------------------------------------------------------------


@respx.mock
def test_update_action_cleans_up_old_outputs_with_changed_filename(
    tmp_path: Path, make_rom
) -> None:
    """If fs_name changes between syncs, old extracted files become orphans."""
    # Set up old state: rom 404 had Game v1.iso.
    config = make_config(tmp_path, transforms={"gc": ("unzip",)})
    gc_dir = tmp_path / "ROMs" / "gc"
    gc_dir.mkdir(parents=True)
    old_iso = gc_dir / "Game v1.iso"
    old_iso.write_bytes(b"old-bytes")
    old_sidecar = sidecar_path_for(old_iso)
    old_sidecar.write_text("{}")  # placeholder content

    previous = make_rom(
        rom_id=404,
        name="Old Game",
        outputs=(),  # we'll rebuild below
    )
    # Rebuild previous with the right output paths.
    from ferry.domain.state import RomState, TransformedOutput

    previous = RomState(
        rom_id=404,
        platform_slug="gc",
        name="Old Game",
        source_filename="Game v1.zip",
        source_md5="oldmd5",
        source_size=10,
        source_updated_at="2026-04-24T00:00:00Z",
        transforms=("unzip",),
        outputs=(TransformedOutput(path="gc/Game v1.iso", md5="oldhash", size=9),),
        primary_output_index=0,
        synced_at="2026-04-24T00:01:00Z",
    )

    # New sync: filename has changed in RomM.
    new_payload = make_zip_bytes({"Game v2.iso": b"new-bytes"})
    rom = romm_rom(404, name="Old Game", fs_name="Game v2.zip", updated_at="2026-04-26T00:00:00Z")
    mock_download(404, "Game v2.zip", new_payload)

    state = LibraryState(roms={404: previous})
    state_path = tmp_path / "state.json"
    save_state(state, state_path)  # bootstrap

    plan = SyncPlan(
        to_add=[],
        to_update=[
            UpdateAction(
                rom_id=404,
                name="Old Game",
                platform_slug="gc",
                rom_data=rom,
                previous=previous,
                reason="updated_at changed",
            )
        ],
        to_delete=[],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        result = execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
        )

    assert len(result.succeeded) == 1
    # New file lands.
    assert (gc_dir / "Game v2.iso").read_bytes() == b"new-bytes"
    # Old file removed.
    assert not old_iso.exists()
    # Old sidecar removed.
    assert not old_sidecar.exists()
    # New sidecar written.
    assert sidecar_path_for(gc_dir / "Game v2.iso").exists()


# ---------------------------------------------------------------------------
# State persistence per-ROM
# ---------------------------------------------------------------------------


@respx.mock
def test_state_persisted_after_each_rom(tmp_path: Path) -> None:
    """If the second ROM blows up, the first ROM's state should already be on disk."""
    payload_a = make_zip_bytes({"A.iso": b"a"})
    mock_download(1, "A.zip", payload_a)
    # Force a 500 on rom 2 — won't retry past 3 attempts but proves isolation.
    import urllib.parse

    respx.get(f"{BASE_URL}/api/roms/2/content/{urllib.parse.quote('B.zip', safe='')}").mock(
        return_value=httpx.Response(500)
    )

    config = make_config(tmp_path, transforms={"gc": ("unzip",)})
    state = LibraryState()
    state_path = tmp_path / "state.json"
    plan = SyncPlan(
        to_add=[
            AddAction(
                rom_id=1,
                name="A",
                platform_slug="gc",
                rom_data=romm_rom(1, name="A", fs_name="A.zip"),
                reason="new",
            ),
            AddAction(
                rom_id=2,
                name="B",
                platform_slug="gc",
                rom_data=romm_rom(2, name="B", fs_name="B.zip"),
                reason="new",
            ),
        ],
        to_update=[],
        to_delete=[],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        result = execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
        )

    assert len(result.succeeded) == 1
    assert len(result.failed) == 1
    # State has rom 1, not rom 2.
    persisted = load_state(state_path)
    assert 1 in persisted.roms
    assert 2 not in persisted.roms


# ---------------------------------------------------------------------------
# Failure isolation + scratch retention
# ---------------------------------------------------------------------------


@respx.mock
def test_failed_rom_leaves_scratch_for_debug(tmp_path: Path) -> None:
    import urllib.parse

    respx.get(f"{BASE_URL}/api/roms/9/content/{urllib.parse.quote('Bad.zip', safe='')}").mock(
        return_value=httpx.Response(500)
    )

    config = make_config(tmp_path, transforms={"gc": ("unzip",)})
    state = LibraryState()
    state_path = tmp_path / "state.json"
    plan = SyncPlan(
        to_add=[
            AddAction(
                rom_id=9,
                name="Bad",
                platform_slug="gc",
                rom_data=romm_rom(9, name="Bad", fs_name="Bad.zip"),
                reason="new",
            )
        ],
        to_update=[],
        to_delete=[],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        result = execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
        )

    assert result.failed[0].rom_id == 9
    # Scratch dir still exists (retained for debug).
    assert (tmp_path / "scratch" / "9").exists()


# ---------------------------------------------------------------------------
# DeleteAction execution — soft-delete to trash
# ---------------------------------------------------------------------------


def test_delete_action_moves_outputs_and_sidecar_to_trash(tmp_path: Path, make_rom) -> None:
    config = make_config(tmp_path)
    # Set up the on-disk state: primary file + sidecar.
    primary = tmp_path / "ROMs" / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"iso-bytes")
    rom = make_rom(rom_id=5, name="Pikmin")
    from ferry.adapters.sidecar import sidecar_path_for, write_sidecar

    write_sidecar(primary, rom)

    state = LibraryState(roms={5: rom})
    state_path = tmp_path / "state.json"
    plan = SyncPlan(
        to_add=[],
        to_update=[],
        to_delete=[
            DeleteAction(
                rom_id=5,
                name="Pikmin",
                platform_slug="gc",
                previous=rom,
                reason="no longer in collection",
            )
        ],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        result = execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
            delete_on_remove=True,
        )
    assert result.succeeded == []
    assert result.failed == []
    assert len(result.deleted) == 1
    assert result.deleted[0].rom_id == 5

    # Files are gone from roms_base.
    assert not primary.exists()
    assert not sidecar_path_for(primary).exists()

    # Files are in the trash, with the gc/ subdir preserved.
    trash_dir = result.deleted[0].trash_dir
    assert (trash_dir / "gc" / "Pikmin.iso").read_bytes() == b"iso-bytes"
    assert (trash_dir / "gc" / "Pikmin.iso.ferry.json").exists()

    # State no longer mentions the rom.
    from ferry.adapters.state_store import load_state

    persisted = load_state(state_path)
    assert 5 not in persisted.roms


def test_delete_on_remove_false_keeps_files_on_disk(tmp_path: Path, make_rom) -> None:
    """Even with `to_delete` populated, executor leaves files alone when flag is off."""
    config = make_config(tmp_path)
    primary = tmp_path / "ROMs" / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"iso-bytes")
    rom = make_rom(rom_id=5)
    state = LibraryState(roms={5: rom})
    state_path = tmp_path / "state.json"
    plan = SyncPlan(
        to_add=[],
        to_update=[],
        to_delete=[
            DeleteAction(
                rom_id=5,
                name="Pikmin",
                platform_slug="gc",
                previous=rom,
                reason="no longer in collection",
            )
        ],
        unchanged_count=0,
    )

    with RommHttpAdapter(config.romm) as http:
        api = RommApi(http)
        result = execute_plan(
            plan=plan,
            config=config,
            api=api,
            state=state,
            state_path=state_path,
            scratch_root=tmp_path / "scratch",
            trash_root=tmp_path / "trash",
            delete_on_remove=False,  # explicit
        )
    assert result.deleted == []
    assert result.succeeded == []
    assert result.failed == []
    # File still on disk; state still has the rom.
    assert primary.read_bytes() == b"iso-bytes"
    assert 5 in state.roms


# ---------------------------------------------------------------------------
# Default scratch root
# ---------------------------------------------------------------------------


def test_default_scratch_root_uses_xdg_cache_home(tmp_path: Path) -> None:
    p = default_scratch_root(env={"XDG_CACHE_HOME": str(tmp_path)})
    assert p == tmp_path / "ferry" / "scratch"


def test_default_scratch_root_falls_back_to_home_cache() -> None:
    p = default_scratch_root(env={})
    assert p == Path.home() / ".cache" / "ferry" / "scratch"
