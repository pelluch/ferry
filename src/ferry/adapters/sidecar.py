"""Per-ROM sidecar files for state recovery.

A sidecar is a small JSON file alongside each ROM's primary output, containing
the same `RomState` info that lives in state.json. If state.json is lost,
ferry can walk the ROM tree, read sidecars, and reconstruct state from them.
Sidecars are also a stable visual marker that ferry "manages" a given file.

Naming convention: `.<primary_output_basename>.ferry.json` — leading dot
makes the file hidden by UNIX convention, so ES-DE / RetroDECK / file
managers skip it during their default-visible listings. (RetroDECK's
GameCube system definition accepts `.json` as a valid extension, which
without the dot caused sidecars to show up as fake "games" in the
frontend. Live testing surfaced this; v1's visible-sidecar choice was
flagged as an open question in DESIGN.md §5.5.)

For a multi-disc ROM whose primary output is `Game.m3u`, the sidecar is
`.Game.m3u.ferry.json` and lists all output files (.m3u + .cue + .bin
parts).

Backward-compat: legacy `<rom>.ferry.json` (no leading dot) sidecars
written by earlier ferry releases are still readable, and on the next
write of any sidecar the legacy twin is removed (silent migration).
`find_sidecars` returns both styles so state-from-sidecars recovery
keeps working through the migration window.
"""

import contextlib
import os
from pathlib import Path

from ferry.domain.state import RomState, rom_from_json, rom_to_json

SIDECAR_SUFFIX = ".ferry.json"
SIDECAR_PREFIX = "."


def sidecar_path_for(primary_output: Path) -> Path:
    """Return the canonical (dot-prefixed, hidden) sidecar path."""
    return primary_output.with_name(SIDECAR_PREFIX + primary_output.name + SIDECAR_SUFFIX)


def legacy_sidecar_path_for(primary_output: Path) -> Path:
    """Return the pre-migration sidecar path (no leading dot).

    Old releases wrote `<rom>.ferry.json`. We still read these so existing
    on-disk state isn't lost; `write_sidecar` removes them after a
    successful write of the new dot-prefixed file.
    """
    return primary_output.with_name(primary_output.name + SIDECAR_SUFFIX)


def write_sidecar(primary_output: Path, rom: RomState) -> Path:
    """Write a sidecar next to *primary_output*, atomically.

    On success, removes any legacy non-dot-prefixed sidecar at the old
    path so the on-disk state converges to one canonical location.
    """
    target = sidecar_path_for(primary_output)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = rom_to_json(rom)
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(target)

    legacy = legacy_sidecar_path_for(primary_output)
    if legacy.exists() and legacy != target:
        # Non-fatal cleanup — both files describe the same ROM; the new
        # dot-prefixed one is the source of truth. Next write will retry.
        with contextlib.suppress(OSError):
            legacy.unlink()
    return target


def read_sidecar(primary_output: Path) -> RomState | None:
    """Read the sidecar for *primary_output*, returning None if absent.

    Checks the canonical (dot-prefixed) path first, then falls back to
    the legacy location for not-yet-migrated state.
    """
    canonical = sidecar_path_for(primary_output)
    if canonical.exists():
        return rom_from_json(canonical.read_text())
    legacy = legacy_sidecar_path_for(primary_output)
    if legacy.exists():
        return rom_from_json(legacy.read_text())
    return None


def find_sidecars(roots: list[Path]) -> list[Path]:
    """Walk *roots* and return all sidecar paths (canonical + legacy).

    Used during reconcile to rebuild state from sidecars when state.json is
    missing or stale. Each root is typically `Destination.roms_base` plus its
    per-platform subdirs; pass them all so the walk doesn't span unrelated
    trees.
    """
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        # `*<suffix>` matches both legacy `name.ferry.json` and canonical
        # `.name.ferry.json` — Path.rglob's leading-dot doesn't restrict.
        for path in root.rglob(f"*{SIDECAR_SUFFIX}"):
            if path.is_file():
                out.append(path)
    return sorted(out)
