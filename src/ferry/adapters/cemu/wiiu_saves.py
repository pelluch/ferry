"""Walk Cemu's Wii U save tree, matching per-title save folders to known ROMs.

Wii U saves live as folders, not files: each game's state is the
contents of `<wiiu_saves_root>/<TITLE_HIGH>/<TITLE_LOW>/` — a folder
holding `user/` (the actual per-account save state) and `meta/`
(`iconTex.tga` + `meta.xml`, Cemu-generated from the ROM). ferry
treats each per-title folder as one `LocalSave` per Wii U ROM;
archive-on-upload and extract-on-download are wired by the Cemu save
backend (ck3) via the generic transform hooks on `SaveBackendBase`.

For each Wii U ROM in the library, ferry:

1. Extracts the title ID via `cemu --extract` (`cemu_tool`,
   cache-backed) — the Wii U analogue of reading a GameCube/Wii disc
   header. The 16-hex title ID splits into `TITLE_HIGH` (the
   title-type prefix, `00050000` for standard games) and `TITLE_LOW`.
2. Probes `<wiiu_saves_root>/<TITLE_HIGH>/<TITLE_LOW>/`. Missing or
   empty (no non-ignored files anywhere underneath) → no save yet for
   this title; skip silently.
3. Computes `LocalSave.local_md5` via `folder_content_hash`, matching
   what RomM's server-side `content_hash` would be for the
   corresponding zip — and what Argosy computes for the same folder.

Scope: only standard-game saves under the title-type prefix Cemu uses
for them. The device-level `system/` tree (play stats, account data)
and system-title saves are intentionally out of scope — see
DESIGN.md §7 v5.

**Argosy compat:** save_filename = `<rom_base_name>.zip`, slot =
`<rom_base_name>`, emulator = `cemu` (Argosy's `SavePathRegistry`
tag). `<rom_base_name>` is `Path(rom.primary_output.path).stem`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from ferry.adapters.cemu.cemu_paths import CemuInstall
from ferry.adapters.cemu.cemu_tool import CemuTool, WiiUTitle, WiiUTitleCache, lookup_wiiu_title
from ferry.adapters.dolphin.dolphin_archive import folder_content_hash, is_save_path_ignored
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.save_local import LocalSave
from ferry.domain.state import RomState

__all__ = ("list_local_saves", "wiiu_save_folder")

logger = logging.getLogger(__name__)

_WIIU_PLATFORM_DIR = "wiiu"
_CEMU_EMULATOR_LABEL = "cemu"


def wiiu_save_folder(install: CemuInstall, title: WiiUTitle) -> Path:
    """Resolve the on-disk save folder for a Wii U title.

    Returns `<wiiu_saves_root>/<TITLE_HIGH>/<TITLE_LOW>/` — the per-game
    folder holding `user/` and `meta/`. The path may not exist on disk,
    or may exist but be empty; callers probe separately to decide
    "no save yet" vs. "has save state".
    """
    return install.wiiu_saves_root / title.title_id_high / title.title_id_low


def list_local_saves(
    install: CemuInstall,
    roms: Iterable[RomState],
    *,
    roms_base: Path,
    tool: CemuTool,
    cache: WiiUTitleCache | None = None,
) -> tuple[list[LocalSave], list[str]]:
    """Walk *install*'s save tree and emit one LocalSave per Wii U title with saves.

    Filters `roms` to Wii U only (resolved platform dir == `wiiu`);
    other platforms are silently ignored. Emits a warning (and skips
    the rom) when the ROM file is missing on disk or the title ID
    can't be extracted (`cemu --extract` failed — see `cemu_tool`,
    typically a missing/dangling keys.txt or an unsupported format).

    Returns (saves sorted by save folder path for deterministic output, warnings).
    """
    if not install.wiiu_saves_root.is_dir():
        return [], []

    matched: list[LocalSave] = []
    warnings: list[str] = []

    for rom in _wiiu_roms(roms):
        rom_path = roms_base / rom.primary_output.path
        if not rom_path.is_file():
            warnings.append(f"rom_id={rom.rom_id} ({rom.name}): ROM file not on disk at {rom_path}")
            continue

        title = lookup_wiiu_title(rom_path, tool, cache, keys_dir=install.data_dir)
        if title is None:
            warnings.append(
                f"rom_id={rom.rom_id} ({rom.name}): could not extract Wii U title ID "
                f"from {rom_path} (cemu --extract failed — check keys.txt / ROM format)"
            )
            continue

        save_folder = wiiu_save_folder(install, title)
        if not save_folder.is_dir():
            continue  # no save yet for this title; not a warning

        try:
            mtime, size = _aggregate_folder_stats(save_folder)
        except OSError as exc:
            warnings.append(f"rom_id={rom.rom_id}: could not stat {save_folder}: {exc}")
            continue
        if mtime is None:
            continue  # folder has no non-ignored files; treat as "no save yet"

        try:
            content_hash = folder_content_hash(save_folder)
        except OSError as exc:
            warnings.append(f"rom_id={rom.rom_id}: could not hash {save_folder}: {exc}")
            continue

        rom_base_name = Path(rom.primary_output.path).stem
        matched.append(
            LocalSave(
                rom_id=rom.rom_id,
                emulator=_CEMU_EMULATOR_LABEL,
                slot=rom_base_name,
                save_filename=f"{rom_base_name}.zip",
                local_path=save_folder,
                local_mtime=mtime,
                local_md5=content_hash,
                local_size=size,
            )
        )

    matched.sort(key=lambda ls: str(ls.local_path))
    return matched, warnings


def _wiiu_roms(roms: Iterable[RomState]) -> list[RomState]:
    """Return only ROMs whose resolved platform dir is `wiiu`."""
    return [rom for rom in roms if resolve_platform_dir(rom.platform_slug) == _WIIU_PLATFORM_DIR]


def _aggregate_folder_stats(folder: Path) -> tuple[float | None, int]:
    """Return (max_mtime, summed_size) over the folder's non-ignored files.

    Mirrors `wii_saves._aggregate_folder_stats`: `local_mtime` follows
    the most recently touched file so a launch that rewrites only one
    small file doesn't suppress the save's mtime signal; `local_size`
    is the summed file size, an approximation of transport size that
    classify only uses as a tiebreaker.

    `max_mtime is None` ⇒ the folder is empty after filtering — the
    walker treats this as "no save yet".
    """
    max_mtime: float | None = None
    total_size = 0
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if is_save_path_ignored(path.relative_to(folder)):
            continue
        stat = path.stat()
        total_size += stat.st_size
        if max_mtime is None or stat.st_mtime > max_mtime:
            max_mtime = stat.st_mtime
    return max_mtime, total_size
