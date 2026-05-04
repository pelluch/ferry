"""Walk RetroArch's saves directory, layout-aware, matching against ROMs.

The directory layout depends on RetroArch's runtime settings (parsed from
`retroarch.cfg` per `RetroArchInstall`):

- `sort_savefiles_by_content_enable=true` AND `sort_savefiles_enable=true` →
  `<saves>/<content>/<core>/<file>`
- `sort_savefiles_by_content_enable=true` only →
  `<saves>/<content>/<file>`
- `sort_savefiles_enable=true` only →
  `<saves>/<core>/<file>`
- both off → `<saves>/<file>` (flat)

ferry's walker handles all four. Saves are matched to ROMs by **filename
stem** — RetroArch's save filename mirrors the content it loaded (the .zip
basename if the core read the archive directly, or the extracted file's
basename if a transform pipeline ran). Each ROM's plausible stems are
indexed and the walker looks each save up against the index. Misses
produce warnings, never aborts.

The save's RomM `emulator` label is `retroarch-<core>` when the layout
exposes a core directory, plain `retroarch` otherwise — that's a
real limitation for `sort_savefiles_by_content_enable=true` setups
(common on RetroDECK), where the path knows the platform but not the
core. Core attribution from RetroArch's playlists is a follow-up; for
v2 we accept the reduced label and document it.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.retroarch_paths import RetroArchInstall
from ferry.domain.state import RomState

logger = logging.getLogger(__name__)

_HASH_BLOCK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalSave:
    """A save file present on disk, matched against a known ROM."""

    rom_id: int
    emulator: str  # "retroarch" or "retroarch-<core>"
    slot: str  # "default" for v2 (SRAM-style)
    save_filename: str
    local_path: Path
    local_mtime: float
    local_md5: str
    local_size: int


def list_local_saves(
    install: RetroArchInstall,
    roms: Iterable[RomState],
) -> tuple[list[LocalSave], list[str]]:
    """Walk *install*'s savefile directory and return matched saves + warnings.

    Treats a missing or non-directory savefile_directory as "no saves yet"
    (RetroArch creates it lazily). Returns sorted-by-path output for
    deterministic CLI display.
    """
    saves_dir = install.savefile_directory
    if not saves_dir.is_dir():
        return [], []

    rom_index = _build_stem_index(roms)
    matched: list[LocalSave] = []
    warnings: list[str] = []

    for path in sorted(p for p in saves_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(saves_dir)
        rom = rom_index.get(path.stem)
        if rom is None:
            warnings.append(
                f"could not match save {str(rel)!r} to any known ROM "
                "(skipping — may belong to a ROM not synced via ferry)"
            )
            continue

        emulator = _emulator_from_layout(
            rel,
            sort_savefiles_enable=install.sort_savefiles_enable,
            sort_savefiles_by_content_enable=install.sort_savefiles_by_content_enable,
        )
        try:
            stat = path.stat()
            local_md5 = _md5_of_file(path)
        except OSError as exc:
            warnings.append(f"could not read save {str(rel)!r}: {exc}")
            continue

        matched.append(
            LocalSave(
                rom_id=rom.rom_id,
                emulator=emulator,
                slot="default",
                save_filename=path.name,
                local_path=path,
                local_mtime=stat.st_mtime,
                local_md5=local_md5,
                local_size=stat.st_size,
            )
        )

    return matched, warnings


def _emulator_from_layout(
    rel: Path,
    *,
    sort_savefiles_enable: bool,
    sort_savefiles_by_content_enable: bool,
) -> str:
    """Map (path-relative-to-saves, sort_*) to the RomM emulator label.

    Layouts:
      both true  → <content>/<core>/file → `retroarch-<core>` (parts[1])
      core only  → <core>/file           → `retroarch-<core>` (parts[0])
      content    → <content>/file        → `retroarch` (we don't know the core)
      neither    → file                  → `retroarch`

    Saves found at unexpected depths (e.g., user manually nested files
    inside what should be a flat layout) fall through to plain
    `retroarch` — better than guessing wrong.
    """
    parts = rel.parts
    if sort_savefiles_enable and sort_savefiles_by_content_enable:
        if len(parts) >= 3:
            return f"retroarch-{parts[1]}"
        return "retroarch"
    if sort_savefiles_enable and not sort_savefiles_by_content_enable:
        if len(parts) >= 2:
            return f"retroarch-{parts[0]}"
        return "retroarch"
    return "retroarch"


def _build_stem_index(roms: Iterable[RomState]) -> dict[str, RomState]:
    """Index every plausible save-filename stem to its owning ROM.

    Indexes both the source filename's stem (RA core read the archive
    directly) and each transformed output's stem (RA loaded an extracted
    file). Last-write-wins on duplicate stems — collisions are rare in
    practice, and v2 doesn't need to surface ambiguity beyond the warnings.
    """
    index: dict[str, RomState] = {}
    for rom in roms:
        index[Path(rom.source_filename).stem] = rom
        for output in rom.outputs:
            index[Path(output.path).stem] = rom
    return index


def _md5_of_file(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_BLOCK_SIZE):
            md5.update(chunk)
    return md5.hexdigest()
