"""Walk a Dolphin GCI Folder save tree, matching .gci files to known ROMs.

For each GameCube ROM in the library, ferry:

1. Reads the disc header (via `dolphin_tool`, with on-disk caching) to
   get the 6-char `game_id` and `region`. The first 4 chars are the
   gamecode, the last 2 are the maker code.
2. Maps `region` → folder name based on the install's `region_encoding`
   (3-letter `USA/JAP/EUR` for native + EmuDeck; 2-letter `US/JP/EU` for
   RetroDECK).
3. Globs `<saves_root>/<region_folder>/Card A|B/<MAKER>-<CODE>-*.gci` —
   matches every save Dolphin has written for that game across BOTH
   memory cards (Argosy parity; v3.6 was Card A only). A single ROM
   typically produces multiple .gci files (Smash Melee replays, F-Zero
   GX ghosts, MKDD course ghosts, etc.).
4. Bundles the matched .gci files into ONE LocalSave per ROM (v3.7
   Argosy schema; v3.6 emitted one LocalSave per .gci). Slot +
   filename = `<rom_base_name>` / `<rom_base_name>.zip`. Emulator tag
   `dolphin` (unchanged from v3.6).
5. Filename clash between Card A and Card B for the same ROM (same
   `<MAKER>-<CODE>-<INTERNAL>.gci` in both): **Card A wins, warn**.
   Deterministic + matches v3 Card A bias. Argosy's behavior here is
   non-deterministic (Android file-iteration order); see open-issues
   notes in `project_argosy_save_schema` memory.

Non-GameCube ROMs are skipped without invoking dolphin-tool. Unknown
regions (currently NTSC-K) and ROMs whose header can't be read produce
warnings; they don't abort the walk.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from ferry.adapters.dolphin.dolphin_paths import DolphinInstall, RegionEncoding
from ferry.adapters.dolphin.dolphin_tool import (
    DiscHeader,
    DiscHeaderCache,
    DolphinTool,
    lookup_disc_header,
)
from ferry.adapters.dolphin.wii_archive import files_content_hash
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.save_local import LocalSave
from ferry.domain.state import RomState

__all__ = (
    "LocalSave",
    "list_local_saves",
    "match_rom_gcis",
    "region_card_dir",
)

logger = logging.getLogger(__name__)

_GAMECUBE_PLATFORM_DIR = "gc"
_DOLPHIN_EMULATOR_LABEL = "dolphin"
_CARDS: tuple[str, ...] = ("Card A", "Card B")

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


def list_local_saves(
    install: DolphinInstall,
    roms: Iterable[RomState],
    *,
    roms_base: Path,
    tool: DolphinTool,
    cache: DiscHeaderCache | None = None,
) -> tuple[list[LocalSave], list[str]]:
    """Walk *install*'s saves_root and emit one LocalSave per GC ROM with saves.

    Filters `roms` to GameCube only (resolved platform dir == `gc`); other
    platforms are silently ignored. Each GC ROM's primary output is read
    via `dolphin-tool` to get its game_id; matching .gci files are
    collected from `<saves_root>/<region_folder>/Card A|B/<MAKER>-<CODE>-
    *.gci`. The matched set is bundled into a single LocalSave with
    `local_md5 = files_content_hash(matched, wrapper=<rom_base_name>)`.

    Warnings cover: ROM file missing on disk, dolphin-tool failure,
    unknown region, Card A vs Card B filename clash. None of these
    abort the walk — the caller surfaces them and the rest of the
    sync proceeds.

    `LocalSave.local_path` is set to `<saves_root>` (just an "exists"
    sentinel for the base class's path probe) — the actual matched
    GCI list is recomputed at upload time via `match_rom_gcis`. Avoids
    growing `LocalSave`'s shape with a per-backend list-of-files field
    and keeps walker + uploader on the same matcher.

    Returns (saves sorted by rom_id for deterministic output, warnings).
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

        if _region_folder(header.region, install.region_encoding) is None:
            warnings.append(
                f"rom_id={rom.rom_id} ({rom.name}): unsupported region "
                f"{header.region!r} (only NTSC-U / NTSC-J / PAL are mapped)"
            )
            continue

        gci_paths, match_warnings = match_rom_gcis(install, header, rom=rom)
        warnings.extend(match_warnings)
        if not gci_paths:
            continue  # No saves yet for this ROM; not a warning.

        rom_base_name = Path(rom.primary_output.path).stem
        try:
            local_md5 = files_content_hash(gci_paths, wrapper=rom_base_name)
            stats = [p.stat() for p in gci_paths]
        except OSError as exc:
            warnings.append(f"rom_id={rom.rom_id}: could not stat/hash GCIs: {exc}")
            continue
        local_size = sum(s.st_size for s in stats)
        local_mtime = max(s.st_mtime for s in stats)

        matched.append(
            LocalSave(
                rom_id=rom.rom_id,
                emulator=_DOLPHIN_EMULATOR_LABEL,
                slot=rom_base_name,
                save_filename=f"{rom_base_name}.zip",
                # Saves_root as a sentinel: base class only uses this
                # for `is_dir/is_file` existence probes; the real GCI
                # set is reconstructed by `match_rom_gcis` at upload
                # time so the walker and the upload path can never
                # diverge on what counts as "this rom's saves".
                local_path=install.saves_root,
                local_mtime=local_mtime,
                local_md5=local_md5,
                local_size=local_size,
            )
        )

    matched.sort(key=lambda ls: ls.rom_id)
    return matched, warnings


def match_rom_gcis(
    install: DolphinInstall,
    header: DiscHeader,
    *,
    rom: RomState | None = None,
) -> tuple[list[Path], list[str]]:
    """Return all .gci files belonging to *header*'s game, mashed across cards.

    Walks `<saves_root>/<region_folder>/{Card A, Card B}/` and globs
    `<MAKER>-<CODE>-*.gci`. Card A entries are taken first; Card B
    entries with a colliding basename are dropped + warned (Card A
    wins). Returns (deduplicated paths sorted by basename, warnings).

    `rom` is optional and only used to enrich warnings with the ROM
    name when called from the walker. Callers from the upload path
    (where the LocalSave already encodes the rom) can omit it; warnings
    will reference the game_code instead.

    Returns ([], []) when the region isn't supported or the region
    directory doesn't exist on disk — caller treats this as "no saves
    yet for this rom" and skips silently.
    """
    region_folder = _region_folder(header.region, install.region_encoding)
    if region_folder is None:
        return [], []

    region_dir = install.saves_root / region_folder
    if not region_dir.is_dir():
        return [], []

    rom_label = (
        f"rom_id={rom.rom_id} ({rom.name})"
        if rom is not None
        else f"game={header.maker_code}-{header.game_code}"
    )
    prefix = f"{header.maker_code}-{header.game_code}-"

    by_name: dict[str, Path] = {}
    warnings: list[str] = []
    for card in _CARDS:
        card_dir = region_dir / card
        if not card_dir.is_dir():
            continue
        for path in sorted(card_dir.glob(f"{prefix}*.gci")):
            if not path.is_file():
                continue
            existing = by_name.get(path.name)
            if existing is None:
                by_name[path.name] = path
                continue
            # Filename clash. `_CARDS` is iterated A-then-B, so the
            # incumbent always wins — surface a warning so the user
            # knows Card B's copy was ignored.
            warnings.append(
                f"{rom_label}: {path.name} exists in both Card A and Card B "
                f"(using Card A's copy at {existing}; Card B's copy at {path} ignored)"
            )

    matched = sorted(by_name.values(), key=lambda p: p.name)
    return matched, warnings


def region_card_dir(install: DolphinInstall, region: str) -> Path | None:
    """Card A directory for *region*'s saves under *install*'s saves_root.

    Used by the GC backend on download to land all extracted GCIs in
    one place — Card A is always the destination since v3.7 bundles
    don't carry per-GCI card-source metadata. Card B becomes
    effectively read-only (we walk it on upload but never write to it
    on download). Returns None for regions not in the supported set.
    """
    folder = _region_folder(region, install.region_encoding)
    if folder is None:
        return None
    return install.saves_root / folder / "Card A"


def _gamecube_roms(roms: Iterable[RomState]) -> list[RomState]:
    """Return only ROMs whose resolved platform dir is `gc`."""
    return [
        rom for rom in roms if resolve_platform_dir(rom.platform_slug) == _GAMECUBE_PLATFORM_DIR
    ]


def _region_folder(region: str, encoding: RegionEncoding) -> str | None:
    """Map Dolphin Region enum value to the install's folder convention."""
    table = _REGION_FOLDERS_3LETTER if encoding == "3-letter" else _REGION_FOLDERS_2LETTER
    return table.get(region)
