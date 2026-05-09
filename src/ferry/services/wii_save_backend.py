"""Wii NAND save sync backend via standalone Dolphin (v3.6).

Subclass of `SaveBackendBase` (`services/save_backend_base.py`) — the
shared sync/plan/delete machinery lives there. This module supplies
Wii-specific glue:

- `WiiSaveBackend` — the four hook methods, plus the three transform
  hooks introduced in ck3 (`_pre_upload_archive`,
  `_download_io_context`, `_local_md5_from_download`) since Wii saves
  travel as zip blobs but live as folders on disk.

The walker (`adapters.dolphin.wii_saves.list_local_saves`) emits one
LocalSave per Wii title with a save folder present, with
`local_path` pointing at the folder itself and `local_md5` set to
`folder_content_hash` (matches RomM's manifest hash for the
corresponding zip — see `wii_archive` for the equivalence). The
backend's transform hooks turn that folder→zip on upload and
zip→folder on download; the base class never sees the zip's bytes
directly.

Predicate widening (ck3): the emulator tag `"dolphin"` is shared
with the GameCube backend; `_record_belongs_to_backend` filters by
`rom.platform_slug == "wii"` so each backend only owns its platform.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ferry.adapters.dolphin.dolphin_paths import DolphinInstall
from ferry.adapters.dolphin.dolphin_tool import (
    DiscHeader,
    DiscHeaderCache,
    DolphinTool,
    lookup_disc_header,
)
from ferry.adapters.dolphin.wii_archive import (
    archive_save_folder,
    extract_save_zip,
    folder_content_hash,
)
from ferry.adapters.dolphin.wii_saves import list_local_saves, wii_save_folder
from ferry.adapters.romm import RommApi
from ferry.adapters.romm.http import DownloadResult
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.save_local import LocalSave
from ferry.domain.state import LibraryState, RomState
from ferry.services.save_backend import SaveSyncResult
from ferry.services.save_backend_base import SaveBackendBase

_WII_PLATFORM_DIR = "wii"
_DOLPHIN_EMULATOR_LABEL = "dolphin"

logger = logging.getLogger(__name__)


class WiiSaveBackend(SaveBackendBase):
    """Sync standalone-Dolphin's Wii NAND saves with RomM's `/api/saves`."""

    backend_label = "Dolphin (Wii)"
    default_slot = "default"

    def __init__(
        self,
        *,
        install: DolphinInstall,
        api: RommApi,
        device_id: str,
        tool: DolphinTool,
        roms_base: Path,
        cache: DiscHeaderCache | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if install.wii_saves_root is None:
            # Caller-side guard: install resolution (ck5) is responsible
            # for filtering out installs without a verified Wii layout.
            # Construction with `wii_saves_root=None` is a programming error.
            raise ValueError(
                f"WiiSaveBackend requires install.wii_saves_root; "
                f"{install.source!r} install has no verified Wii layout"
            )
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
        # The `dolphin` emulator tag is shared with the GameCube backend;
        # disambiguate by platform so GC server records don't get routed
        # into the Wii walker / extract path.
        return (
            emulator == _DOLPHIN_EMULATOR_LABEL
            and resolve_platform_dir(rom.platform_slug) == _WII_PLATFORM_DIR
        )

    def _saves_root(self) -> Path:
        # Constructor asserts non-None; type narrowing for mypy.
        assert self._install.wii_saves_root is not None
        return self._install.wii_saves_root

    def _resolve_local_path(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        save_filename: str,
        result: SaveSyncResult | None = None,
    ) -> Path | None:
        """Canonical save folder for *rom*: `<wii_root>/<HIGH>/<LOW>/data`.

        Used by the base-class path probe (does the folder exist?) and
        by `_do_download` as the `final_dest` arg to the IO-context hook.
        Failures (missing rom file, dolphin-tool failure, header without
        title_id) route into `result.failed` and return None.
        """
        header = self._header_for_rom(rom)
        if header is None:
            if result is not None:
                result.failed.append(
                    f"download {rom.name} ({save_filename}): cannot read disc header "
                    f"(rom file missing or dolphin-tool failed)"
                )
            return None
        if header.title_id is None:
            if result is not None:
                result.failed.append(
                    f"download {rom.name} ({save_filename}): disc header has no title_id "
                    f"(is this actually a Wii ROM?)"
                )
            return None
        folder = wii_save_folder(self._install, header)
        if folder is None and result is not None:
            result.failed.append(
                f"download {rom.name} ({save_filename}): could not resolve Wii save folder"
            )
        return folder

    # ------------------------------------------------------------------
    # Transform hooks — fold a folder save into a single zip blob
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _pre_upload_archive(self, rom: RomState, local: LocalSave) -> Iterator[Path]:
        """Materialize a transient zip from the save folder for upload.

        `tempfile.TemporaryDirectory` cleans up on context exit (normal
        or exceptional). The zip's bytes aren't byte-stable across
        machines (mtimes etc.), but RomM's content_hash hashes the inner
        files — the upload's identity matches what `local_md5`
        (`folder_content_hash`) already encoded.
        """
        with tempfile.TemporaryDirectory(prefix="ferry-wii-upload-") as tmp:
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
        save folder) and the temp dir is deleted. On exception the
        temp dir is still cleaned up; the failure surfaces as a
        download error and the base class skips writing a SaveRecord
        (v3.5 server-as-arbiter contract).
        """
        with tempfile.TemporaryDirectory(prefix="ferry-wii-download-") as tmp:
            zip_path = Path(tmp) / save_filename
            yield zip_path
            # Reaching here means download_save + confirm_download both
            # succeeded. Extract synchronously; translate `BadZipFile`
            # (e.g. corrupted upload, server returned non-zip bytes) into
            # OSError so the base class's existing OSError handler catches
            # it and routes through `result.failed`. ValueError from
            # path-traversal entries also surfaces — re-raised as OSError
            # for the same reason.
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
        just-extracted folder. Both equal each other by construction
        (see `wii_archive` equivalence proof); the fallback exists
        defensively for the RomM 4.8.1 PUT-without-content_hash bug.

        Never returns `download.md5` — the byte-md5 of the zip blob
        wouldn't match what the next walker run computes (manifest hash
        on the extracted folder), and would produce spurious uploads.
        """
        del download, rom, emulator, slot  # unused in this branch
        server_content_hash = server.get("content_hash")
        if isinstance(server_content_hash, str) and server_content_hash:
            return server_content_hash
        return folder_content_hash(final_dest)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _header_for_rom(self, rom: RomState) -> DiscHeader | None:
        """Disc header for a state ROM. Cache hit if the walker ran first."""
        rom_path = self._roms_base / rom.primary_output.path
        if not rom_path.is_file():
            return None
        return lookup_disc_header(rom_path, self._tool, self._cache)
