"""Soft-delete primitive — move files to a timestamped trash directory.

DESIGN.md §5.1 calls for delete-on-remove with retention: items disappear
from `roms_base/` but are recoverable for `trash_retention_days` (default
14) by walking the trash tree before they're purged.

Layout: `<trash_root>/<UTC-timestamp>__<key>[-<n>]/`, where `<key>` is
`rom<rom_id>` for ROMs and `bios<firmware_id>` for BIOS files. The `-n`
suffix only kicks in when two trash events for the same key collide
within the same second. Files inside preserve their path relative to
the anchor dir (`roms_base` / `bios_base`) so a manual restore is
`mv <trash>/<rel> <anchor>/<rel>`.

`purge_expired` is meant to run at the start of each `ferry sync` —
trash older than the configured retention is removed.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ferry.domain.user_dirs import state_dir

logger = logging.getLogger(__name__)


def default_trash_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the canonical trash directory."""
    return state_dir(env) / "ferry" / "trash"


def trash_paths(
    paths: list[Path | tuple[Path, Path]],
    rom_id: int,
    *,
    trash_root: Path,
    roms_base: Path,
    now: datetime | None = None,
) -> Path:
    """Move *paths* into a fresh timestamped trash dir keyed by *rom_id*.

    Each entry is one of:
      - `Path`: relative path within the trash dir is computed against
        *roms_base*; falls back to the bare filename when the path
        isn't under roms_base.
      - `(source, rel)` tuple: *rel* is used directly as the
        trash-dir-relative path. Used to trash files that live outside
        roms_base (e.g. sidecars) but should sit at a known location
        inside the trash dir for manual restore.

    Paths that don't exist are skipped silently.

    Returns the trash dir created (always; even if every path was missing
    on disk so the dir ends up empty — caller may inspect or rmdir).
    """
    return _trash_into(paths, key=f"rom{rom_id}", trash_root=trash_root, anchor=roms_base, now=now)


def trash_bios_files(
    paths: list[Path | tuple[Path, Path]],
    firmware_id: int,
    *,
    trash_root: Path,
    bios_base: Path,
    now: datetime | None = None,
) -> Path:
    """Move *paths* into a fresh timestamped trash dir for a deleted BIOS file.

    The BIOS analogue of `trash_paths` (v5.5): identical timestamped
    layout — so `purge_expired` sweeps these on the same retention clock —
    keyed by RomM firmware id, with rel paths anchored at *bios_base*.
    """
    return _trash_into(
        paths, key=f"bios{firmware_id}", trash_root=trash_root, anchor=bios_base, now=now
    )


def _trash_into(
    paths: list[Path | tuple[Path, Path]],
    *,
    key: str,
    trash_root: Path,
    anchor: Path,
    now: datetime | None,
) -> Path:
    """Shared mover behind `trash_paths` / `trash_bios_files`.

    Creates `<trash_root>/<timestamp>__<key>[-<n>]/` and moves each entry
    into it. See `trash_paths` for the per-entry `Path` / tuple contract.
    """
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{timestamp}__{key}"
    target_dir = trash_root / base_name
    counter = 1
    while target_dir.exists():
        target_dir = trash_root / f"{base_name}-{counter}"
        counter += 1
    target_dir.mkdir(parents=True)

    for entry in paths:
        if isinstance(entry, tuple):
            src, rel = entry
        else:
            src = entry
            try:
                rel = src.relative_to(anchor)
            except ValueError:
                # Not under the anchor — fall back to a flat location with
                # the filename. (Sidecar callers should pass tuples to get
                # layout-preserving placement.)
                rel = Path(src.name)
        if not src.exists():
            continue
        dst = target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    return target_dir


def purge_expired(
    trash_root: Path,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Remove trash subdirs older than *retention_days*. Returns count purged.

    Subdirs whose names don't parse as the canonical timestamped format are
    left alone — purge is conservative; manual ferry trash entries (or
    user-created junk) won't be auto-removed.
    """
    if not trash_root.exists():
        return 0
    now = now or datetime.now(UTC)
    threshold = timedelta(days=retention_days)
    purged = 0
    for entry in trash_root.iterdir():
        if not entry.is_dir():
            continue
        age = _trash_dir_age(entry.name, now)
        if age is None or age <= threshold:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        purged += 1
    return purged


def _trash_dir_age(name: str, now: datetime) -> timedelta | None:
    """Return the age of a trash subdir based on its timestamped name."""
    if "__" not in name:
        return None
    ts_str = name.split("__", 1)[0]
    try:
        ts = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return now - ts
