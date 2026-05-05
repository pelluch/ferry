"""RetroArch save sync backend (v2).

Combines the local-saves walker (`adapters.retroarch_saves`), the RomM
saves API (`adapters.romm.api.list_saves` + friends), and the conflict
primitives (`domain.save_conflicts`) into a complete sync pass.

Algorithm:
  1. Walk local saves once (`list_local_saves`).
  2. Bulk-fetch every server save in a single `api.list_saves()` call —
     no per-ROM fan-out.
  3. Group local + server + prior-sync records by (rom_id, emulator,
     slot). The diff layer operates on these triple-keyed buckets.
  4. For each key, decide an action via the conflict primitives:
       - local-only       → upload (new save).
       - server-only      → download (new save from another device).
       - both, no prior   → newest-wins (`resolve_newest`); ambiguous
                            within tolerance → skip with warning.
       - both, with prior → `local_changed` + `server_changed_fast`
                            → `determine_action`. Conflict resolves via
                            newest-wins.
       - neither, prior   → save was deleted both places; clear the
                            prior record on the next state save.
  5. On success, persist an updated `SaveRecord` into the rom's
     `saves` tuple. The caller merges `result.updated_roms` into
     `LibraryState.roms`.

Out of scope for this checkpoint (deferred):
  - Wire-up into `ferry sync` CLI (next checkpoint).
  - `delete_for_rom` hook integrated with the executor's existing trash
    path (next checkpoint).
  - 409-on-stale-upload retry — single-user RomM rarely has a concurrent
    write between this sync's start and end. Next sync resolves any
    drift via the standard diff path.

The 'backend' framing is forward-looking: v3 (GameCube/Dolphin) will
reveal what's worth abstracting into a `SaveBackend` Protocol. For v2
this is a single concrete class. (DESIGN.md §5.3.)
"""

from __future__ import annotations

import logging
import os
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ferry import __version__
from ferry.adapters.retroarch_core_info import CoreInfoIndex
from ferry.adapters.retroarch_paths import RetroArchInstall
from ferry.adapters.retroarch_saves import LocalSave, list_local_saves
from ferry.adapters.romm import RommApi, RommApiError
from ferry.domain.save_conflicts import (
    determine_action,
    local_changed,
    resolve_newest,
    server_changed_fast,
)
from ferry.domain.state import LibraryState, RomState, SaveRecord

logger = logging.getLogger(__name__)

_DEFAULT_SLOT = "default"

# Action labels emitted internally for clarity.
_Action = Literal["upload", "download", "skip", "ambiguous", "drop_prior"]

# Matches the suffix RomM appends to save filenames on every upload — the
# `_apply_datetime_tag` logic in `backend/endpoints/saves.py`. We strip it
# both from downloads (so RetroArch finds the file at the expected
# `<rom-stem>.srm` path) and from SaveRecord.save_filename (which represents
# what's on disk locally, not what the server stores).
_DATETIME_TAG_PATTERN = re.compile(r" \[\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\]")


def _strip_datetime_tag(filename: str) -> str:
    """Strip RomM's ` [YYYY-MM-DD_HH-MM-SS]` tag from a save filename.

    `Mario [2026-04-24_15-51-34].srm` → `Mario.srm`. RetroArch writes saves
    using the ROM's plain basename; the timestamp is RomM's history-keeping,
    invisible at the filesystem level.
    """
    name, ext = os.path.splitext(filename)
    return _DATETIME_TAG_PATTERN.sub("", name) + ext


@dataclass(slots=True, kw_only=True)
class SaveSyncResult:
    """Per-run summary of a save sync pass.

    Mutable counter accumulator — the SaveBackend builds it up as it
    decides actions for each (rom_id, emulator, slot) key. Returned to
    the caller once and discarded; not shared across runs.

    `updated_roms` maps rom_id → updated RomState (with new `saves` tuple).
    The caller merges these into the live LibraryState before persisting.
    """

    uploaded: int = 0
    downloaded: int = 0
    skipped: int = 0
    conflicts_resolved: int = 0
    ambiguous: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    updated_roms: dict[int, RomState] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return (
            self.uploaded == 0 and self.downloaded == 0 and not self.ambiguous and not self.failed
        )


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


class RetroArchSaveBackend:
    """Sync the local RetroArch saves dir with RomM's `/api/saves`.

    Stateless across calls — all persistent state lives in the passed-in
    `LibraryState`. `.sync()` returns a `SaveSyncResult` carrying the
    counters plus updated rom records; the caller persists.
    """

    def __init__(
        self,
        *,
        install: RetroArchInstall,
        api: RommApi,
        device_id: str,
        log: logging.Logger | None = None,
    ) -> None:
        self._install = install
        self._api = api
        self._device_id = device_id
        self._logger = log or logger
        self._core_info = CoreInfoIndex(install)

    def delete_for_rom(self, rom: RomState, trash_dir: Path) -> tuple[int, list[str]]:
        """Move every local save file for *rom* into the rom's trash dir.

        Server-side saves are NOT deleted — they remain on RomM as a backup.
        If the user later re-adds the ROM (or syncs from another device),
        the saves are still discoverable via `list_saves`. The state-side
        cleanup happens via the executor removing the rom from
        `LibraryState.roms` entirely.

        `trash_dir` is the per-ROM trash directory the executor already
        created when trashing the ROM file itself; we drop saves into a
        `saves/` subdir there so they sit alongside the ROM artifact and
        the user can see the full deletion footprint in one place.

        Returns (count_trashed, warnings).
        """
        local_saves, walker_warnings = list_local_saves(self._install, [rom])
        # Walker indexes ALL stems across all ROMs; restrict to this rom_id.
        rom_local_saves = [ls for ls in local_saves if ls.rom_id == rom.rom_id]

        if not rom_local_saves:
            return 0, walker_warnings

        saves_subdir = trash_dir / "saves"
        saves_subdir.mkdir(parents=True, exist_ok=True)
        count = 0
        warnings = list(walker_warnings)
        for ls in rom_local_saves:
            try:
                rel = ls.local_path.relative_to(self._install.savefile_directory)
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

    def sync(self, state: LibraryState) -> SaveSyncResult:
        """Run a full save-sync pass against `state.roms`."""
        local_saves, walker_warnings = list_local_saves(
            self._install, state.roms.values(), core_info=self._core_info
        )

        try:
            server_saves = self._api.list_saves(device_id=self._device_id)
        except RommApiError as exc:
            return SaveSyncResult(
                failed=[f"could not list server saves: {exc}"],
                warnings=walker_warnings,
            )

        local_by_key = {(ls.rom_id, ls.emulator, ls.slot): ls for ls in local_saves}
        server_by_key = _index_server_saves(server_saves)
        prev_by_key = _index_prior_records(state.roms.values())

        all_keys = set(local_by_key) | set(server_by_key) | set(prev_by_key)

        # Aggregate counters and per-rom updated-saves accumulator.
        result = SaveSyncResult(warnings=list(walker_warnings))
        rom_save_updates: dict[int, dict[tuple[str, str], SaveRecord | None]] = {}

        for key in sorted(all_keys):
            rom_id, emulator, slot = key
            rom = state.roms.get(rom_id)
            if rom is None:
                # Server has saves for a ROM not in our state — happens during
                # transitional periods when ROMs have been removed locally but
                # saves linger server-side. Skip; checkpoint 5 wires
                # delete_for_rom for the proper cleanup path.
                continue
            local = local_by_key.get(key)
            server = server_by_key.get(key)
            prev = prev_by_key.get(key)

            should_update, outcome = self._process_key(
                rom, emulator, slot, local, server, prev, result
            )
            if should_update:
                rom_save_updates.setdefault(rom_id, {})[(emulator, slot)] = outcome

        # Build updated RomState records, merging in the new SaveRecords.
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
        """Resolve and execute action for one (rom_id, emulator, slot) key.

        Returns `(should_update, outcome)`:
          - (False, _)        — leave RomState.saves alone (skip / ambiguous /
                                 execution failure all collapse to "no update").
          - (True, SaveRecord) — write this record into the rom's saves tuple.
          - (True, None)       — drop the existing record (both sides deleted).
        """
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
        """Compute the action label for one key. No I/O — pure decision logic."""
        if local is None and server is None:
            # Both gone — clear any prior record.
            return "drop_prior" if prev is not None else "skip"

        if local is None and server is not None:
            return "download"

        if local is not None and server is None:
            return "upload"

        # Both present.
        assert local is not None and server is not None
        local_md5 = local.local_md5
        server_md5 = server.get("content_hash") or ""
        server_size = server.get("file_size_bytes")
        server_updated_at = server.get("updated_at") or ""

        if prev is None:
            # First-time conflict — never synced before; use newest-wins.
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
                return "skip"  # bytes identical; no-op even though we lack a record
            result.conflicts_resolved += 1
            return "upload" if resolution == "upload" else "download"

        # We have a prior sync record — full diff.
        l_changed = local_changed(local_md5, prev.last_sync_md5)
        s_changed = server_changed_fast(
            stored_updated_at=prev.last_sync_server_updated_at,
            stored_size=prev.last_sync_server_size,
            server_updated_at=server_updated_at,
            server_size=server_size,
        )
        if s_changed is None:
            # Slow path: hash compare against prior baseline.
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
                overwrite=True,  # we resolved any conflict; force the write
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

        # Strip RomM's `[YYYY-MM-DD_HH-MM-SS]` tag from the filename — the
        # local file must match `<rom-stem>.srm` for RetroArch to load it.
        local_filename = _strip_datetime_tag(server_filename)
        dest = _resolve_local_path_for(
            self._install, rom, emulator, local_filename, self._core_info
        )
        if dest is None:
            result.failed.append(
                f"download {rom.name} ({local_filename}): cannot determine local path "
                f"(emulator={emulator!r}; check sort_savefiles_* settings)"
            )
            return None

        # Download directly to dest. `RommHttpAdapter.download` already uses
        # a `<dest>.part` sibling for atomic placement — no tempfile needed,
        # which sidesteps the cross-device-link issue when /tmp is a
        # different filesystem from the saves dir.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_server_saves(
    server_saves: Iterable[dict[str, Any]],
) -> dict[tuple[int, str, str], dict[str, Any]]:
    """Group raw server save dicts by (rom_id, emulator, slot), retroarch-only.

    RomM accumulates saves per slot — every upload appends a `[datetime]`
    suffix to the filename and creates a new record (`_apply_datetime_tag`
    in RomM's `endpoints/saves.py`). For ferry's diff, the meaningful
    "current" save for a key is the most recent one by `updated_at`; older
    timestamped versions are previous backups the user can recover via the
    web UI but aren't ferry's concern. PUT to the chosen save's id on the
    next upload keeps the chain in place rather than spawning new entries.

    Saves with non-retroarch emulator labels (e.g. v3's `"dolphin"`) are
    skipped — those belong to a different SaveBackend and would only
    produce spurious "cannot determine local path" failures here.

    `slot` is normalized: None or "" → "default" so the key matches what
    the local walker emits.
    """
    out: dict[tuple[int, str, str], dict[str, Any]] = {}
    for save in server_saves:
        rom_id = save.get("rom_id")
        emulator = save.get("emulator")
        slot = save.get("slot") or _DEFAULT_SLOT
        if not isinstance(rom_id, int) or not isinstance(emulator, str):
            continue  # malformed; skip
        if not emulator.startswith("retroarch"):
            continue  # belongs to another backend
        key = (rom_id, emulator, slot)
        existing = out.get(key)
        if existing is None or _updated_after(save, existing):
            out[key] = save
    return out


def _updated_after(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True iff a's `updated_at` sorts strictly after b's. ISO-8601 strings
    sort correctly lexicographically; tie-break by save id (higher = newer).
    """
    a_ts = a.get("updated_at") or ""
    b_ts = b.get("updated_at") or ""
    if a_ts != b_ts:
        return a_ts > b_ts
    a_id = a.get("id") or 0
    b_id = b.get("id") or 0
    return a_id > b_id


def _index_prior_records(roms: Iterable[RomState]) -> dict[tuple[int, str, str], SaveRecord]:
    """Index retroarch SaveRecords across all ROMs by (rom_id, emulator, slot).

    Filters to retroarch-tagged records — non-retroarch ones (v3's
    `"dolphin"`) are managed by other backends and would otherwise
    appear as spurious "drop_prior" candidates here.
    """
    return {
        (rom.rom_id, sr.emulator, sr.slot): sr
        for rom in roms
        for sr in rom.saves
        if sr.emulator.startswith("retroarch")
    }


def _save_record_from_server(server: dict[str, Any], *, local_md5: str) -> SaveRecord | None:
    """Build a fresh SaveRecord from a server-save response (post-upload).

    Uses the locally computed MD5 as the last-sync hash — the bytes we
    just wrote upstream. Server hash is also available but the local
    one is the canonical "what did we send" signal.

    The server's `file_name` carries RomM's `[YYYY-MM-DD_HH-MM-SS]` tag;
    `SaveRecord.save_filename` represents the LOCAL filename (no tag),
    so we strip the tag here. Tests against the local path on disk on
    next sync use this value, not the server's tagged form.
    """
    save_id = server.get("id")
    file_name = server.get("file_name") or ""
    emulator = server.get("emulator") or ""
    slot = server.get("slot") or _DEFAULT_SLOT
    file_size = server.get("file_size_bytes")
    updated_at = server.get("updated_at") or ""
    if (
        not isinstance(save_id, int)
        or not file_name
        or not emulator
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


def _merge_save_records(
    existing: tuple[SaveRecord, ...],
    updates: dict[tuple[str, str], SaveRecord | None],
) -> tuple[SaveRecord, ...]:
    """Merge per-key updates into a rom's existing save records.

    `updates` values are: SaveRecord (set or replace), or None
    (drop the prior record entirely).
    """
    by_key: dict[tuple[str, str], SaveRecord] = {(s.emulator, s.slot): s for s in existing}
    for key, value in updates.items():
        if value is None:
            by_key.pop(key, None)
        else:
            by_key[key] = value
    # Stable order: sort by (emulator, slot) so state.json diffs are tidy.
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
