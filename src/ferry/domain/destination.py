from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


class _PresetPaths(NamedTuple):
    """Canonical default paths for a preset, relative to $HOME.

    `bios_base` is None for frontends that don't centralize BIOS — bare ES-DE
    leaves BIOS placement to each emulator (RetroArch's system/, Dolphin's
    Sys/, etc.). Multi-destination BIOS routing (DESIGN.md §5.2) lands the
    files in the right per-emulator paths when bios_base is None.
    """

    roms_base: str
    bios_base: str | None


# Built-in destination presets.
#
# `esde-flatpak` and `esde-native` share data paths intentionally — the split
# matters for v4 launcher overrides (config dir, launch prefix), not for v1
# where we only land files under roms_base. Users pick the right one upfront
# so v4 has the metadata it needs without a config migration.
#
# Paths are anchored at $HOME to keep the table data-only; resolution to
# absolute paths happens in `resolve_preset` against an injected home dir.
PRESETS: dict[str, _PresetPaths] = {
    "retrodeck-flatpak": _PresetPaths("retrodeck/roms", "retrodeck/bios"),
    "emudeck": _PresetPaths("Emulation/roms", "Emulation/bios"),
    # Bare ES-DE (both native and flatpak) defaults to ~/ROMs for ROMs and
    # has no centralized BIOS root — each emulator owns its own BIOS dir.
    "esde-flatpak": _PresetPaths("ROMs", None),
    "esde-native": _PresetPaths("ROMs", None),
}


def resolve_preset(name: str, home: Path) -> tuple[Path, Path | None]:
    """Return (roms_base, bios_base) absolute paths for *name* under *home*.

    `bios_base` is None for presets without a centralized BIOS root.
    """
    p = PRESETS[name]
    bios = home / p.bios_base if p.bios_base is not None else None
    return home / p.roms_base, bios


@dataclass(frozen=True, slots=True, kw_only=True)
class Destination:
    """Where ferry lands ROM and BIOS files on disk.

    `bios_base` may be None when the frontend doesn't centralize BIOS placement
    (bare ES-DE). v1 BIOS routing dispatches to per-emulator paths in that
    case via the registry.

    `preset` is display-only metadata recording which recipe (if any) the
    config used; the actual paths are always carried explicitly so callers
    don't have to re-resolve.
    """

    roms_base: Path
    bios_base: Path | None = None
    preset: str | None = None
