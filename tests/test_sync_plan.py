from pathlib import Path

from ferry.domain.destination import Destination
from ferry.domain.state import LibraryState
from ferry.domain.sync_plan import compute_plan


def romm_rom(
    rom_id: int,
    *,
    name: str = "Game",
    platform_slug: str = "gc",
    updated_at: str = "2026-04-25T12:00:00Z",
    fs_name: str = "Game.zip",
    fs_size_bytes: int = 1024,
    md5_hash: str | None = "11111111111111111111111111111111",
) -> dict:
    """Default `md5_hash` matches the `source_md5` baked into
    `conftest.make_rom`, so a state-rom paired with an API-rom by
    rom_id classifies as unchanged unless a test overrides one side."""
    return {
        "id": rom_id,
        "name": name,
        "platform_slug": platform_slug,
        "updated_at": updated_at,
        "fs_name": fs_name,
        "fs_size_bytes": fs_size_bytes,
        "md5_hash": md5_hash,
    }


# ---------------------------------------------------------------------------
# add / update / delete classification
# ---------------------------------------------------------------------------


def test_empty_state_yields_all_adds() -> None:
    plan = compute_plan(
        current_roms=[romm_rom(1, name="A"), romm_rom(2, name="B")],
        state=LibraryState(),
    )
    assert len(plan.to_add) == 2
    assert plan.to_update == []
    assert plan.to_delete == []
    assert plan.unchanged_count == 0


def test_unchanged_when_romm_md5_matches(make_rom) -> None:
    """Equal md5 ⇒ same content. The default fixtures pair on md5
    `11...11`; no override needed."""
    state = LibraryState(roms={1: make_rom(rom_id=1)})
    plan = compute_plan(current_roms=[romm_rom(1)], state=state)
    assert plan.unchanged_count == 1
    assert plan.to_add == []
    assert plan.to_update == []
    assert plan.to_delete == []


def test_unchanged_when_only_updated_at_drifted_but_md5_matches(make_rom) -> None:
    """Regression for the live-test bug observed on 2026-05-08.

    A `Scan library` on RomM bumps `rom.updated_at` for every scanned
    rom (RomM's scan path calls `update_rom(id, {path_cover_s, ...})`
    after each rom, triggering SQLAlchemy `onupdate`) without touching
    the underlying file. The deterministic md5 compare correctly
    classifies this as unchanged regardless of `updated_at` drift.
    """
    state = LibraryState(
        roms={1: make_rom(rom_id=1, source_updated_at="2026-04-28T12:14:09+00:00")}
    )
    plan = compute_plan(
        current_roms=[romm_rom(1, updated_at="2026-05-09T04:45:05+00:00")],
        state=state,
    )
    assert plan.unchanged_count == 1
    assert plan.to_update == []


def test_update_when_romm_md5_differs(make_rom) -> None:
    """Different md5 ⇒ real file change ⇒ to_update."""
    state = LibraryState(roms={1: make_rom(rom_id=1)})  # source_md5 default 11..
    plan = compute_plan(
        current_roms=[romm_rom(1, md5_hash="2" * 32)],
        state=state,
    )
    assert plan.to_add == []
    assert len(plan.to_update) == 1
    assert plan.to_update[0].rom_id == 1
    assert "md5 changed" in plan.to_update[0].reason


def test_unchanged_when_md5_missing_but_size_matches(make_rom) -> None:
    """Tier-2 fallback: state lacks md5 (legacy) and/or server omits
    md5_hash (RomM hashing disabled), but `fs_size_bytes` matches
    `state.source_size` → size fallback says unchanged."""
    state = LibraryState(roms={1: make_rom(rom_id=1, source_md5=None, source_size=1024)})
    plan = compute_plan(
        current_roms=[romm_rom(1, fs_size_bytes=1024)],  # md5 default works too
        state=state,
    )
    assert plan.unchanged_count == 1
    assert plan.to_update == []


def test_unchanged_when_server_omits_md5_but_size_matches(make_rom) -> None:
    """Same fallback from the other side: state has md5, RomM has
    hashing disabled (md5_hash=None) — size still matches, unchanged."""
    state = LibraryState(roms={1: make_rom(rom_id=1, source_size=1024)})
    plan = compute_plan(
        current_roms=[romm_rom(1, md5_hash=None, fs_size_bytes=1024)],
        state=state,
    )
    assert plan.unchanged_count == 1
    assert plan.to_update == []


def test_update_via_size_fallback_when_md5_missing(make_rom) -> None:
    """md5 unavailable on either side AND size differs → real change."""
    state = LibraryState(roms={1: make_rom(rom_id=1, source_md5=None, source_size=1024)})
    plan = compute_plan(
        current_roms=[romm_rom(1, fs_size_bytes=2048)],
        state=state,
    )
    assert len(plan.to_update) == 1
    assert "fs_size_bytes changed" in plan.to_update[0].reason
    assert "md5 unavailable" in plan.to_update[0].reason


def test_update_when_no_comparable_signal(make_rom) -> None:
    """Tier-3 fallback: no md5 on either side AND no size info → conservative re-sync."""
    state = LibraryState(roms={1: make_rom(rom_id=1, source_md5=None, source_size=0)})
    rom = romm_rom(1, md5_hash=None)
    rom.pop("fs_size_bytes", None)  # both sides missing size
    plan = compute_plan(current_roms=[rom], state=state)
    assert len(plan.to_update) == 1
    assert "no comparable signal" in plan.to_update[0].reason


def test_planner_always_populates_to_delete(make_rom) -> None:
    """`to_delete` is informational; the executor decides whether to act on it
    based on `[sync].delete_on_remove`. The planner has no opinion."""
    state = LibraryState(roms={42: make_rom(rom_id=42, name="Old Game")})
    plan = compute_plan(current_roms=[], state=state)
    assert len(plan.to_delete) == 1
    assert plan.to_delete[0].rom_id == 42
    assert plan.to_delete[0].name == "Old Game"
    assert "no longer in collection" in plan.to_delete[0].reason


def test_mixed_plan(make_rom) -> None:
    state = LibraryState(
        roms={
            1: make_rom(rom_id=1),  # unchanged (md5 matches default)
            2: make_rom(rom_id=2, source_md5="aaaa" * 8),  # to update (md5 differs)
            3: make_rom(rom_id=3, name="Removed"),  # to delete
        }
    )
    plan = compute_plan(
        current_roms=[
            romm_rom(1),  # md5 matches state's default
            romm_rom(2),  # md5 default differs from state's "aaaa..."
            romm_rom(99, name="Brand New"),
        ],
        state=state,
    )
    assert len(plan.to_add) == 1
    assert plan.to_add[0].rom_id == 99
    assert len(plan.to_update) == 1
    assert plan.to_update[0].rom_id == 2
    assert len(plan.to_delete) == 1
    assert plan.to_delete[0].rom_id == 3
    assert plan.unchanged_count == 1


# ---------------------------------------------------------------------------
# Stability of output
# ---------------------------------------------------------------------------


def test_actions_are_sorted_by_name() -> None:
    plan = compute_plan(
        current_roms=[
            romm_rom(1, name="Charlie"),
            romm_rom(2, name="Alpha"),
            romm_rom(3, name="Bravo"),
        ],
        state=LibraryState(),
    )
    assert [a.name for a in plan.to_add] == ["Alpha", "Bravo", "Charlie"]


def test_helpers_compute_correct_summaries(make_rom) -> None:
    plan = compute_plan(current_roms=[romm_rom(1)], state=LibraryState())
    assert plan.is_empty is False
    assert plan.total_changes == 1


def test_empty_plan_is_empty() -> None:
    plan = compute_plan(current_roms=[], state=LibraryState())
    assert plan.is_empty is True
    assert plan.total_changes == 0


# ---------------------------------------------------------------------------
# Robustness against malformed RomM responses
# ---------------------------------------------------------------------------


def test_skips_rows_without_integer_id() -> None:
    plan = compute_plan(
        current_roms=[
            {"id": "not-an-int", "name": "Junk"},
            {"name": "Junk2"},  # missing id entirely
            romm_rom(1, name="Real"),
        ],
        state=LibraryState(),
    )
    assert len(plan.to_add) == 1
    assert plan.to_add[0].rom_id == 1


def test_falls_back_through_name_fields() -> None:
    plan = compute_plan(
        current_roms=[
            # Has fs_name_no_ext but no name → uses fs_name_no_ext.
            {"id": 1, "fs_name_no_ext": "FromExt", "fs_name": "FromExt.zip"},
            # No name fields at all → "?".
            {"id": 2},
        ],
        state=LibraryState(),
    )
    names = sorted(a.name for a in plan.to_add)
    assert names == ["?", "FromExt"]


# ---------------------------------------------------------------------------
# State-vs-disk drift: primary output missing → re-sync
# ---------------------------------------------------------------------------


def _destination(roms_base: Path) -> Destination:
    return Destination(roms_base=roms_base, bios_base=None, preset="esde-native")


def test_unchanged_promoted_to_update_when_primary_missing(tmp_path: Path, make_rom) -> None:
    """User deleted files manually → next sync should re-fetch."""
    state = LibraryState(
        roms={1: make_rom(rom_id=1, source_updated_at="2026-04-25T12:00:00Z")},
    )
    # Default make_rom uses outputs at "gc/Pikmin.iso" — never created on disk.
    plan = compute_plan(
        current_roms=[romm_rom(1, updated_at="2026-04-25T12:00:00Z")],
        state=state,
        destination=_destination(tmp_path),
    )
    assert plan.unchanged_count == 0
    assert len(plan.to_update) == 1
    assert "missing on disk" in plan.to_update[0].reason


def test_unchanged_stays_unchanged_when_primary_present(tmp_path: Path, make_rom) -> None:
    state = LibraryState(
        roms={1: make_rom(rom_id=1, source_updated_at="2026-04-25T12:00:00Z")},
    )
    # Create the primary output on disk.
    primary = tmp_path / "gc" / "Pikmin.iso"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"data")

    plan = compute_plan(
        current_roms=[romm_rom(1, updated_at="2026-04-25T12:00:00Z")],
        state=state,
        destination=_destination(tmp_path),
    )
    assert plan.unchanged_count == 1
    assert plan.to_update == []


def test_no_destination_skips_disk_check(make_rom) -> None:
    """Without a destination (e.g., unit tests), the planner trusts state."""
    state = LibraryState(
        roms={1: make_rom(rom_id=1, source_updated_at="2026-04-25T12:00:00Z")},
    )
    plan = compute_plan(
        current_roms=[romm_rom(1, updated_at="2026-04-25T12:00:00Z")],
        state=state,
        # destination=None (default)
    )
    assert plan.unchanged_count == 1
    assert plan.to_update == []
