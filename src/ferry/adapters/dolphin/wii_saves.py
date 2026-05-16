"""Walk standalone-Dolphin's Wii NAND save tree, matching titles to known ROMs.

Wii saves live as folders, not files: each title's state is the
contents of `<wii_saves_root>/<TID_HIGH>/<TID_LOW>/` — a recursive
parent containing `data/` (the actual save state — `banner.bin`,
`save.bin`, etc.), `content/` (usually empty for vanilla discs;
populated for VC titles + games with system-update content), and any
other subdirs Dolphin populates per title. ferry treats each title
parent as one `LocalSave` per Wii ROM; archive-on-upload and
extract-on-download are wired by the Wii save backend via the generic
transform hooks on `SaveBackendBase`.

For each Wii ROM in the library, ferry:

1. Reads the disc header (via cached `dolphin-tool` invocation) to get
   the 64-bit `title_id`.
2. Splits the title id into 8-hex `TID_HIGH` + `TID_LOW` and probes
   `<wii_saves_root>/<TID_HIGH>/<TID_LOW>/`. Missing or empty (no
   non-ignored files anywhere underneath) → no save yet for this
   title; skip silently.
3. Computes `LocalSave.local_md5` via `folder_content_hash`, which
   matches RomM's server-side `content_hash` for the corresponding
   zip without ever materializing one. The wrapper prefix passed to
   `folder_content_hash` matches what `archive_save_folder` will
   produce on upload (`<TID_LOW>/...`), keeping the three-way invariant
   with RomM and Argosy.

**v3.7 schema (Argosy compat):** save_filename = `<rom_base_name>.zip`,
slot = `<rom_base_name>`, emulator = `dolphin_wii`. `<rom_base_name>` is
`Path(rom.primary_output.path).stem` — matches what Argosy on the same
on-disk file would compute. Slot equals filename base because Argosy
expects symmetry AND because RomM's slot-based 409 conflict detection
only fires on truthy slots (preserving v3.5's server-as-arbiter
contract for Wii too).

`LocalSave.local_path` points at the title parent folder. The base
class's upload path will route through `_pre_upload_archive` to
materialize the zip on demand.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from ferry.adapters.dolphin.dolphin_archive import (
    folder_content_hash,
    is_save_path_ignored,
)
from ferry.adapters.dolphin.dolphin_paths import DolphinInstall
from ferry.adapters.dolphin.dolphin_tool import (
    DiscHeader,
    DiscHeaderCache,
    DolphinTool,
    lookup_disc_header,
)
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.save_local import LocalSave
from ferry.domain.state import RomState

__all__ = ("list_local_saves", "wii_save_folder")

logger = logging.getLogger(__name__)

_WII_PLATFORM_DIR = "wii"
_DOLPHIN_WII_EMULATOR_LABEL = "dolphin_wii"


def wii_save_folder(install: DolphinInstall, header: DiscHeader) -> Path | None:
    """Resolve the on-disk save folder for a Wii title.

    Returns `<wii_saves_root>/<TID_HIGH>/<TID_LOW>/` (the title parent,
    recursive — includes `data/`, `content/`, etc.), or None when
    either `install.wii_saves_root` or `header.title_id` is unavailable.
    Path may not exist on disk, or may exist but contain no save state
    yet — callers probe separately to decide "no save yet" vs.
    "supported but empty."
    """
    if install.wii_saves_root is None:
        return None
    tid_high = header.title_id_high
    tid_low = header.title_id_low
    if tid_high is None or tid_low is None:
        return None
    return install.wii_saves_root / tid_high / tid_low


def list_local_saves(
    install: DolphinInstall,
    roms: Iterable[RomState],
    *,
    roms_base: Path,
    tool: DolphinTool,
    cache: DiscHeaderCache | None = None,
) -> tuple[list[LocalSave], list[str]]:
    """Walk *install*'s NAND tree and emit one LocalSave per Wii title with saves.

    Filters `roms` to Wii only (resolved platform dir == `wii`); other
    platforms are silently ignored. Emits a warning (and skips the rom)
    when the ROM file is missing on disk, dolphin-tool fails, or the
    header lacks a `title_id` (likely a GC-disc-tagged-Wii user error).

    Returns (saves sorted by save folder path for deterministic output, warnings).
    """
    if install.wii_saves_root is None or not install.wii_saves_root.is_dir():
        return [], []

    matched: list[LocalSave] = []
    warnings: list[str] = []

    for rom in _wii_roms(roms):
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

        if header.title_id is None:
            warnings.append(
                f"rom_id={rom.rom_id} ({rom.name}): disc header has no title_id "
                f"(is this actually a Wii ROM?)"
            )
            continue

        save_folder = wii_save_folder(install, header)
        if save_folder is None or not save_folder.is_dir():
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
                emulator=_DOLPHIN_WII_EMULATOR_LABEL,
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


def _wii_roms(roms: Iterable[RomState]) -> list[RomState]:
    """Return only ROMs whose resolved platform dir is `wii`."""
    return [rom for rom in roms if resolve_platform_dir(rom.platform_slug) == _WII_PLATFORM_DIR]


def _aggregate_folder_stats(folder: Path) -> tuple[float | None, int]:
    """Return (max_mtime, summed_size) over the folder's non-ignored files.

    `local_mtime` follows the most recently touched file — Wii games
    that update e.g. only `banner.bin` on launch shouldn't suppress
    the rest of the save's mtime signal. `local_size` is the sum of
    all bundled file sizes, an approximation of "transport size";
    classify only uses size as a tiebreaker against the server's
    `file_size_bytes` (the zip's size), so an exact match isn't
    required.

    `max_mtime is None` ⇒ the folder is empty after filtering — the
    walker treats this as "no save yet" rather than emitting a
    LocalSave with no underlying state.
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
