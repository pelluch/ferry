"""RetroArch save sync backend (v2).

Subclass of `SaveBackendBase` (`services/save_backend_base.py`) — the
shared sync/plan/delete machinery lives there. This module supplies
RetroArch-specific glue:

- `RetroArchSaveBackend` — the four hook methods + `_resolve_local_path`
  that maps a save to its on-disk location based on the install's
  `sort_savefiles_*` flags.
- `_resolve_local_path_for` — pure helper for the above; mirrors the
  walker's emulator-from-layout logic in reverse so downloads land where
  RetroArch will read them.

`get_or_register_device` (the device registration handshake) lives
here — it's backend-neutral and used by the CLI before constructing
any backend. `SaveSyncResult` and the `SaveBackend` Protocol live in
`save_backend_base`; both are re-exported from this module for
existing-import compatibility.

Algorithm details (the `.sync()` loop, conflict resolution, etc.) and
the `SaveBackend` Protocol are documented in `save_backend_base.py`.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ferry import __version__
from ferry.adapters.retroarch.retroarch_core_info import CoreInfoIndex
from ferry.adapters.retroarch.retroarch_paths import RetroArchInstall
from ferry.adapters.retroarch.retroarch_saves import list_local_saves
from ferry.adapters.romm import RommApi, RommApiError
from ferry.domain.save_local import LocalSave  # noqa: F401 — re-exported for tests
from ferry.domain.state import LibraryState, RomState
from ferry.services.save_backend_base import (
    SaveBackend,
    SaveBackendBase,
    SaveSyncResult,
    index_prior_records,
    index_server_saves,
)

# Re-export so existing `from ferry.services.save_backend import ...` keeps
# working. The canonical homes are `save_backend_base` (Protocol +
# SaveSyncResult) and the per-backend modules.
__all__ = (
    "RetroArchSaveBackend",
    "SaveBackend",
    "SaveSyncResult",
    "get_or_register_device",
)

logger = logging.getLogger(__name__)


def get_or_register_device(
    api: RommApi,
    state: LibraryState,
    *,
    hostname: str | None = None,
) -> tuple[str, LibraryState]:
    """Return (device_id, possibly-updated state).

    If `state.device_id` is set, return it unchanged. Otherwise, register
    this client with RomM. Registration is idempotent server-side via
    fingerprint (mac/hostname/platform), so even a wiped state.json
    re-registers to the same device record. The freshly-discovered
    device_id is cached into a new LibraryState that the caller
    persists.

    `hostname` defaults to `socket.gethostname()`; injectable for tests.
    """
    if state.device_id is not None:
        return state.device_id, state

    name = hostname or socket.gethostname() or "ferry-client"
    response = api.register_device(
        name=name,
        platform="linux",
        client="ferry",
        client_version=__version__,
        hostname=name,
    )
    device_id = response.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise RommApiError(
            f"register_device response missing device_id: {response}",
            url="/api/devices",
            method="POST",
        )
    new_state = LibraryState(
        schema_version=state.schema_version,
        last_updated_after=state.last_updated_after,
        roms=state.roms,
        device_id=device_id,
    )
    return device_id, new_state


class RetroArchSaveBackend(SaveBackendBase):
    """Sync the local RetroArch saves dir with RomM's `/api/saves`."""

    backend_label = "RetroArch"
    default_slot = "default"

    def __init__(
        self,
        *,
        install: RetroArchInstall,
        api: RommApi,
        device_id: str,
        log: logging.Logger | None = None,
    ) -> None:
        super().__init__(api=api, device_id=device_id, log=log)
        self._install = install
        self._core_info = CoreInfoIndex(install)

    # ------------------------------------------------------------------
    # SaveBackendBase hooks
    # ------------------------------------------------------------------

    def _walk_local(self, state: LibraryState) -> tuple[list[LocalSave], list[str]]:
        return list_local_saves(self._install, state.roms.values(), core_info=self._core_info)

    def _emulator_matches(self, emulator: str) -> bool:
        return emulator.startswith("retroarch")

    def _saves_root(self) -> Path:
        return self._install.savefile_directory

    def _resolve_local_path(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        save_filename: str,
        result: SaveSyncResult | None = None,
    ) -> Path | None:
        dest = _resolve_local_path_for(self._install, rom, emulator, save_filename, self._core_info)
        if dest is None and result is not None:
            result.failed.append(
                f"download {rom.name} ({save_filename}): cannot determine local path "
                f"(emulator={emulator!r}; check sort_savefiles_* settings)"
            )
        return dest


def _resolve_local_path_for(
    install: RetroArchInstall,
    rom: RomState,
    emulator: str,
    save_filename: str,
    core_info: CoreInfoIndex | None = None,
) -> Path | None:
    """Mirror of the walker's emulator-from-layout in reverse.

    Compute where on disk RetroArch expects this save based on the
    install's sort_* settings + the save's emulator label. Returns None
    when we lack the information (e.g., sort_by_core=true but emulator
    label is plain `retroarch` so we don't know which core subdir).

    `core_info` resolves the lowercase `core_so_prefix` (e.g., `snes9x`)
    from the emulator label to RetroArch's actual `corename` directory
    (e.g., `Snes9x`) — without this, the resolved path's casing won't
    match what RetroArch creates and saves end up in parallel dirs.
    Identity fallback when the index isn't available or the core isn't
    known.
    """
    base = install.savefile_directory
    sort_by_core = install.sort_savefiles_enable
    sort_by_content = install.sort_savefiles_by_content_enable
    core_prefix: str | None = None
    if emulator.startswith("retroarch-"):
        core_prefix = emulator[len("retroarch-") :]
    core_dir = core_info.forward(core_prefix) if (core_prefix and core_info) else core_prefix

    if sort_by_core and sort_by_content:
        if core_dir is None:
            return None
        return base / rom.platform_slug / core_dir / save_filename
    if sort_by_core and not sort_by_content:
        if core_dir is None:
            return None
        return base / core_dir / save_filename
    if sort_by_content and not sort_by_core:
        return base / rom.platform_slug / save_filename
    return base / save_filename


# ---------------------------------------------------------------------------
# Backward-compat re-exports for tests / downstream imports
# ---------------------------------------------------------------------------
# The indexer functions used to live in this module under private names; tests
# and adjacent code import them from here. The implementations now live in
# `save_backend_base` (parameterized by emulator predicate); preserve the old
# names with retroarch-specific predicates baked in.


def _index_server_saves(
    server_saves: Iterable[dict[str, Any]],
) -> dict[tuple[int, str, str], dict[str, Any]]:
    return index_server_saves(
        server_saves,
        emulator_matches=lambda e: e.startswith("retroarch"),
        default_slot="default",
    )


def _index_prior_records(
    roms: Iterable[RomState],
) -> dict[tuple[int, str, str], RomState]:
    # Type return is dict[..., SaveRecord] in practice; preserved for
    # call-site compatibility with the old private name.
    return index_prior_records(  # type: ignore[return-value]
        roms, emulator_matches=lambda e: e.startswith("retroarch")
    )
