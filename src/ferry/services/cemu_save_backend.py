"""Cemu Wii U save sync backend (v5).

Subclass of `SaveBackendBase` (`services/save_backend_base.py`) — the
shared sync/plan/delete machinery lives there. This module supplies
Cemu-specific glue:

- `CemuSaveBackend` — the four hook methods, plus the three transform
  hooks (`_pre_upload_archive`, `_download_io_context`,
  `_local_md5_from_download`) since Wii U saves travel as zip blobs
  but live as folders on disk.

Structurally this is the Wii NAND backend (`wii_save_backend`) with
`cemu_tool` swapped in for `dolphin_tool`: Wii U save identity comes
from `cemu --extract` rather than a disc-header read, but the
on-disk save is still a per-title folder, so the folder↔zip transform
hooks and the `dolphin_archive` helpers are reused verbatim.

The walker (`adapters.cemu.wiiu_saves.list_local_saves`) emits one
LocalSave per Wii U title with a save folder present, `local_path`
pointing at the `<TITLE_HIGH>/<TITLE_LOW>/` folder and `local_md5`
set to `folder_content_hash` (matches RomM's manifest hash for the
corresponding zip — and Argosy's for the same folder).

**Argosy compat:** emulator tag is `cemu` (Argosy's `SavePathRegistry`
tag); filename + slot are both `<rom_base_name>`. `_record_belongs_to_
backend` cross-checks `platform_slug == "wiiu"` defensively — the
`cemu` tag is already unique among ferry's backends, but the check
costs nothing and catches future taxonomy drift.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ferry.adapters.cemu.cemu_paths import CemuInstall
from ferry.adapters.cemu.cemu_tool import CemuTool, WiiUTitle, WiiUTitleCache, lookup_wiiu_title
from ferry.adapters.cemu.wiiu_saves import list_local_saves, wiiu_save_folder
from ferry.adapters.dolphin.dolphin_archive import (
    archive_save_folder,
    extract_save_zip,
    folder_content_hash,
)
from ferry.adapters.romm import RommApi
from ferry.adapters.romm.http import DownloadResult
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.save_local import LocalSave
from ferry.domain.state import LibraryState, RomState
from ferry.services.save_backend import SaveSyncResult
from ferry.services.save_backend_base import SaveBackendBase

_WIIU_PLATFORM_DIR = "wiiu"
_CEMU_EMULATOR_LABEL = "cemu"

logger = logging.getLogger(__name__)


class CemuSaveBackend(SaveBackendBase):
    """Sync Cemu's Wii U saves with RomM's `/api/saves`."""

    backend_label = "Cemu (Wii U)"
    default_slot = "default"  # unused: walker always sets a real slot (<rom_base_name>)

    def __init__(
        self,
        *,
        install: CemuInstall,
        api: RommApi,
        device_id: str,
        tool: CemuTool,
        roms_base: Path,
        cache: WiiUTitleCache | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        super().__init__(api=api, device_id=device_id, log=log)
        self._install = install
        self._tool = tool
        self._cache = cache
        self._roms_base = roms_base

    # ------------------------------------------------------------------
    # SaveBackendBase hooks
    # ------------------------------------------------------------------

    def _walk_local(self, state: LibraryState) -> tuple[list[LocalSave], list[str]]:
        return list_local_saves(
            self._install,
            state.roms.values(),
            roms_base=self._roms_base,
            tool=self._tool,
            cache=self._cache,
        )

    def _record_belongs_to_backend(self, rom: RomState, emulator: str) -> bool:
        return (
            emulator == _CEMU_EMULATOR_LABEL
            and resolve_platform_dir(rom.platform_slug) == _WIIU_PLATFORM_DIR
        )

    def _saves_root(self) -> Path:
        return self._install.wiiu_saves_root

    def _resolve_local_path(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        save_filename: str,
        result: SaveSyncResult | None = None,
    ) -> Path | None:
        """Canonical save folder for *rom*: `<wiiu_saves_root>/<HIGH>/<LOW>/`.

        Per-title folder — includes `user/` (per-account save state)
        and `meta/`. Used by the base-class path probe and by
        `_do_download` as the `final_dest` for the IO-context hook.
        Failure to resolve the title ID (rom file missing, keys.txt
        unavailable, unsupported ROM format) routes into `result.failed`
        and returns None.
        """
        title = self._title_for_rom(rom)
        if title is None:
            if result is not None:
                result.failed.append(
                    f"download {rom.name} ({save_filename}): cannot extract Wii U title ID "
                    f"(rom file missing, keys.txt unavailable, or unsupported format)"
                )
            return None
        return wiiu_save_folder(self._install, title)

    # ------------------------------------------------------------------
    # Transform hooks — fold a folder save into a single zip blob
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _pre_upload_archive(self, rom: RomState, local: LocalSave) -> Iterator[Path]:
        """Materialize a transient zip from the save folder for upload.

        `tempfile.TemporaryDirectory` cleans up on context exit. The
        zip's bytes aren't byte-stable across machines, but RomM's
        content_hash hashes the inner files — the upload's identity
        matches what `local_md5` (`folder_content_hash`) encoded.
        """
        with tempfile.TemporaryDirectory(prefix="ferry-cemu-upload-") as tmp:
            zip_path = Path(tmp) / local.save_filename
            archive_save_folder(local.local_path, zip_path)
            yield zip_path

    @contextlib.contextmanager
    def _download_io_context(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        save_filename: str,
        final_dest: Path,
    ) -> Iterator[Path]:
        """Receive bytes as a zip in a temp dir; extract into final_dest on exit.

        On normal exit the zip is extracted into `final_dest` (the
        per-title save folder). `BadZipFile` / path-traversal
        `ValueError` are translated to `OSError` so the base class's
        OSError handler routes them through `result.failed` and skips
        writing a SaveRecord (v3.5 server-as-arbiter contract).
        """
        with tempfile.TemporaryDirectory(prefix="ferry-cemu-download-") as tmp:
            zip_path = Path(tmp) / save_filename
            yield zip_path
            try:
                extract_save_zip(zip_path, final_dest)
            except zipfile.BadZipFile as exc:
                raise OSError(f"corrupt zip from {save_filename}: {exc}") from exc
            except ValueError as exc:
                raise OSError(f"unsafe zip from {save_filename}: {exc}") from exc

    def _local_md5_from_download(
        self,
        *,
        download: DownloadResult,
        server: dict[str, Any],
        rom: RomState,
        emulator: str,
        slot: str,
        final_dest: Path,
    ) -> str:
        """Use `server.content_hash` (manifest hash for the zip) when
        available; otherwise recompute `folder_content_hash` from the
        just-extracted folder. Both are equal by construction; the
        fallback covers the RomM 4.8.1 PUT-without-content_hash bug.

        Never returns `download.md5` — the byte-md5 of the zip blob
        wouldn't match what the next walker run computes, producing
        spurious uploads.
        """
        del download, rom, emulator, slot  # unused in this branch
        server_content_hash = server.get("content_hash")
        if isinstance(server_content_hash, str) and server_content_hash:
            return server_content_hash
        return folder_content_hash(final_dest)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _title_for_rom(self, rom: RomState) -> WiiUTitle | None:
        """Wii U title ID for a state ROM. Cache hit if the walker ran first."""
        rom_path = self._roms_base / rom.primary_output.path
        if not rom_path.is_file():
            return None
        return lookup_wiiu_title(rom_path, self._tool, self._cache, keys_dir=self._install.data_dir)
