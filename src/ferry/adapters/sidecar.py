"""Per-ROM sidecar files for state recovery.

A sidecar is a small JSON file alongside each ROM's primary output, containing
the same `RomState` info that lives in state.json. If state.json is lost,
ferry can walk the ROM tree, read sidecars, and reconstruct state from them.
Sidecars are also a stable visual marker that ferry "manages" a given file.

Naming convention: `<primary_output_basename>.ferry.json`. For a multi-disc
ROM whose primary output is `Game.m3u`, the sidecar is `Game.m3u.ferry.json`
and lists all output files (.m3u + .cue + .bin parts).
"""

import os
from pathlib import Path

from ferry.domain.state import RomState, rom_from_json, rom_to_json

SIDECAR_SUFFIX = ".ferry.json"


def sidecar_path_for(primary_output: Path) -> Path:
    """Return the sidecar path for a given primary output file."""
    return primary_output.with_name(primary_output.name + SIDECAR_SUFFIX)


def write_sidecar(primary_output: Path, rom: RomState) -> Path:
    """Write a sidecar next to *primary_output*, atomically."""
    target = sidecar_path_for(primary_output)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = rom_to_json(rom)
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(target)
    return target


def read_sidecar(primary_output: Path) -> RomState | None:
    """Read the sidecar for *primary_output*, returning None if absent."""
    path = sidecar_path_for(primary_output)
    if not path.exists():
        return None
    return rom_from_json(path.read_text())


def find_sidecars(roots: list[Path]) -> list[Path]:
    """Walk *roots* and return all sidecar paths found.

    Used during reconcile to rebuild state from sidecars when state.json is
    missing or stale. Each root is typically `Destination.roms_base` plus its
    per-platform subdirs; pass them all so the walk doesn't span unrelated
    trees.
    """
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob(f"*{SIDECAR_SUFFIX}"):
            if path.is_file():
                out.append(path)
    return sorted(out)
