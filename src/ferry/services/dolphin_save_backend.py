"""Dolphin GameCube save sync backend (v3).

Subclass of `SaveBackendBase` (`services/save_backend_base.py`) — the
shared sync/plan/delete machinery lives there. This module supplies
Dolphin-specific glue:

- `DolphinSaveBackend` — the four hook methods plus disc-header
  resolution for download path computation.

The walker (`adapters.dolphin_saves.list_local_saves`) and the
disc-header adapter (`adapters.dolphin_tool`) handle the
Dolphin-specific I/O; this class just wires them into the base's
sync loop and adds the region-folder mapping for downloads.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ferry.adapters.dolphin_paths import DolphinInstall
from ferry.adapters.dolphin_saves import (
    list_local_saves,
    lookup_disc_header,
    resolve_save_path,
)
from ferry.adapters.dolphin_tool import DiscHeader, DiscHeaderCache, DolphinTool
from ferry.adapters.romm import RommApi
from ferry.domain.save_local import LocalSave
from ferry.domain.state import LibraryState, RomState
from ferry.services.save_backend import SaveSyncResult
from ferry.services.save_backend_base import SaveBackendBase

logger = logging.getLogger(__name__)


class DolphinSaveBackend(SaveBackendBase):
    """Sync standalone-Dolphin's GCI Folder saves with RomM's `/api/saves`."""

    backend_label = "Dolphin"
    default_slot = "default"  # unused: Dolphin always sets a real slot

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

    def _emulator_matches(self, emulator: str) -> bool:
        return emulator == "dolphin"

    def _saves_root(self) -> Path:
        return self._install.saves_root

    def _resolve_local_path(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        save_filename: str,
        result: SaveSyncResult | None = None,
    ) -> Path | None:
        header = self._header_for_rom(rom)
        if header is None:
            if result is not None:
                result.failed.append(
                    f"download {rom.name} ({save_filename}): cannot read disc header "
                    f"(rom file missing or dolphin-tool failed)"
                )
            return None
        dest = resolve_save_path(self._install, header.region, save_filename)
        if dest is None and result is not None:
            result.failed.append(
                f"download {rom.name} ({save_filename}): unsupported region {header.region!r}"
            )
        return dest

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _header_for_rom(self, rom: RomState) -> DiscHeader | None:
        """Disc header for a state ROM. Cache hit if the walker ran first."""
        rom_path = self._roms_base / rom.primary_output.path
        if not rom_path.is_file():
            return None
        return lookup_disc_header(rom_path, self._tool, self._cache)
