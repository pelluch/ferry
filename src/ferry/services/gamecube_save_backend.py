"""Dolphin GameCube save sync backend (v3.7 ck2 — per-rom bundle schema).

Subclass of `SaveBackendBase` (`services/save_backend_base.py`) — the
shared sync/plan/delete machinery lives there. This module supplies
GameCube-specific glue:

- `GameCubeSaveBackend` — the four hook methods plus the three
  transform hooks (`_pre_upload_archive`, `_download_io_context`,
  `_local_md5_from_download`) since GC saves travel as a wrapper-
  prefixed zip blob but live as scattered `.gci` files on disk.

The walker (`adapters.dolphin.gamecube_saves.list_local_saves`) emits
one LocalSave per GC ROM with at least one matching `.gci`, with
`local_path` set to `<saves_root>` (sentinel; real GCI list is
recomputed at upload time via `match_rom_gcis`) and `local_md5` set
to `files_content_hash(matched_gcis, wrapper=<rom_base_name>)`.

**v3.7 Argosy compat (ck7.2):** filename + slot = `<rom_base_name>` /
`<rom_base_name>.zip`; emulator tag stays `dolphin` (unchanged from
v3.6). `_record_belongs_to_backend` still cross-checks
`platform_slug == "gc"` defensively — Wii records carry the new
`dolphin_wii` tag (ck7.1) so collision is no longer possible, but
costs nothing and catches future taxonomy drift cheaply.
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
from ferry.adapters.dolphin.gamecube_saves import (
    list_local_saves,
    match_rom_gcis,
    region_card_dir,
)
from ferry.adapters.dolphin.wii_archive import (
    archive_files,
    extract_save_zip,
    files_content_hash,
)
from ferry.adapters.romm import RommApi
from ferry.adapters.romm.http import DownloadResult
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.save_local import LocalSave
from ferry.domain.state import LibraryState, RomState
from ferry.services.save_backend import SaveSyncResult
from ferry.services.save_backend_base import SaveBackendBase

_GAMECUBE_PLATFORM_DIR = "gc"
_DOLPHIN_EMULATOR_LABEL = "dolphin"

logger = logging.getLogger(__name__)


class GameCubeSaveBackend(SaveBackendBase):
    """Sync standalone-Dolphin's GCI Folder saves with RomM's `/api/saves`."""

    backend_label = "Dolphin (GameCube)"
    default_slot = "default"  # unused: walker always sets a real slot (<rom_base_name>)

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

    def _record_belongs_to_backend(self, rom: RomState, emulator: str) -> bool:
        # As of v3.7 ck1, Wii records use `dolphin_wii`; only GC carries
        # the bare `dolphin` tag. The platform check is defensive belt-
        # and-suspenders against future tag overloading.
        return (
            emulator == _DOLPHIN_EMULATOR_LABEL
            and resolve_platform_dir(rom.platform_slug) == _GAMECUBE_PLATFORM_DIR
        )

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
        """Card A directory for *rom*'s region: `<saves_root>/<region>/Card A`.

        Used by the base-class path probe and as `final_dest` for the
        download IO context. v3.7 always restores extracted GCIs to
        Card A — bundles don't carry per-GCI card-source metadata so
        Card B becomes effectively read-only on download.
        """
        header = self._header_for_rom(rom)
        if header is None:
            if result is not None:
                result.failed.append(
                    f"download {rom.name} ({save_filename}): cannot read disc header "
                    f"(rom file missing or dolphin-tool failed)"
                )
            return None
        dest = region_card_dir(self._install, header.region)
        if dest is None and result is not None:
            result.failed.append(
                f"download {rom.name} ({save_filename}): unsupported region {header.region!r}"
            )
        return dest

    # ------------------------------------------------------------------
    # Transform hooks — bundle GCIs into one zip on upload, fan out on download
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _pre_upload_archive(self, rom: RomState, local: LocalSave) -> Iterator[Path]:
        """Materialize a transient bundle zip from the rom's matched GCIs.

        Re-runs `match_rom_gcis` against the current disk state — same
        matcher the walker used, so the upload's content_hash matches
        what `local.local_md5` already encoded (any drift would
        manifest as a benign re-upload on the next sync rather than
        a stale-bytes upload now). `tempfile.TemporaryDirectory`
        cleans up on context exit.

        OSError / failure to read the disc header surfaces as OSError
        out of this context — the base class's existing OSError handler
        catches it and routes through `result.failed`.
        """
        header = self._header_for_rom(rom)
        if header is None:
            raise OSError(f"cannot read disc header for rom_id={rom.rom_id} ({rom.name})")
        gci_paths, warnings = match_rom_gcis(self._install, header, rom=rom)
        for w in warnings:
            self._logger.warning(w)
        if not gci_paths:
            raise OSError(
                f"no GCI files matched for rom_id={rom.rom_id} ({rom.name}); "
                f"local saves may have been removed since the walker ran"
            )
        rom_base_name = Path(rom.primary_output.path).stem
        with tempfile.TemporaryDirectory(prefix="ferry-gc-upload-") as tmp:
            zip_path = Path(tmp) / local.save_filename
            archive_files(gci_paths, zip_path, wrapper=rom_base_name)
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
        """Receive bytes as a zip in a temp dir; extract GCIs into final_dest on exit.

        `final_dest` is Card A for the rom's region (per
        `_resolve_local_path`). All extracted GCIs land flat there,
        regardless of which card they originated on — bundles don't
        carry card-source metadata. `extract_save_zip` strips the
        wrapper directory unconditionally (Argosy parity).

        `BadZipFile` and `ValueError` (path-traversal) are translated
        to `OSError` so the base class's OSError handler catches them
        and routes through `result.failed`. Same pattern as the Wii
        backend.
        """
        with tempfile.TemporaryDirectory(prefix="ferry-gc-download-") as tmp:
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
        """Use `server.content_hash` (manifest hash) when available;
        otherwise recompute `files_content_hash` from the freshly-
        extracted GCIs in Card A. Both are equal by construction (the
        three-way invariant with RomM and Argosy); the fallback exists
        defensively for the RomM 4.8.1 PUT-without-content_hash bug.

        Never returns `download.md5` — the byte-md5 of the zip blob
        wouldn't match what the next walker run computes (manifest hash
        on the matched-GCI set), and would produce spurious uploads.
        """
        del download  # unused in this branch
        server_content_hash = server.get("content_hash")
        if isinstance(server_content_hash, str) and server_content_hash:
            return server_content_hash
        # Re-match from disk — same matcher the next walker run will use.
        header = self._header_for_rom(rom)
        if header is None:
            # Can't recompute without the header; pre-upload would have
            # failed earlier in the same flow if this branch fired.
            # Empty-string sentinel → next sync's classify will treat
            # as "hash unknown" and fall through to size compare.
            return ""
        gci_paths, _ = match_rom_gcis(self._install, header, rom=rom)
        rom_base_name = Path(rom.primary_output.path).stem
        return files_content_hash(gci_paths, wrapper=rom_base_name)

    # ------------------------------------------------------------------
    # delete_for_rom — override: walk-emitted local_path is the saves_root
    # sentinel, not the actual GCI list. Re-match to know what to trash.
    # ------------------------------------------------------------------

    def delete_for_rom(self, rom: RomState, trash_dir: Path) -> tuple[int, list[str]]:
        """Move every matched .gci for *rom* across both cards into the trash.

        Overrides the base default because v3.7's `LocalSave.local_path`
        is `<saves_root>` (sentinel) — letting the base implementation
        run would move the entire saves_root tree. We re-run
        `match_rom_gcis` to get the real per-rom GCI list and move each
        one individually. Trash relpath layout matches the base class
        (`<trash>/saves/<region>/Card?/<gci_filename>`).
        """
        header = self._header_for_rom(rom)
        if header is None:
            return 0, [
                f"rom_id={rom.rom_id} ({rom.name}): cannot read disc header — no GCIs trashed"
            ]
        gci_paths, warnings = match_rom_gcis(self._install, header, rom=rom)
        if not gci_paths:
            return 0, warnings

        saves_root = self._saves_root()
        saves_subdir = trash_dir / "saves"
        saves_subdir.mkdir(parents=True, exist_ok=True)
        count = 0
        for path in gci_paths:
            try:
                rel = path.relative_to(saves_root)
            except ValueError:
                rel = Path(path.name)
            dst = saves_subdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.replace(dst)
                count += 1
            except OSError as exc:
                warnings.append(f"could not move {path}: {exc}")
        return count, warnings

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _header_for_rom(self, rom: RomState) -> DiscHeader | None:
        """Disc header for a state ROM. Cache hit if the walker ran first."""
        rom_path = self._roms_base / rom.primary_output.path
        if not rom_path.is_file():
            return None
        return lookup_disc_header(rom_path, self._tool, self._cache)
