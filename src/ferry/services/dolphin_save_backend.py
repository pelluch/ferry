"""Dolphin GameCube save sync backend (v3).

Mirrors `services/save_backend.RetroArchSaveBackend` in shape and intent.
Glues together:

- `adapters.dolphin_paths.DolphinInstall` — where Dolphin lives + its
  region encoding.
- `adapters.dolphin_tool` — disc-header reads via dolphin-tool, with
  on-disk caching.
- `adapters.dolphin_saves` — local saves walker producing `LocalSave`
  records.
- `adapters.romm.RommApi` — `/api/saves` endpoints (shared with v2).
- `domain.save_conflicts` — newest-wins / determine-action / etc.
  (shared with v2; backend-neutral).

Algorithm (per `.sync()` pass) — same as v2:
  1. Walk local saves once (also populates the disc-header cache for
     every GC ROM in state).
  2. Bulk-fetch every server save in one `/api/saves` GET.
  3. Filter local + server + prior-sync records to `emulator == "dolphin"`
     so this backend doesn't trample on RetroArch saves managed by
     `RetroArchSaveBackend`.
  4. Per-key dispatch via `domain.save_conflicts` primitives:
     local-only → upload; server-only → download; both with no prior
     → newest-wins; both with prior → diff + newest-wins on conflict.
  5. On success, persist updated SaveRecord into rom's `saves` tuple.

`SaveSyncResult` is imported from v2's module — the result shape is
identical and shared. `get_or_register_device` is also reused unchanged
(emulator-agnostic).

`delete_for_rom` is the executor-integration hook: when ferry trashes
a ROM, it asks each save backend to trash the rom's local saves into
the per-ROM trash dir. Server-side saves are NOT deleted (kept as a
backup).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ferry.adapters.dolphin_paths import DolphinInstall
from ferry.adapters.dolphin_saves import (
    LocalSave,
    list_local_saves,
    lookup_disc_header,
    resolve_save_path,
)
from ferry.adapters.dolphin_tool import DiscHeader, DiscHeaderCache, DolphinTool
from ferry.adapters.romm import RommApi, RommApiError
from ferry.domain.save_conflicts import (
    determine_action,
    local_changed,
    resolve_newest,
    server_changed_fast,
)
from ferry.domain.state import LibraryState, RomState, SaveRecord
from ferry.services.save_backend import SaveSyncResult

logger = logging.getLogger(__name__)

_DOLPHIN_EMULATOR_LABEL = "dolphin"

# Per-key action labels emitted internally for clarity.
_Action = Literal["upload", "download", "skip", "ambiguous", "drop_prior"]

# Same datetime-tag pattern v2 uses — RomM's `_apply_datetime_tag` in
# `backend/endpoints/saves.py` appends ` [YYYY-MM-DD_HH-MM-SS]` to every
# upload's filename. We strip on download so Dolphin reads the file at
# `<MAKER>-<CODE>-<m_filename>.gci` (no tag), and on the SaveRecord we
# persist locally so re-syncs match the on-disk filename.
_DATETIME_TAG_PATTERN = re.compile(r" \[\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\]")


def _strip_datetime_tag(filename: str) -> str:
    """`Mario [2026-04-24_15-51-34].srm` → `Mario.srm`."""
    name, ext = os.path.splitext(filename)
    return _DATETIME_TAG_PATTERN.sub("", name) + ext


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class DolphinSaveBackend:
    """Sync standalone-Dolphin's GCI Folder saves with RomM's `/api/saves`.

    Stateless across `.sync()` calls — all persistent state lives in the
    passed-in `LibraryState`. Returns a `SaveSyncResult` carrying the
    counters plus updated rom records; the caller persists.
    """

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
        self._install = install
        self._api = api
        self._device_id = device_id
        self._tool = tool
        self._cache = cache
        self._roms_base = roms_base
        self._logger = log or logger

    # ------------------------------------------------------------------
    # Trash hook (executor integration; checkpoint 5 will wire this up)
    # ------------------------------------------------------------------

    def delete_for_rom(self, rom: RomState, trash_dir: Path) -> tuple[int, list[str]]:
        """Move every local Dolphin save for *rom* into the rom's trash dir.

        Server-side saves are not deleted — they remain on RomM as a
        backup. Mirror of `RetroArchSaveBackend.delete_for_rom`'s
        contract: returns (count_trashed, warnings); the executor
        already created `trash_dir` for the ROM file itself, we drop
        saves into a `saves/` subdir there.
        """
        local_saves, walker_warnings = list_local_saves(
            self._install,
            [rom],
            roms_base=self._roms_base,
            tool=self._tool,
            cache=self._cache,
        )
        if not local_saves:
            return 0, walker_warnings

        saves_subdir = trash_dir / "saves"
        saves_subdir.mkdir(parents=True, exist_ok=True)
        count = 0
        warnings = list(walker_warnings)
        for ls in local_saves:
            try:
                rel = ls.local_path.relative_to(self._install.saves_root)
            except ValueError:
                rel = Path(ls.save_filename)
            dst = saves_subdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                ls.local_path.replace(dst)
                count += 1
            except OSError as exc:
                warnings.append(f"could not move {ls.local_path}: {exc}")
        return count, warnings

    # ------------------------------------------------------------------
    # Sync pass
    # ------------------------------------------------------------------

    def sync(self, state: LibraryState) -> SaveSyncResult:
        """Run a full save-sync pass against `state.roms`."""
        local_saves, walker_warnings = list_local_saves(
            self._install,
            state.roms.values(),
            roms_base=self._roms_base,
            tool=self._tool,
            cache=self._cache,
        )

        try:
            server_saves = self._api.list_saves(device_id=self._device_id)
        except RommApiError as exc:
            return SaveSyncResult(
                failed=[f"could not list server saves: {exc}"],
                warnings=walker_warnings,
            )

        local_by_key = {(ls.rom_id, ls.emulator, ls.slot): ls for ls in local_saves}
        server_by_key = _index_dolphin_server_saves(server_saves)
        prev_by_key = _index_dolphin_prior_records(state.roms.values())

        all_keys = set(local_by_key) | set(server_by_key) | set(prev_by_key)

        result = SaveSyncResult(warnings=list(walker_warnings))
        rom_save_updates: dict[int, dict[tuple[str, str], SaveRecord | None]] = {}

        for key in sorted(all_keys):
            rom_id, emulator, slot = key
            rom = state.roms.get(rom_id)
            if rom is None:
                # Server has saves for a ROM not in our state — happens when
                # ROMs have been removed locally but saves linger server-side.
                continue
            local = local_by_key.get(key)
            server = server_by_key.get(key)
            prev = prev_by_key.get(key)

            should_update, outcome = self._process_key(
                rom, emulator, slot, local, server, prev, result
            )
            if should_update:
                rom_save_updates.setdefault(rom_id, {})[(emulator, slot)] = outcome

        for rom_id, updates in rom_save_updates.items():
            rom = state.roms[rom_id]
            new_saves = _merge_save_records(rom.saves, updates)
            result.updated_roms[rom_id] = RomState(**{**_rom_as_kwargs(rom), "saves": new_saves})

        return result

    # ------------------------------------------------------------------
    # Per-key dispatch
    # ------------------------------------------------------------------

    def _process_key(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        local: LocalSave | None,
        server: dict[str, Any] | None,
        prev: SaveRecord | None,
        result: SaveSyncResult,
    ) -> tuple[bool, SaveRecord | None]:
        action = self._decide_action(
            local, server, prev, result, rom_id=rom.rom_id, emulator=emulator, slot=slot
        )
        if action == "skip":
            result.skipped += 1
            return False, None
        if action == "ambiguous":
            return False, None
        if action == "drop_prior":
            return True, None
        if action == "upload":
            assert local is not None
            outcome = self._do_upload(rom, local, prev, server, result)
            return (outcome is not None, outcome)
        if action == "download":
            assert server is not None
            outcome = self._do_download(rom, emulator, slot, server, result)
            return (outcome is not None, outcome)
        return False, None  # defensive

    def _decide_action(
        self,
        local: LocalSave | None,
        server: dict[str, Any] | None,
        prev: SaveRecord | None,
        result: SaveSyncResult,
        *,
        rom_id: int,
        emulator: str,
        slot: str,
    ) -> _Action:
        """Pure decision logic — no I/O."""
        if local is None and server is None:
            return "drop_prior" if prev is not None else "skip"
        if local is None and server is not None:
            return "download"
        if local is not None and server is None:
            return "upload"

        assert local is not None and server is not None
        local_md5 = local.local_md5
        server_md5 = server.get("content_hash") or ""
        server_size = server.get("file_size_bytes")
        server_updated_at = server.get("updated_at") or ""

        if prev is None:
            resolution = resolve_newest(
                local_mtime=local.local_mtime,
                server_updated_at=server_updated_at,
            )
            if resolution == "ambiguous":
                _flag_ambiguous(
                    result, rom_id, local.save_filename, "first sync — within tolerance"
                )
                return "ambiguous"
            if local_md5 == server_md5:
                return "skip"
            result.conflicts_resolved += 1
            return "upload" if resolution == "upload" else "download"

        l_changed = local_changed(local_md5, prev.last_sync_md5)
        s_changed = server_changed_fast(
            stored_updated_at=prev.last_sync_server_updated_at,
            stored_size=prev.last_sync_server_size,
            server_updated_at=server_updated_at,
            server_size=server_size,
        )
        if s_changed is None:
            s_changed = server_md5 != prev.last_sync_md5
        action = determine_action(local_changed_=l_changed, server_changed=s_changed)
        if action != "conflict":
            return action  # type: ignore[return-value]

        resolution = resolve_newest(
            local_mtime=local.local_mtime,
            server_updated_at=server_updated_at,
        )
        if resolution == "ambiguous":
            _flag_ambiguous(result, rom_id, local.save_filename, "conflict within tolerance")
            return "ambiguous"
        result.conflicts_resolved += 1
        return "upload" if resolution == "upload" else "download"

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _do_upload(
        self,
        rom: RomState,
        local: LocalSave,
        prev: SaveRecord | None,
        server: dict[str, Any] | None,
        result: SaveSyncResult,
    ) -> SaveRecord | None:
        save_id = (server or {}).get("id") or (prev.server_save_id if prev else None)
        try:
            response = self._api.upload_save(
                rom.rom_id,
                local.local_path,
                emulator=local.emulator,
                save_id=save_id,
                device_id=self._device_id,
                slot=local.slot,
                overwrite=True,
            )
        except RommApiError as exc:
            result.failed.append(f"upload {rom.name} ({local.save_filename}): {exc}")
            return prev
        result.uploaded += 1
        return _save_record_from_server(response, local_md5=local.local_md5)

    def _do_download(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        server: dict[str, Any],
        result: SaveSyncResult,
    ) -> SaveRecord | None:
        save_id = server.get("id")
        server_filename = server.get("file_name")
        if not isinstance(save_id, int) or not isinstance(server_filename, str):
            result.failed.append(
                f"download {rom.name} (rom_id={rom.rom_id}, {emulator}/{slot}): "
                f"server response missing id or file_name"
            )
            return None

        local_filename = _strip_datetime_tag(server_filename)
        header = self._header_for_rom(rom)
        if header is None:
            result.failed.append(
                f"download {rom.name} ({local_filename}): cannot read disc header "
                f"(rom file missing or dolphin-tool failed)"
            )
            return None
        dest = resolve_save_path(self._install, header.region, local_filename)
        if dest is None:
            result.failed.append(
                f"download {rom.name} ({local_filename}): unsupported region {header.region!r}"
            )
            return None

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            download = self._api.download_save(
                save_id,
                dest,
                device_id=self._device_id,
            )
        except RommApiError as exc:
            result.failed.append(f"download {rom.name} ({local_filename}): {exc}")
            return None
        except OSError as exc:
            result.failed.append(f"download {rom.name} ({local_filename}): {exc}")
            return None

        result.downloaded += 1
        return SaveRecord(
            emulator=emulator,
            slot=slot,
            save_filename=local_filename,
            last_sync_md5=download.md5,
            last_sync_server_size=server.get("file_size_bytes") or download.size,
            last_sync_server_updated_at=server.get("updated_at") or "",
            last_synced_at=_now_iso(),
            server_save_id=save_id,
        )

    def _header_for_rom(self, rom: RomState) -> DiscHeader | None:
        """Disc header for a state ROM. Cache hit if the walker ran first."""
        rom_path = self._roms_base / rom.primary_output.path
        if not rom_path.is_file():
            return None
        return lookup_disc_header(rom_path, self._tool, self._cache)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_dolphin_server_saves(
    server_saves: Iterable[dict[str, Any]],
) -> dict[tuple[int, str, str], dict[str, Any]]:
    """Group server saves by (rom_id, "dolphin", slot), filtering out other backends.

    For the same key, RomM may carry multiple history entries (each
    upload appends `[datetime]` to the filename and creates a new
    record). We pick the most recent by `updated_at` — older entries
    are previous backups visible in the web UI but not ferry's concern.
    """
    out: dict[tuple[int, str, str], dict[str, Any]] = {}
    for save in server_saves:
        rom_id = save.get("rom_id")
        emulator = save.get("emulator")
        slot = save.get("slot")
        if (
            not isinstance(rom_id, int)
            or not isinstance(emulator, str)
            or not isinstance(slot, str)
        ):
            continue  # malformed; skip
        if emulator != _DOLPHIN_EMULATOR_LABEL:
            continue  # belongs to another backend
        if not slot:
            continue  # Dolphin saves always have a real slot (m_filename)
        key = (rom_id, emulator, slot)
        existing = out.get(key)
        if existing is None or _updated_after(save, existing):
            out[key] = save
    return out


def _updated_after(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """ISO-8601 timestamps sort lexicographically; tie-break by save id."""
    a_ts = a.get("updated_at") or ""
    b_ts = b.get("updated_at") or ""
    if a_ts != b_ts:
        return a_ts > b_ts
    a_id = a.get("id") or 0
    b_id = b.get("id") or 0
    return a_id > b_id


def _index_dolphin_prior_records(
    roms: Iterable[RomState],
) -> dict[tuple[int, str, str], SaveRecord]:
    """Index `emulator == "dolphin"` SaveRecords across all ROMs.

    Filters out retroarch-tagged records — those belong to
    RetroArchSaveBackend.
    """
    return {
        (rom.rom_id, sr.emulator, sr.slot): sr
        for rom in roms
        for sr in rom.saves
        if sr.emulator == _DOLPHIN_EMULATOR_LABEL
    }


def _save_record_from_server(server: dict[str, Any], *, local_md5: str) -> SaveRecord | None:
    """Build a fresh SaveRecord from a server-save response (post-upload).

    `save_filename` strips RomM's datetime tag — that's what's on disk
    locally, which is what we'll match against on next sync.
    """
    save_id = server.get("id")
    file_name = server.get("file_name") or ""
    emulator = server.get("emulator") or ""
    slot = server.get("slot") or ""
    file_size = server.get("file_size_bytes")
    updated_at = server.get("updated_at") or ""
    if (
        not isinstance(save_id, int)
        or not file_name
        or not emulator
        or not slot
        or not isinstance(file_size, int)
    ):
        return None
    return SaveRecord(
        emulator=emulator,
        slot=slot,
        save_filename=_strip_datetime_tag(file_name),
        last_sync_md5=local_md5,
        last_sync_server_size=file_size,
        last_sync_server_updated_at=updated_at,
        last_synced_at=_now_iso(),
        server_save_id=save_id,
    )


def _merge_save_records(
    existing: tuple[SaveRecord, ...],
    updates: dict[tuple[str, str], SaveRecord | None],
) -> tuple[SaveRecord, ...]:
    """Merge per-key updates into a rom's existing save records.

    `updates` values: SaveRecord (set/replace), or None (drop). Preserves
    SaveRecords for OTHER backends (e.g. retroarch entries on the same
    ROM) — only the keys in `updates` are touched.
    """
    by_key: dict[tuple[str, str], SaveRecord] = {(s.emulator, s.slot): s for s in existing}
    for key, value in updates.items():
        if value is None:
            by_key.pop(key, None)
        else:
            by_key[key] = value
    return tuple(sorted(by_key.values(), key=lambda s: (s.emulator, s.slot)))


def _rom_as_kwargs(rom: RomState) -> dict[str, Any]:
    """Field-by-field kwargs for re-constructing a RomState (avoiding asdict's recursion)."""
    return {
        "rom_id": rom.rom_id,
        "platform_slug": rom.platform_slug,
        "name": rom.name,
        "source_filename": rom.source_filename,
        "source_md5": rom.source_md5,
        "source_size": rom.source_size,
        "source_updated_at": rom.source_updated_at,
        "transforms": rom.transforms,
        "outputs": rom.outputs,
        "primary_output_index": rom.primary_output_index,
        "synced_at": rom.synced_at,
        "saves": rom.saves,
    }


def _flag_ambiguous(result: SaveSyncResult, rom_id: int, filename: str, reason: str) -> None:
    result.ambiguous.append(f"rom_id={rom_id} ({filename}): {reason}")
