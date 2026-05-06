"""Per-ROM sidecar files for state recovery.

A sidecar is a small JSON file mirroring the same `RomState` info that
lives in state.json. If state.json is lost, ferry can walk the sidecar
tree and reconstruct state — recovery is the load-bearing reason
sidecars exist.

**Location** (current):
`$XDG_STATE_HOME/ferry/sidecars/<rel-of-primary-under-roms-base>.ferry.json`.

The sidecar tree mirrors the ROM tree under `sidecars_root`. A ROM at
`<roms_base>/gc/Pikmin (USA).iso` gets its sidecar at
`<sidecars_root>/gc/Pikmin (USA).iso.ferry.json`. The location was
moved out of the ROM tree in v8 ck4 because sidecars-next-to-rom
caused friction with file managers, ES-DE's GameCube system definition
(which accepts `.json` ROMs and surfaced sidecars as fake "games"),
backup tools, and `ls -A`. Out-of-tree placement is unconditionally
invisible regardless of frontend or `ShowHiddenFiles` settings.

**Legacy sidecars** still on disk (next-to-rom, two prior schemes):

  1. `<rom>.ferry.json` — original v1 layout (visible).
  2. `.<rom>.ferry.json` — v2 dot-prefixed (hidden, but ES-DE's GC
     system still picked them up when `ShowHiddenFiles=true`).

Read paths fall back to both. `write_sidecar` writes only the canonical
location and removes any legacy twin it finds. `migrate_legacy_sidecars`
is the one-shot sweep called at the start of `ferry sync` after upgrade
— it walks `roms_base` for legacy sidecars and migrates them in one
pass, making the next sync find zero legacies.

For a multi-disc ROM whose primary output is `Game.m3u`, the sidecar is
keyed off the primary's relative path and lists all output files
(.m3u + .cue + .bin parts) inside.
"""

import contextlib
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from ferry.domain.state import RomState, StateDecodeError, rom_from_json, rom_to_json

logger = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".ferry.json"
SIDECAR_PREFIX = "."  # leading dot used by the v2 legacy layout


def default_sidecars_root(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_STATE_HOME/ferry/sidecars`, default `~/.local/state/ferry/sidecars`."""
    env = env if env is not None else os.environ
    base = env.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "ferry" / "sidecars"


def sidecar_path_for(
    primary_output: Path,
    *,
    roms_base: Path,
    sidecars_root: Path | None = None,
) -> Path:
    """Resolve the canonical sidecar path for *primary_output*.

    The sidecar tree under `sidecars_root` mirrors the ROM tree under
    `roms_base`. `primary_output` MUST sit under `roms_base` (raises
    ValueError otherwise) — sidecar identity is keyed off the relative
    path so a ROM moving inside roms_base also moves its sidecar.
    """
    sidecars_root = sidecars_root if sidecars_root is not None else default_sidecars_root()
    rel = primary_output.relative_to(roms_base)
    return sidecars_root / f"{rel}{SIDECAR_SUFFIX}"


def legacy_sidecar_paths_for(primary_output: Path) -> tuple[Path, Path]:
    """Pre-relocation sidecar paths (next-to-rom). Returns (dot, plain).

    Each prior layout reads a sibling of the ROM file:
      - dot:   `.<rom>.<ext>.ferry.json` (v2)
      - plain: `<rom>.<ext>.ferry.json`  (v1)

    Returned in newest-first order so reads check the most-recent legacy
    location first.
    """
    return (
        primary_output.with_name(SIDECAR_PREFIX + primary_output.name + SIDECAR_SUFFIX),
        primary_output.with_name(primary_output.name + SIDECAR_SUFFIX),
    )


def write_sidecar(
    primary_output: Path,
    rom: RomState,
    *,
    roms_base: Path,
    sidecars_root: Path | None = None,
) -> Path:
    """Write a sidecar to the canonical location, atomically.

    On success, removes any legacy next-to-rom sidecars at the v1/v2
    paths so the on-disk state converges to one canonical location.
    Returns the path written.
    """
    target = sidecar_path_for(primary_output, roms_base=roms_base, sidecars_root=sidecars_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = rom_to_json(rom)
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(target)

    for legacy in legacy_sidecar_paths_for(primary_output):
        if legacy.exists() and legacy != target:
            # Non-fatal cleanup — sidecar at the canonical location is the
            # source of truth; legacy is redundant data. Next write retries.
            with contextlib.suppress(OSError):
                legacy.unlink()
    return target


def read_sidecar(
    primary_output: Path,
    *,
    roms_base: Path | None = None,
    sidecars_root: Path | None = None,
) -> RomState | None:
    """Read the sidecar for *primary_output*, falling back through legacies.

    Order: canonical (state dir) → v2 dot-prefixed next-to-rom → v1
    plain next-to-rom. Returns None when no sidecar exists at any of
    those paths.

    `roms_base=None` skips the canonical lookup and only checks legacy
    next-to-rom paths — used by the launch-hook flow where destination
    isn't necessarily configured.
    """
    if roms_base is not None:
        canonical = sidecar_path_for(
            primary_output, roms_base=roms_base, sidecars_root=sidecars_root
        )
        if canonical.exists():
            return rom_from_json(canonical.read_text())
    for legacy in legacy_sidecar_paths_for(primary_output):
        if legacy.exists():
            return rom_from_json(legacy.read_text())
    return None


def find_sidecars(
    *,
    roms_base: Path,
    sidecars_root: Path | None = None,
) -> list[Path]:
    """Walk both the canonical sidecar dir and `roms_base` for sidecars.

    Returns ALL sidecar paths (canonical + legacy v1 + legacy v2). Used
    during state recovery — caller doesn't care which scheme each came
    from, only that each parses to a valid `RomState`.
    """
    sidecars_root = sidecars_root if sidecars_root is not None else default_sidecars_root()
    out: list[Path] = []
    if sidecars_root.is_dir():
        for path in sidecars_root.rglob(f"*{SIDECAR_SUFFIX}"):
            if path.is_file():
                out.append(path)
    if roms_base.is_dir():
        # `*<suffix>` matches both legacy `name.ferry.json` and `.name.ferry.json`.
        for path in roms_base.rglob(f"*{SIDECAR_SUFFIX}"):
            if path.is_file():
                out.append(path)
    return sorted(out)


def migrate_legacy_sidecars(
    *,
    roms_base: Path,
    sidecars_root: Path | None = None,
) -> int:
    """Move all legacy next-to-rom sidecars under `roms_base` to `sidecars_root`.

    Idempotent — runs at start of every `ferry sync` so a single sync
    after upgrade clears the legacy state. Returns count migrated.
    Stragglers (sidecars whose canonical home already has content) are
    just deleted; on collision the canonical version wins.
    """
    sidecars_root = sidecars_root if sidecars_root is not None else default_sidecars_root()
    if not roms_base.is_dir():
        return 0
    migrated = 0
    for legacy in roms_base.rglob(f"*{SIDECAR_SUFFIX}"):
        if not legacy.is_file():
            continue
        primary = _primary_from_legacy_sidecar(legacy)
        if primary is None:
            continue
        target = sidecar_path_for(primary, roms_base=roms_base, sidecars_root=sidecars_root)
        if target.exists():
            # Canonical already has content — drop the redundant legacy file.
            with contextlib.suppress(OSError):
                legacy.unlink()
            migrated += 1
            continue
        try:
            content = legacy.read_text()
            rom_from_json(content)  # validate before promoting
        except (OSError, StateDecodeError) as e:
            logger.warning("skipping malformed legacy sidecar %s: %s", legacy, e)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        with contextlib.suppress(OSError):
            legacy.unlink()
        migrated += 1
    return migrated


def _primary_from_legacy_sidecar(sidecar: Path) -> Path | None:
    """Reverse the legacy naming back to the primary ROM file path.

    Handles both v1 (`<rom>.ferry.json`) and v2 (`.<rom>.ferry.json`)
    schemes. Returns None for filenames that don't fit either pattern
    (defensive — callers ignore None).
    """
    name = sidecar.name
    if not name.endswith(SIDECAR_SUFFIX):
        return None
    stem = name[: -len(SIDECAR_SUFFIX)]
    if stem.startswith(SIDECAR_PREFIX):
        stem = stem[len(SIDECAR_PREFIX) :]
    if not stem:
        return None
    return sidecar.with_name(stem)
