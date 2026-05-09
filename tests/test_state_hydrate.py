"""Tests for `services.state_hydrate.hydrate_romm_md5`."""

from __future__ import annotations

import zipfile
from pathlib import Path

from ferry.domain.state import LibraryState, RomState, TransformedOutput
from ferry.services.state_hydrate import hydrate_romm_md5


def _make_rom(
    rom_id: int,
    *,
    output_path: str,
    output_md5: str = "0" * 32,
    source_romm_md5: str | None = None,
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug="gc",
        name=f"Rom-{rom_id}",
        source_filename=Path(output_path).name,
        source_md5="0" * 32,
        source_size=1024,
        source_updated_at="2026-04-25T12:00:00Z",
        source_romm_md5=source_romm_md5,
        transforms=(),
        outputs=(TransformedOutput(path=output_path, md5=output_md5, size=1024),),
        primary_output_index=0,
        synced_at="2026-04-25T12:01:00Z",
    )


def _plant(roms_base: Path, rel_path: str, content: bytes) -> Path:
    target = roms_base / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _plant_zip(roms_base: Path, rel_path: str, members: dict[str, bytes]) -> Path:
    target = roms_base / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return target


def test_hydrate_no_op_when_all_entries_have_romm_md5(tmp_path: Path) -> None:
    state = LibraryState(
        roms={
            1: _make_rom(1, output_path="gc/A.iso", source_romm_md5="a" * 32),
            2: _make_rom(2, output_path="gc/B.iso", source_romm_md5="b" * 32),
        }
    )
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 0
    assert new_state is state  # identity preserved on no-op


def test_hydrate_populates_missing_romm_md5_for_non_archive(tmp_path: Path) -> None:
    """Single-file ROM (not an archive): md5 of the file's bytes."""
    _plant(tmp_path, "gc/Game.iso", b"raw rom bytes")
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Game.iso", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    import hashlib

    assert new_state.roms[1].source_romm_md5 == hashlib.md5(b"raw rom bytes").hexdigest()


def test_hydrate_populates_missing_romm_md5_for_zip_archive(tmp_path: Path) -> None:
    """Zip ROM: md5 of the largest inner file's bytes (RomM's algorithm)."""
    _plant_zip(tmp_path, "gc/Game.zip", {"Game.iso": b"largest inner file content"})
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Game.zip", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    import hashlib

    assert (
        new_state.roms[1].source_romm_md5 == hashlib.md5(b"largest inner file content").hexdigest()
    )


def test_hydrate_skips_entries_with_missing_primary_output(tmp_path: Path) -> None:
    """File deleted from disk → entry stays unhydrated. The
    `_primary_missing` check in compute_plan handles re-download."""
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Missing.iso", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 0
    assert new_state is state
    assert new_state.roms[1].source_romm_md5 is None  # unchanged


def test_hydrate_preserves_existing_md5_when_mixing_with_new(tmp_path: Path) -> None:
    """Mixed state: some entries already hydrated, some not. Only the
    None-valued ones get backfilled; populated ones aren't re-hashed."""
    _plant(tmp_path, "gc/A.iso", b"a-bytes")
    _plant(tmp_path, "gc/B.iso", b"b-bytes")
    state = LibraryState(
        roms={
            1: _make_rom(1, output_path="gc/A.iso", source_romm_md5="preserved" * 4),
            2: _make_rom(2, output_path="gc/B.iso", source_romm_md5=None),
        }
    )
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    assert new_state.roms[1].source_romm_md5 == "preserved" * 4
    import hashlib

    assert new_state.roms[2].source_romm_md5 == hashlib.md5(b"b-bytes").hexdigest()


def test_hydrate_treats_empty_string_as_missing(tmp_path: Path) -> None:
    """Empty string is normalized to None at decode time; defensive
    check at hydration in case some other path inserts `""`."""
    _plant(tmp_path, "gc/A.iso", b"a-bytes")
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/A.iso", source_romm_md5="")})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    assert new_state.roms[1].source_romm_md5  # populated


def test_hydrate_handles_corrupt_archive_gracefully(tmp_path: Path) -> None:
    """A `.zip` that isn't a valid zip file → `hash_orphan_file` falls
    back to direct md5 (RomM's behavior). Hydration uses whatever it
    returns — no exception escapes."""
    _plant(tmp_path, "gc/Bogus.zip", b"not a real zip")
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/Bogus.zip", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)
    assert hydrated == 1
    # Whatever was computed is fine; the property under test is "no crash."
    assert new_state.roms[1].source_romm_md5 is not None


def test_hydrate_does_not_mutate_input_state(tmp_path: Path) -> None:
    """Returns a new LibraryState; original is left intact."""
    _plant(tmp_path, "gc/A.iso", b"a-bytes")
    original = LibraryState(roms={1: _make_rom(1, output_path="gc/A.iso", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(original, tmp_path)
    assert hydrated == 1
    assert original.roms[1].source_romm_md5 is None  # untouched
    assert new_state.roms[1].source_romm_md5 is not None


# ---------------------------------------------------------------------------
# Resumability — checkpoint callback
# ---------------------------------------------------------------------------


def test_checkpoint_fires_every_n_hydrated_entries(tmp_path: Path) -> None:
    """With `checkpoint_every=2`, a 5-entry hydration fires at counts
    2 and 4 — every multiple of N up to but not including the final
    flush (which the caller does explicitly after the function returns)."""
    for i in range(5):
        _plant(tmp_path, f"gc/{i}.iso", f"content-{i}".encode())
    state = LibraryState(
        roms={i: _make_rom(i, output_path=f"gc/{i}.iso", source_romm_md5=None) for i in range(5)}
    )
    checkpoints: list[int] = []
    hydrate_romm_md5(
        state,
        tmp_path,
        on_checkpoint=lambda partial, count: checkpoints.append(count),
        checkpoint_every=2,
    )
    assert checkpoints == [2, 4]


def test_checkpoint_partial_state_carries_hydrated_entries_so_far(tmp_path: Path) -> None:
    """The state passed to the callback has all so-far-hydrated entries
    populated — so a caller persisting it gets a valid resume point."""
    for i in range(3):
        _plant(tmp_path, f"gc/{i}.iso", f"c-{i}".encode())
    state = LibraryState(
        roms={i: _make_rom(i, output_path=f"gc/{i}.iso", source_romm_md5=None) for i in range(3)}
    )
    snapshots: list[dict[int, str | None]] = []

    def capture(partial: LibraryState, count: int) -> None:
        snapshots.append({rid: r.source_romm_md5 for rid, r in partial.roms.items()})

    hydrate_romm_md5(state, tmp_path, on_checkpoint=capture, checkpoint_every=1)
    # Three checkpoints fired (one per rom); each successive one has
    # one more entry populated.
    assert len(snapshots) == 3
    populated_counts = [sum(1 for v in s.values() if v) for s in snapshots]
    assert populated_counts == [1, 2, 3]


def test_resume_after_simulated_interrupt_completes_remaining_entries(tmp_path: Path) -> None:
    """Run hydration, simulate an interrupt by raising from the checkpoint
    callback after the first entry. The persisted partial state has
    one entry populated; a subsequent run picks up the remaining two
    without re-hashing the first.
    """
    for i in range(3):
        _plant(tmp_path, f"gc/{i}.iso", f"content-{i}".encode())
    state = LibraryState(
        roms={i: _make_rom(i, output_path=f"gc/{i}.iso", source_romm_md5=None) for i in range(3)}
    )

    persisted: list[LibraryState] = []

    class _Interrupt(Exception):
        pass

    def crash_after_first(partial: LibraryState, count: int) -> None:
        persisted.append(partial)
        raise _Interrupt("simulated interrupt mid-hydration")

    import contextlib

    with contextlib.suppress(_Interrupt):
        hydrate_romm_md5(state, tmp_path, on_checkpoint=crash_after_first, checkpoint_every=1)

    # First-run snapshot: exactly one entry populated.
    snapshot = persisted[-1]
    populated_first_run = {rid for rid, r in snapshot.roms.items() if r.source_romm_md5}
    assert len(populated_first_run) == 1

    # Second run: pass the persisted partial state back in. The already-
    # hydrated entry is left alone; the other two get hashed.
    second_state, second_hydrated = hydrate_romm_md5(snapshot, tmp_path)
    assert second_hydrated == 2  # the two we didn't get to last time
    assert all(r.source_romm_md5 for r in second_state.roms.values())
    # The original entry's hash hasn't been recomputed — preserved verbatim.
    preserved_id = next(iter(populated_first_run))
    preserved_hash = snapshot.roms[preserved_id].source_romm_md5
    assert second_state.roms[preserved_id].source_romm_md5 == preserved_hash


def test_no_checkpoint_callback_still_works(tmp_path: Path) -> None:
    """Backward-compat: calling without a checkpoint callback runs to
    completion and returns the fully-hydrated state in one shot."""
    _plant(tmp_path, "gc/A.iso", b"a")
    state = LibraryState(roms={1: _make_rom(1, output_path="gc/A.iso", source_romm_md5=None)})
    new_state, hydrated = hydrate_romm_md5(state, tmp_path)  # no on_checkpoint
    assert hydrated == 1
    assert new_state.roms[1].source_romm_md5 is not None


def test_skipped_entries_dont_count_toward_checkpoint(tmp_path: Path) -> None:
    """Entries skipped (missing on disk) shouldn't tick the checkpoint
    counter — otherwise a state with many missing files would
    thrash state.json with no actual progress to persist."""
    # 1 real file, 4 missing — checkpoint_every=2 should fire ZERO times
    # (only 1 entry actually hydrates; 1 < 2).
    _plant(tmp_path, "gc/0.iso", b"real")
    state = LibraryState(
        roms={i: _make_rom(i, output_path=f"gc/{i}.iso", source_romm_md5=None) for i in range(5)}
    )
    checkpoints: list[int] = []
    new_state, hydrated = hydrate_romm_md5(
        state,
        tmp_path,
        on_checkpoint=lambda partial, count: checkpoints.append(count),
        checkpoint_every=2,
    )
    assert hydrated == 1
    assert checkpoints == []  # not enough successes to trigger a checkpoint


# ---------------------------------------------------------------------------
# Per-rom progress callback
# ---------------------------------------------------------------------------


def test_progress_fires_for_every_hydrated_rom(tmp_path: Path) -> None:
    """`on_progress` fires once per successful hash, with the in-flight
    state, the running count, and the rom that just completed."""
    for i in range(3):
        _plant(tmp_path, f"gc/{i}.iso", f"c-{i}".encode())
    state = LibraryState(
        roms={i: _make_rom(i, output_path=f"gc/{i}.iso", source_romm_md5=None) for i in range(3)}
    )
    events: list[tuple[int, int]] = []  # (count, rom_id)

    def progress(partial: LibraryState, count: int, rom: RomState) -> None:
        events.append((count, rom.rom_id))
        # Each call sees a state where exactly `count` entries are populated.
        populated = sum(1 for r in partial.roms.values() if r.source_romm_md5)
        assert populated == count

    hydrate_romm_md5(state, tmp_path, on_progress=progress)
    assert [count for count, _ in events] == [1, 2, 3]
    assert sorted(rom_id for _, rom_id in events) == [0, 1, 2]


def test_progress_does_not_fire_for_skipped_entries(tmp_path: Path) -> None:
    """Entries that are skipped (missing on disk) don't appear in the
    progress stream — only successful hashes do."""
    _plant(tmp_path, "gc/0.iso", b"real")
    state = LibraryState(
        roms={i: _make_rom(i, output_path=f"gc/{i}.iso", source_romm_md5=None) for i in range(3)}
    )
    events: list[int] = []
    hydrate_romm_md5(
        state,
        tmp_path,
        on_progress=lambda partial, count, rom: events.append(rom.rom_id),
    )
    assert events == [0]  # only the rom whose file exists


def test_progress_and_checkpoint_can_run_together(tmp_path: Path) -> None:
    """Both callbacks compose without interference. Progress fires per
    rom; checkpoint fires every N — no conflict, no double-counting."""
    for i in range(5):
        _plant(tmp_path, f"gc/{i}.iso", f"c-{i}".encode())
    state = LibraryState(
        roms={i: _make_rom(i, output_path=f"gc/{i}.iso", source_romm_md5=None) for i in range(5)}
    )
    progress_events: list[int] = []
    checkpoint_events: list[int] = []
    hydrate_romm_md5(
        state,
        tmp_path,
        on_progress=lambda partial, count, rom: progress_events.append(count),
        on_checkpoint=lambda partial, count: checkpoint_events.append(count),
        checkpoint_every=2,
    )
    assert progress_events == [1, 2, 3, 4, 5]  # one per rom
    assert checkpoint_events == [2, 4]  # every other rom
