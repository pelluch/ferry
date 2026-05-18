"""Tests for the soft-delete trash primitive."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ferry.services.trash import (
    default_trash_root,
    purge_expired,
    trash_bios_files,
    trash_paths,
)


def _now() -> datetime:
    return datetime(2026, 4, 25, 18, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_default_trash_root_uses_xdg_state_home(tmp_path: Path) -> None:
    p = default_trash_root(env={"XDG_STATE_HOME": str(tmp_path)})
    assert p == tmp_path / "ferry" / "trash"


def test_default_trash_root_falls_back_to_local_state() -> None:
    p = default_trash_root(env={})
    assert p == Path.home() / ".local" / "state" / "ferry" / "trash"


# ---------------------------------------------------------------------------
# trash_paths — moves files into a timestamped per-rom subdir
# ---------------------------------------------------------------------------


def test_trash_paths_creates_timestamped_dir_and_moves_files(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    primary = roms_base / "gc" / "Game.iso"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"data")

    trash_root = tmp_path / "trash"
    target = trash_paths(
        [primary],
        rom_id=42,
        trash_root=trash_root,
        roms_base=roms_base,
        now=_now(),
    )
    assert target == trash_root / "20260425T183000Z__rom42"
    assert (target / "gc" / "Game.iso").read_bytes() == b"data"
    assert not primary.exists()  # moved


def test_trash_paths_preserves_relative_layout(tmp_path: Path) -> None:
    """Files keep their path relative to roms_base, so a manual restore is `mv`."""
    roms_base = tmp_path / "ROMs"
    a = roms_base / "psx" / "CD1.cue"
    b = roms_base / "psx" / "CD1.bin"
    for p in (a, b):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    target = trash_paths(
        [a, b], rom_id=7, trash_root=tmp_path / "trash", roms_base=roms_base, now=_now()
    )
    assert (target / "psx" / "CD1.cue").exists()
    assert (target / "psx" / "CD1.bin").exists()


def test_trash_paths_handles_collision_with_counter_suffix(tmp_path: Path) -> None:
    """Two trash events for the same rom in the same second don't collide."""
    roms_base = tmp_path / "ROMs"
    a = roms_base / "gc" / "A.iso"
    b = roms_base / "gc" / "B.iso"
    a.parent.mkdir(parents=True)
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    first = trash_paths(
        [a], rom_id=1, trash_root=tmp_path / "trash", roms_base=roms_base, now=_now()
    )
    second = trash_paths(
        [b], rom_id=1, trash_root=tmp_path / "trash", roms_base=roms_base, now=_now()
    )
    assert first != second
    assert second.name.endswith("-1")


def test_trash_paths_skips_missing_paths(tmp_path: Path) -> None:
    """A path that's already gone is silently skipped — no error."""
    roms_base = tmp_path / "ROMs"
    roms_base.mkdir()
    target = trash_paths(
        [roms_base / "gone.iso"],
        rom_id=99,
        trash_root=tmp_path / "trash",
        roms_base=roms_base,
        now=_now(),
    )
    # Trash dir is created (always); empty.
    assert target.exists()
    assert list(target.iterdir()) == []


def test_trash_paths_falls_back_to_flat_for_outside_roms_base(tmp_path: Path) -> None:
    """A path outside roms_base lands flat under the trash dir."""
    roms_base = tmp_path / "ROMs"
    roms_base.mkdir()
    other = tmp_path / "elsewhere" / "rogue.iso"
    other.parent.mkdir()
    other.write_bytes(b"r")
    target = trash_paths(
        [other], rom_id=42, trash_root=tmp_path / "trash", roms_base=roms_base, now=_now()
    )
    assert (target / "rogue.iso").exists()


# ---------------------------------------------------------------------------
# purge_expired
# ---------------------------------------------------------------------------


def test_purge_expired_removes_old_dirs(tmp_path: Path) -> None:
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    # Old: 30 days back.
    old_ts = (_now() - timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
    (trash_root / f"{old_ts}__rom1").mkdir()
    # Fresh: today.
    fresh_ts = _now().strftime("%Y%m%dT%H%M%SZ")
    (trash_root / f"{fresh_ts}__rom2").mkdir()

    purged = purge_expired(trash_root, retention_days=14, now=_now())
    assert purged == 1
    assert (trash_root / f"{fresh_ts}__rom2").exists()
    assert not (trash_root / f"{old_ts}__rom1").exists()


def test_purge_expired_keeps_dirs_within_retention(tmp_path: Path) -> None:
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    # Exactly at the boundary — kept.
    edge_ts = (_now() - timedelta(days=14)).strftime("%Y%m%dT%H%M%SZ")
    (trash_root / f"{edge_ts}__rom1").mkdir()

    assert purge_expired(trash_root, retention_days=14, now=_now()) == 0
    assert (trash_root / f"{edge_ts}__rom1").exists()


def test_purge_expired_ignores_nontimestamped_dirs(tmp_path: Path) -> None:
    """User-created or malformed dirs in trash are left alone."""
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    (trash_root / "not-a-timestamped-dir").mkdir()
    (trash_root / "malformed__rom1").mkdir()

    assert purge_expired(trash_root, retention_days=0, now=_now()) == 0
    assert (trash_root / "not-a-timestamped-dir").exists()
    assert (trash_root / "malformed__rom1").exists()


def test_purge_expired_handles_missing_root(tmp_path: Path) -> None:
    """Fresh ferry: trash dir doesn't exist yet — no-op."""
    assert purge_expired(tmp_path / "nope", retention_days=14, now=_now()) == 0


def test_purge_expired_zero_retention_purges_everything_dated(tmp_path: Path) -> None:
    """retention_days=0 means anything past 'now' is expired (effectively immediate)."""
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    ts = (_now() - timedelta(seconds=1)).strftime("%Y%m%dT%H%M%SZ")
    (trash_root / f"{ts}__rom1").mkdir()
    assert purge_expired(trash_root, retention_days=0, now=_now()) == 1


# ---------------------------------------------------------------------------
# trash_bios_files — BIOS analogue (v5.5)
# ---------------------------------------------------------------------------


def test_trash_bios_files_creates_bios_keyed_dir(tmp_path: Path) -> None:
    bios_base = tmp_path / "bios"
    fw = bios_base / "ps2-0230a.bin"
    fw.parent.mkdir(parents=True)
    fw.write_bytes(b"bios")

    trash_root = tmp_path / "trash"
    target = trash_bios_files([fw], 7, trash_root=trash_root, bios_base=bios_base, now=_now())

    assert target == trash_root / "20260425T183000Z__bios7"
    assert (target / "ps2-0230a.bin").read_bytes() == b"bios"
    assert not fw.exists()


def test_trash_bios_files_preserves_subfolder_layout(tmp_path: Path) -> None:
    bios_base = tmp_path / "bios"
    fw = bios_base / "dc" / "dc_boot.bin"
    fw.parent.mkdir(parents=True)
    fw.write_bytes(b"dc")

    target = trash_bios_files(
        [fw], 3, trash_root=tmp_path / "trash", bios_base=bios_base, now=_now()
    )
    assert (target / "dc" / "dc_boot.bin").read_bytes() == b"dc"


def test_purge_expired_sweeps_bios_trash_dirs(tmp_path: Path) -> None:
    """`__bios` dirs ride the same retention clock as `__rom` dirs."""
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    old_ts = (_now() - timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
    (trash_root / f"{old_ts}__bios7").mkdir()
    assert purge_expired(trash_root, retention_days=14, now=_now()) == 1
    assert not (trash_root / f"{old_ts}__bios7").exists()
