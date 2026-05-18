"""End-to-end tests for the BIOS executor — RomM firmware bytes to disk."""

import hashlib
from pathlib import Path

import httpx
import respx

from ferry.adapters.romm import RommApi, RommHttpAdapter
from ferry.adapters.state_store import load_state
from ferry.config import RommConfig
from ferry.domain.bios_plan import BiosAddAction, BiosDeleteAction, BiosPlan, BiosUpdateAction
from ferry.domain.destination import Destination
from ferry.domain.state import BiosRecord, LibraryState
from ferry.services.bios_executor import execute_bios_plan

BASE_URL = "https://romm.example.tld"
PAYLOAD = b"firmware-payload-bytes" * 64
PAYLOAD_MD5 = hashlib.md5(PAYLOAD).hexdigest()


def make_config() -> RommConfig:
    return RommConfig(url=BASE_URL, api_key="rmm_testkey_abcdef", allow_insecure_ssl=False)


def destination(tmp_path: Path) -> Destination:
    return Destination(
        roms_base=tmp_path / "roms",
        bios_base=tmp_path / "bios",
        preset="retrodeck-flatpak",
    )


def add(
    fid: int,
    file_name: str = "ps2.bin",
    *,
    platform: str = "ps2",
    target: str | None = None,
    unverified: bool = False,
) -> BiosAddAction:
    return BiosAddAction(
        firmware_id=fid,
        platform_slug=platform,
        file_name=file_name,
        target_path=target if target is not None else file_name,
        firmware_data={},
        unverified=unverified,
        reason="new in RomM",
    )


def update(
    fid: int,
    prev: BiosRecord,
    file_name: str = "ps2.bin",
    *,
    platform: str = "ps2",
    target: str | None = None,
) -> BiosUpdateAction:
    return BiosUpdateAction(
        firmware_id=fid,
        platform_slug=platform,
        file_name=file_name,
        target_path=target if target is not None else file_name,
        firmware_data={},
        previous=prev,
        unverified=False,
        reason="md5 changed",
    )


def rec(
    fid: int, *, platform: str = "ps2", file_name: str = "ps2.bin", path: str | None = None
) -> BiosRecord:
    return BiosRecord(
        firmware_id=fid,
        platform_slug=platform,
        file_name=file_name,
        path=path if path is not None else file_name,
        md5="0" * 32,
        size=10,
    )


def plan(*, to_add=(), to_update=(), to_delete=()) -> BiosPlan:
    return BiosPlan(
        to_add=list(to_add),
        to_update=list(to_update),
        to_delete=list(to_delete),
        unchanged_count=0,
    )


def mock_firmware(firmware_id: int, file_name: str, content: bytes = PAYLOAD) -> None:
    respx.get(f"{BASE_URL}/api/firmware/{firmware_id}/content/{file_name}").mock(
        return_value=httpx.Response(200, content=content)
    )


def run(
    plan_obj: BiosPlan,
    tmp_path: Path,
    *,
    state: LibraryState | None = None,
    delete_on_remove: bool = False,
):
    state = state if state is not None else LibraryState()
    state_path = tmp_path / "state.json"
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        result = execute_bios_plan(
            plan=plan_obj,
            api=api,
            state=state,
            state_path=state_path,
            destination=destination(tmp_path),
            trash_root=tmp_path / "trash",
            delete_on_remove=delete_on_remove,
        )
    return result, state, state_path


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@respx.mock
def test_add_downloads_and_places_flat(tmp_path: Path) -> None:
    mock_firmware(7, "ps2.bin")
    result, state, state_path = run(plan(to_add=[add(7)]), tmp_path)

    landed = tmp_path / "bios" / "ps2.bin"
    assert landed.read_bytes() == PAYLOAD
    assert len(result.succeeded) == 1
    # state recorded and persisted
    assert state.bios[7].path == "ps2.bin"
    assert state.bios[7].md5 == PAYLOAD_MD5
    assert load_state(state_path).bios[7].md5 == PAYLOAD_MD5


@respx.mock
def test_add_places_into_subfolder(tmp_path: Path) -> None:
    mock_firmware(3, "dc_boot.bin")
    result, state, _ = run(
        plan(to_add=[add(3, "dc_boot.bin", platform="dc", target="dc/dc_boot.bin")]),
        tmp_path,
    )
    assert (tmp_path / "bios" / "dc" / "dc_boot.bin").read_bytes() == PAYLOAD
    assert state.bios[3].path == "dc/dc_boot.bin"


@respx.mock
def test_unverified_firmware_still_syncs(tmp_path: Path) -> None:
    mock_firmware(7, "ps2.bin")
    result, state, _ = run(plan(to_add=[add(7, unverified=True)]), tmp_path)
    assert len(result.succeeded) == 1
    assert (tmp_path / "bios" / "ps2.bin").exists()


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@respx.mock
def test_update_redownloads_in_place(tmp_path: Path) -> None:
    bios = tmp_path / "bios"
    bios.mkdir()
    (bios / "ps2.bin").write_bytes(b"old")
    mock_firmware(7, "ps2.bin")

    _, state, _ = run(
        plan(to_update=[update(7, rec(7))]),
        tmp_path,
        state=LibraryState(bios={7: rec(7)}),
    )
    assert (bios / "ps2.bin").read_bytes() == PAYLOAD
    assert state.bios[7].md5 == PAYLOAD_MD5


@respx.mock
def test_placement_change_removes_stale_file(tmp_path: Path) -> None:
    """A flat file moved into a subfolder leaves no stale copy behind."""
    bios = tmp_path / "bios"
    bios.mkdir()
    (bios / "dc_boot.bin").write_bytes(b"old-flat")
    mock_firmware(3, "dc_boot.bin")

    prev = rec(3, platform="dc", file_name="dc_boot.bin", path="dc_boot.bin")
    _, state, _ = run(
        plan(to_update=[update(3, prev, "dc_boot.bin", platform="dc", target="dc/dc_boot.bin")]),
        tmp_path,
        state=LibraryState(bios={3: prev}),
    )
    assert (bios / "dc" / "dc_boot.bin").read_bytes() == PAYLOAD
    assert not (bios / "dc_boot.bin").exists()  # stale flat copy gone
    assert state.bios[3].path == "dc/dc_boot.bin"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_trashes_when_opted_in(tmp_path: Path) -> None:
    bios = tmp_path / "bios"
    bios.mkdir()
    (bios / "ps2.bin").write_bytes(b"stale")
    prev = rec(7)
    deletion = BiosDeleteAction(
        firmware_id=7,
        platform_slug="ps2",
        file_name="ps2.bin",
        previous=prev,
        reason="no longer in RomM",
    )

    result, state, _ = run(
        plan(to_delete=[deletion]),
        tmp_path,
        state=LibraryState(bios={7: prev}),
        delete_on_remove=True,
    )
    assert not (bios / "ps2.bin").exists()
    assert 7 not in state.bios
    assert len(result.deleted) == 1


@respx.mock
def test_delete_skipped_when_not_opted_in(tmp_path: Path) -> None:
    bios = tmp_path / "bios"
    bios.mkdir()
    (bios / "ps2.bin").write_bytes(b"stale")
    prev = rec(7)
    deletion = BiosDeleteAction(
        firmware_id=7,
        platform_slug="ps2",
        file_name="ps2.bin",
        previous=prev,
        reason="no longer in RomM",
    )

    result, state, _ = run(
        plan(to_delete=[deletion]),
        tmp_path,
        state=LibraryState(bios={7: prev}),
        delete_on_remove=False,
    )
    assert (bios / "ps2.bin").exists()  # untouched
    assert 7 in state.bios
    assert result.deleted == []


# ---------------------------------------------------------------------------
# failure isolation
# ---------------------------------------------------------------------------


@respx.mock
def test_one_failure_does_not_abort_the_rest(tmp_path: Path) -> None:
    mock_firmware(7, "ps2.bin")
    respx.get(f"{BASE_URL}/api/firmware/8/content/missing.bin").mock(
        return_value=httpx.Response(404)
    )
    result, state, _ = run(
        plan(to_add=[add(7), add(8, "missing.bin")]),
        tmp_path,
    )
    assert len(result.succeeded) == 1
    assert len(result.failed) == 1
    assert result.failed[0].firmware_id == 8
    assert 7 in state.bios and 8 not in state.bios
