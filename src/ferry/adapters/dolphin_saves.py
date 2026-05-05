"""Walk a Dolphin GCI Folder save tree, matching .gci files to known ROMs.

For each GameCube ROM in the library, ferry:

1. Reads the disc header (via `dolphin_tool`, with on-disk caching) to
   get the 6-char `game_id` and `region`. The first 4 chars are the
   gamecode, the last 2 are the maker code.
2. Maps `region` → folder name based on the install's `region_encoding`
   (3-letter `USA/JAP/EUR` for native + EmuDeck; 2-letter `US/JP/EU` for
   RetroDECK).
3. Globs `<saves_root>/<region_folder>/Card A/<MAKER>-<CODE>-*.gci` —
   matches every save Dolphin has written for that game. A single ROM
   can produce multiple .gci files (Smash Melee replays, F-Zero GX
   ghosts, MKDD course ghosts, etc.) — each becomes its own LocalSave.
4. Slot key = the filename portion between `<MAKER>-<CODE>-` and `.gci`.
   That portion is bytes 0x08-0x27 of the GCI's directory entry —
   written by the game's save logic (e.g. `MetroidPrime A`,
   `f_zero.dat`, `smashbros_personal_data`) and deterministic across
   devices for the same save identity.

Non-GameCube ROMs are skipped without invoking dolphin-tool. Unknown
regions (currently NTSC-K) and ROMs whose header can't be read produce
warnings; they don't abort the walk.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.dolphin_paths import DolphinInstall, RegionEncoding
from ferry.adapters.dolphin_tool import DiscHeader, DiscHeaderCache, DolphinTool
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.state import RomState

logger = logging.getLogger(__name__)

_HASH_BLOCK_SIZE = 64 * 1024
_GAMECUBE_PLATFORM_DIR = "gc"
_DOLPHIN_EMULATOR_LABEL = "dolphin"

# Dolphin Region enum → folder names. RetroDECK's bundled standalone
# Dolphin truncates to 2 letters; native + EmuDeck use the documented
# 3-letter convention from `Source/Core/Core/HW/GCMemcard/GCMemcardDirectory.cpp`.
_REGION_FOLDERS_3LETTER: dict[str, str] = {
    "NTSC-U": "USA",
    "NTSC-J": "JAP",
    "PAL": "EUR",
}
_REGION_FOLDERS_2LETTER: dict[str, str] = {
    "NTSC-U": "US",
    "NTSC-J": "JP",
    "PAL": "EU",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalSave:
    """A Dolphin .gci save file present on disk, matched against a known ROM.

    Field shape mirrors v2's `retroarch_saves.LocalSave` deliberately —
    when checkpoint 6 extracts the SaveBackend Protocol, both backends
    collapse to a shared type without surgery. `slot` semantics differ
    (v2: SRAM "default" / state slot index; v3: in-game save name from
    GCI's directory entry) but both are stable string keys per backend.
    """

    rom_id: int
    emulator: str  # always "dolphin" for v3
    slot: str  # e.g. "MetroidPrime A", "f_zero.dat", "SuperSmashBros0110290334"
    save_filename: str  # e.g. "01-GM8E-MetroidPrime A.gci"
    local_path: Path
    local_mtime: float
    local_md5: str
    local_size: int


def list_local_saves(
    install: DolphinInstall,
    roms: Iterable[RomState],
    *,
    roms_base: Path,
    tool: DolphinTool,
    cache: DiscHeaderCache | None = None,
) -> tuple[list[LocalSave], list[str]]:
    """Walk *install*'s saves_root and return matched .gci files + warnings.

    Filters `roms` to GameCube only (resolved platform dir == `gc`); other
    platforms are silently ignored. Each GC ROM's primary output is read
    via `dolphin-tool` to get its game_id; saves are then globbed under
    `<saves_root>/<region_folder>/Card A/<MAKER>-<CODE>-*.gci`.

    Warnings cover: ROM file missing on disk, dolphin-tool failure,
    unknown region, malformed save filenames. None of these abort the
    walk — the caller surfaces them and the rest of the sync proceeds.

    Returns (saves sorted by path for deterministic output, warnings).
    """
    if not install.saves_root.is_dir():
        return [], []

    matched: list[LocalSave] = []
    warnings: list[str] = []

    for rom in _gamecube_roms(roms):
        rom_path = roms_base / rom.primary_output.path
        if not rom_path.is_file():
            warnings.append(f"rom_id={rom.rom_id} ({rom.name}): ROM file not on disk at {rom_path}")
            continue

        header = lookup_disc_header(rom_path, tool, cache)
        if header is None:
            warnings.append(
                f"rom_id={rom.rom_id} ({rom.name}): could not read disc header from {rom_path}"
            )
            continue

        region_folder = _region_folder(header.region, install.region_encoding)
        if region_folder is None:
            warnings.append(
                f"rom_id={rom.rom_id} ({rom.name}): unsupported region "
                f"{header.region!r} (only NTSC-U / NTSC-J / PAL are mapped)"
            )
            continue

        card_dir = install.saves_root / region_folder / "Card A"
        if not card_dir.is_dir():
            continue  # No saves for this region yet; not a warning.

        prefix = f"{header.maker_code}-{header.game_code}-"
        for path in sorted(card_dir.glob(f"{prefix}*.gci")):
            if not path.is_file():
                continue
            slot = _slot_from_filename(path.name, prefix)
            if slot is None:
                warnings.append(
                    f"rom_id={rom.rom_id} ({rom.name}): unexpected save filename "
                    f"{path.name!r} (skipped)"
                )
                continue
            try:
                stat = path.stat()
                local_md5 = _md5_of_file(path)
            except OSError as exc:
                warnings.append(f"rom_id={rom.rom_id}: could not read {path}: {exc}")
                continue
            matched.append(
                LocalSave(
                    rom_id=rom.rom_id,
                    emulator=_DOLPHIN_EMULATOR_LABEL,
                    slot=slot,
                    save_filename=path.name,
                    local_path=path,
                    local_mtime=stat.st_mtime,
                    local_md5=local_md5,
                    local_size=stat.st_size,
                )
            )

    return matched, warnings


def _gamecube_roms(roms: Iterable[RomState]) -> list[RomState]:
    """Return only ROMs whose resolved platform dir is `gc`."""
    return [
        rom for rom in roms if resolve_platform_dir(rom.platform_slug) == _GAMECUBE_PLATFORM_DIR
    ]


def lookup_disc_header(
    rom_path: Path,
    tool: DolphinTool,
    cache: DiscHeaderCache | None,
) -> DiscHeader | None:
    """Cache-first header lookup. Populates the cache on a fresh read.

    Used by the walker (this module) and the save backend (which needs
    the disc header again to resolve download destinations). Callers
    expecting headers for many ROMs should pass a shared cache so the
    walker's reads are reused.
    """
    if cache is not None:
        cached = cache.get(rom_path)
        if cached is not None:
            return cached
    header = tool.read_header(rom_path)
    if header is not None and cache is not None:
        cache.put(rom_path, header)
    return header


def _region_folder(region: str, encoding: RegionEncoding) -> str | None:
    """Map Dolphin Region enum value to the install's folder convention."""
    table = _REGION_FOLDERS_3LETTER if encoding == "3-letter" else _REGION_FOLDERS_2LETTER
    return table.get(region)


def resolve_save_path(install: DolphinInstall, region: str, save_filename: str) -> Path | None:
    """Where on disk Dolphin reads/writes a save with this filename.

    `<saves_root>/<region_folder>/Card A/<save_filename>`, or None when
    the region isn't in the supported set (NTSC-U / NTSC-J / PAL). The
    save backend uses this both to compute download destinations and to
    sanity-check upload sources are where Dolphin will read them.
    """
    folder = _region_folder(region, install.region_encoding)
    if folder is None:
        return None
    return install.saves_root / folder / "Card A" / save_filename


def _slot_from_filename(filename: str, prefix: str) -> str | None:
    """Strip `<MAKER>-<CODE>-` prefix and `.gci` suffix from a save filename.

    Returns None if the filename doesn't end in `.gci` or doesn't start
    with the expected prefix — defensive against weird files glob picked
    up that shouldn't have matched (e.g. user manually placed something).
    """
    if not filename.endswith(".gci"):
        return None
    if not filename.startswith(prefix):
        return None
    return filename[len(prefix) : -len(".gci")]


def _md5_of_file(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_BLOCK_SIZE):
            md5.update(chunk)
    return md5.hexdigest()
