"""Tests for BIOS placement (`domain.bios_placement`) and the BIOS sync
planner (`domain.bios_plan`)."""

from typing import Any

from ferry.domain.bios_placement import placement_for
from ferry.domain.bios_plan import compute_bios_plan
from ferry.domain.state import BiosRecord, LibraryState


def fw(
    firmware_id: int,
    file_name: str = "ps2-0230a-20080220.bin",
    *,
    md5: str = "a" * 32,
    size: int = 4194304,
    is_verified: bool = True,
) -> dict[str, Any]:
    return {
        "id": firmware_id,
        "file_name": file_name,
        "md5_hash": md5,
        "file_size_bytes": size,
        "is_verified": is_verified,
    }


def rec(
    firmware_id: int,
    *,
    platform_slug: str = "ps2",
    file_name: str = "ps2-0230a-20080220.bin",
    path: str | None = None,
    md5: str = "a" * 32,
    size: int = 4194304,
) -> BiosRecord:
    return BiosRecord(
        firmware_id=firmware_id,
        platform_slug=platform_slug,
        file_name=file_name,
        path=path if path is not None else file_name,
        md5=md5,
        size=size,
    )


# ---------------------------------------------------------------------------
# placement_for
# ---------------------------------------------------------------------------


def test_placement_flat_by_default() -> None:
    assert placement_for("ps2", "ps2-0230a.bin") == "ps2-0230a.bin"
    assert placement_for("psx", "scph5501.bin") == "scph5501.bin"


def test_placement_dreamcast_uses_dc_subfolder() -> None:
    assert placement_for("dc", "dc_boot.bin") == "dc/dc_boot.bin"


def test_placement_wiiu_uses_cemu_subfolder() -> None:
    assert placement_for("wiiu", "keys.txt") == "cemu/keys.txt"


# ---------------------------------------------------------------------------
# compute_bios_plan — add / unchanged
# ---------------------------------------------------------------------------


def test_empty_inputs_yield_empty_plan() -> None:
    plan = compute_bios_plan(firmware_by_platform={}, state=LibraryState())
    assert plan.is_empty
    assert plan.total_changes == 0


def test_new_firmware_is_added_with_placement() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"dc": [fw(3, "dc_boot.bin")]},
        state=LibraryState(),
    )
    assert len(plan.to_add) == 1
    add = plan.to_add[0]
    assert add.firmware_id == 3
    assert add.platform_slug == "dc"
    assert add.target_path == "dc/dc_boot.bin"
    assert add.reason == "new in RomM"


def test_unchanged_firmware_counted_not_actioned() -> None:
    state = LibraryState(bios={7: rec(7)})
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7)]},
        state=state,
    )
    assert plan.is_empty
    assert plan.unchanged_count == 1


def test_unverified_firmware_flag_propagates() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7, is_verified=False)]},
        state=LibraryState(),
    )
    assert plan.to_add[0].unverified is True


def test_verified_firmware_is_not_flagged() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7, is_verified=True)]},
        state=LibraryState(),
    )
    assert plan.to_add[0].unverified is False


# ---------------------------------------------------------------------------
# compute_bios_plan — update
# ---------------------------------------------------------------------------


def test_md5_change_triggers_update() -> None:
    state = LibraryState(bios={7: rec(7, md5="a" * 32)})
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7, md5="b" * 32)]},
        state=state,
    )
    assert len(plan.to_update) == 1
    assert "md5 changed" in plan.to_update[0].reason


def test_size_fallback_when_md5_unavailable() -> None:
    """No server md5 → size compare decides."""
    state = LibraryState(bios={7: rec(7, md5="", size=100)})
    server = fw(7, md5="", size=200)
    plan = compute_bios_plan(firmware_by_platform={"ps2": [server]}, state=state)
    assert len(plan.to_update) == 1
    assert "file_size_bytes changed" in plan.to_update[0].reason


def test_same_size_no_md5_is_unchanged() -> None:
    state = LibraryState(bios={7: rec(7, md5="", size=100)})
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7, md5="", size=100)]},
        state=state,
    )
    assert plan.unchanged_count == 1


def test_placement_change_triggers_update() -> None:
    """Content unchanged, but the subfolder map moved the file."""
    state = LibraryState(
        bios={3: rec(3, platform_slug="dc", file_name="dc_boot.bin", path="dc_boot.bin")}
    )
    plan = compute_bios_plan(
        firmware_by_platform={"dc": [fw(3, "dc_boot.bin")]},
        state=state,
    )
    assert len(plan.to_update) == 1
    assert "placement changed" in plan.to_update[0].reason
    assert plan.to_update[0].target_path == "dc/dc_boot.bin"


def test_missing_on_disk_triggers_update(tmp_path) -> None:
    state = LibraryState(bios={7: rec(7)})
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7)]},
        state=state,
        bios_base=tmp_path,
    )
    assert len(plan.to_update) == 1
    assert "missing on disk" in plan.to_update[0].reason


def test_present_on_disk_is_unchanged(tmp_path) -> None:
    (tmp_path / "ps2-0230a-20080220.bin").write_bytes(b"x")
    state = LibraryState(bios={7: rec(7)})
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7)]},
        state=state,
        bios_base=tmp_path,
    )
    assert plan.unchanged_count == 1


# ---------------------------------------------------------------------------
# compute_bios_plan — delete
# ---------------------------------------------------------------------------


def test_firmware_dropped_from_romm_is_deleted() -> None:
    state = LibraryState(bios={7: rec(7)})
    plan = compute_bios_plan(firmware_by_platform={"ps2": []}, state=state)
    assert len(plan.to_delete) == 1
    assert plan.to_delete[0].firmware_id == 7
    assert "no longer in RomM" in plan.to_delete[0].reason


def test_platform_removed_from_scope_deletes_its_firmware() -> None:
    """A platform absent from the fetched listing → its state firmware deletes."""
    state = LibraryState(bios={7: rec(7, platform_slug="ps2")})
    # ps2 not in firmware_by_platform at all (user dropped it from [sync]).
    plan = compute_bios_plan(firmware_by_platform={"dc": []}, state=state)
    assert [d.firmware_id for d in plan.to_delete] == [7]


# ---------------------------------------------------------------------------
# compute_bios_plan — [bios.files] allowlist
# ---------------------------------------------------------------------------


def test_allowlist_excludes_unlisted_files() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7, "wanted.bin"), fw(8, "unwanted.bin")]},
        state=LibraryState(),
        allowlists={"ps2": ("wanted.bin",)},
    )
    assert [a.firmware_id for a in plan.to_add] == [7]


def test_allowlist_absent_platform_syncs_all() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7, "a.bin"), fw(8, "b.bin")]},
        state=LibraryState(),
        allowlists={"dc": ("dc_boot.bin",)},  # only dc constrained; ps2 unconstrained
    )
    assert {a.firmware_id for a in plan.to_add} == {7, 8}


def test_allowlist_excluding_a_synced_file_deletes_it() -> None:
    """A file previously synced but now excluded by [bios.files] → delete."""
    state = LibraryState(bios={8: rec(8, file_name="unwanted.bin", path="unwanted.bin")})
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7, "wanted.bin"), fw(8, "unwanted.bin")]},
        state=state,
        allowlists={"ps2": ("wanted.bin",)},
    )
    assert [a.firmware_id for a in plan.to_add] == [7]
    assert [d.firmware_id for d in plan.to_delete] == [8]


def test_empty_allowlist_syncs_nothing_for_platform() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(7), fw(8, "b.bin")]},
        state=LibraryState(),
        allowlists={"ps2": ()},
    )
    assert plan.to_add == []


# ---------------------------------------------------------------------------
# compute_bios_plan — robustness / ordering
# ---------------------------------------------------------------------------


def test_rows_without_id_or_filename_are_skipped() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [{"file_name": "x.bin"}, {"id": 9}, fw(7)]},
        state=LibraryState(),
    )
    assert [a.firmware_id for a in plan.to_add] == [7]


def test_actions_sorted_by_filename() -> None:
    plan = compute_bios_plan(
        firmware_by_platform={"ps2": [fw(1, "z.bin"), fw(2, "a.bin"), fw(3, "m.bin")]},
        state=LibraryState(),
    )
    assert [a.file_name for a in plan.to_add] == ["a.bin", "m.bin", "z.bin"]
