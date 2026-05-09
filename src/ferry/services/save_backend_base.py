"""Shared base + Protocol for save sync backends.

Two concrete backends ship today:
  - `RetroArchSaveBackend` (v2): RetroArch SRAM `.srm` files.
  - `GameCubeSaveBackend` (v3): Dolphin GameCube GCI Folder `.gci` files.

Both share an identical sync algorithm — walk local, GET `/api/saves`,
index by `(rom_id, emulator, slot)`, dispatch per-key via the shared
`classify` primitive, upload/download/skip, persist updated SaveRecords.
The backend-specific bits are: how to walk the local saves tree, which
emulator labels this backend manages, how to map a save back to its
on-disk path (for downloads), and where the saves root lives (for
`delete_for_rom`'s relative-path calculation).

`SaveBackendBase` factors out the loop bodies; subclasses implement
four hook methods. The free `SaveBackend` Protocol declares the public
surface (`sync` / `plan` / `delete_for_rom` / `backend_label`) so the
CLI can hold a `list[SaveBackend]` without caring which concrete type.

`SaveSyncResult` lives here (backend-neutral). `get_or_register_device`
lives in `save_backend.py` because it's the device-registration
handshake — used by the CLI before constructing any backend. Both are
re-exported from `save_backend.py` for backward compatibility with
existing imports.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ferry.adapters.romm import RommApi, RommApiError, RommConflictError
from ferry.adapters.romm.http import DownloadResult
from ferry.domain.iso_time import now_iso, parse_iso_to_epoch
from ferry.domain.save_conflicts import Classification, classify
from ferry.domain.save_local import LocalSave
from ferry.domain.save_plan import PlannedSaveAction, SavePlan
from ferry.domain.state import LibraryState, RomState, SaveRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type — backend-neutral; per-run accumulator
# ---------------------------------------------------------------------------


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
    # 409 from a strict-mode upload: server says this device's `last_synced_at`
    # is older than the slot's `updated_at`, so another device has uploaded
    # since we last sync'd. Counted separately from `failed` (which is
    # network/I/O failures) and from `skipped` (which is no-op classify
    # outcomes) — the next sync re-classifies with fresh server state and
    # naturally pivots to download.
    upload_conflicts: int = 0
    ambiguous: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    updated_roms: dict[int, RomState] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return (
            self.uploaded == 0 and self.downloaded == 0 and not self.ambiguous and not self.failed
        )


# ---------------------------------------------------------------------------
# Public Protocol — what the CLI consumes
# ---------------------------------------------------------------------------


@runtime_checkable
class SaveBackend(Protocol):
    """Structural type for any save backend the CLI can drive uniformly."""

    backend_label: str

    def sync(self, state: LibraryState) -> SaveSyncResult: ...

    def sync_for_rom(self, rom: RomState, state: LibraryState) -> SaveSyncResult: ...

    def plan(self, state: LibraryState) -> SavePlan: ...

    def delete_for_rom(self, rom: RomState, trash_dir: Path) -> tuple[int, list[str]]: ...


# ---------------------------------------------------------------------------
# Datetime tag stripping — RomM appends ` [YYYY-MM-DD_HH-MM-SS]` to every
# uploaded save's filename. We strip on download (so the local file lands
# at the path the emulator expects) and on `SaveRecord.save_filename` (which
# represents the on-disk filename, not the server's stored one).
# ---------------------------------------------------------------------------

_DATETIME_TAG_PATTERN = re.compile(r" \[\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\]")


def strip_datetime_tag(filename: str) -> str:
    """`Mario [2026-04-24_15-51-34].srm` → `Mario.srm`."""
    name, ext = os.path.splitext(filename)
    return _DATETIME_TAG_PATTERN.sub("", name) + ext


# ---------------------------------------------------------------------------
# Indexers — backend-parameterized via emulator predicate
# ---------------------------------------------------------------------------


def index_server_saves(
    server_saves: Iterable[dict[str, Any]],
    *,
    record_belongs: Callable[[RomState, str], bool],
    lookup_rom: Callable[[int], RomState | None],
    default_slot: str,
) -> dict[tuple[int, str, str], dict[str, Any]]:
    """Group server saves by (rom_id, emulator, slot), filtered to one backend.

    `record_belongs(rom, emulator) -> bool` decides which records this
    backend owns; takes the rom too so backends sharing an emulator tag
    (Dolphin GC vs Wii — both `"dolphin"`) can disambiguate by platform.
    `lookup_rom(rom_id) -> RomState | None` resolves a record's rom to
    feed the predicate; records whose rom isn't in our state get dropped.

    RomM may serve multiple history entries for the same key (every
    upload appends `[datetime]` to the filename and creates a new
    record); we keep only the most recent by `updated_at` per key.

    `default_slot` is what we fill in when a record's `slot` is missing
    or empty. RetroArch's SRAM convention is `"default"`; Dolphin always
    sets a real slot so the default is never used in practice but we
    require it for symmetry.
    """
    out: dict[tuple[int, str, str], dict[str, Any]] = {}
    for save in server_saves:
        rom_id = save.get("rom_id")
        emulator = save.get("emulator")
        slot = save.get("slot") or default_slot
        if not isinstance(rom_id, int) or not isinstance(emulator, str):
            continue  # malformed
        rom = lookup_rom(rom_id)
        if rom is None:
            continue  # record's rom isn't in our state; another backend or removed locally
        if not record_belongs(rom, emulator):
            continue  # belongs to another backend
        if not slot:
            continue  # empty slot after default fill — skip
        key = (rom_id, emulator, slot)
        existing = out.get(key)
        if existing is None or _updated_after(save, existing):
            out[key] = save
    return out


def _updated_after(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Compare two server-save dicts by `updated_at`; tie-break by save id.

    Parses to epoch seconds rather than lexically so equivalent-instant
    different-offset timestamps (`...Z` vs `...+02:00`) order correctly.
    Unparseable timestamps fall through to 0.0 — they sort to the bottom
    instead of polluting the order.
    """
    a_epoch = parse_iso_to_epoch(a.get("updated_at")) or 0.0
    b_epoch = parse_iso_to_epoch(b.get("updated_at")) or 0.0
    if a_epoch != b_epoch:
        return a_epoch > b_epoch
    a_id = a.get("id") or 0
    b_id = b.get("id") or 0
    return a_id > b_id


def index_prior_records(
    roms: Iterable[RomState],
    *,
    record_belongs: Callable[[RomState, str], bool],
) -> dict[tuple[int, str, str], SaveRecord]:
    """Index this backend's prior SaveRecords across all ROMs.

    `record_belongs(rom, emulator)` filters by both rom and emulator so
    Dolphin's GC and Wii backends can split a shared `"dolphin"` tag
    by platform.
    """
    return {
        (rom.rom_id, sr.emulator, sr.slot): sr
        for rom in roms
        for sr in rom.saves
        if record_belongs(rom, sr.emulator)
    }


# ---------------------------------------------------------------------------
# Per-key classify adapter + display helpers
# ---------------------------------------------------------------------------


def classify_for(
    local: LocalSave | None,
    server: dict[str, Any] | None,
    prev: SaveRecord | None,
    *,
    local_path_exists: bool | None = None,
    local_path_mtime: float | None = None,
) -> Classification:
    """Adapter from our typed inputs to the shared `classify` primitive.

    When `local is None and server is not None`, the caller can pass
    `local_path_exists` / `local_path_mtime` from probing the resolved
    download destination — classify uses that pair to make a
    newer-wins decision instead of falling back to the prior-only
    heuristics. See `domain.save_conflicts.classify` docstring for
    behaviour.
    """
    server_md5 = server.get("content_hash") if server else None
    server_size = server.get("file_size_bytes") if server else None
    server_updated_at = server.get("updated_at") if server else None
    return classify(
        local_md5=local.local_md5 if local else None,
        local_mtime=local.local_mtime if local else None,
        local_save_filename=local.save_filename if local else None,
        server_md5=server_md5 if isinstance(server_md5, str) else None,
        server_size=server_size if isinstance(server_size, int) else None,
        server_updated_at=server_updated_at if isinstance(server_updated_at, str) else None,
        last_sync_md5=prev.last_sync_md5 if prev else None,
        last_sync_server_size=prev.last_sync_server_size if prev else None,
        last_sync_server_updated_at=prev.last_sync_server_updated_at if prev else None,
        local_path_exists=local_path_exists,
        local_path_mtime=local_path_mtime,
    )


def filename_and_slot_for_action(
    direction: str,
    local: LocalSave | None,
    server: dict[str, Any] | None,
    slot: str,
) -> tuple[str, str]:
    """Pick the filename + slot label to display for a planned action.

    Uploads use the local filename; downloads use the server's `file_name`
    with RomM's datetime tag stripped.
    """
    if direction == "upload" and local is not None:
        return local.save_filename, slot
    if direction == "download" and server is not None:
        raw = server.get("file_name") or ""
        return strip_datetime_tag(raw) if raw else "(unknown)", slot
    return "(unknown)", slot


# ---------------------------------------------------------------------------
# SaveRecord builders + state mutation helpers
# ---------------------------------------------------------------------------


def save_record_from_server(server: dict[str, Any], *, local_md5: str) -> SaveRecord | None:
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
        save_filename=strip_datetime_tag(file_name),
        last_sync_md5=local_md5,
        last_sync_server_size=file_size,
        last_sync_server_updated_at=updated_at,
        last_synced_at=now_iso(),
        server_save_id=save_id,
    )


def merge_save_records(
    existing: tuple[SaveRecord, ...],
    updates: dict[tuple[str, str], SaveRecord | None],
) -> tuple[SaveRecord, ...]:
    """Merge per-key updates into a rom's existing save records.

    `updates` values: SaveRecord (set/replace), or None (drop). Preserves
    SaveRecords for OTHER backends (the indexer for one backend only ever
    surfaces its own keys, so other-backend records pass through).
    """
    by_key: dict[tuple[str, str], SaveRecord] = {(s.emulator, s.slot): s for s in existing}
    for key, value in updates.items():
        if value is None:
            by_key.pop(key, None)
        else:
            by_key[key] = value
    return tuple(sorted(by_key.values(), key=lambda s: (s.emulator, s.slot)))


# ---------------------------------------------------------------------------
# Abstract base — shared sync/plan/delete machinery
# ---------------------------------------------------------------------------


class SaveBackendBase(ABC):
    """Abstract base for save sync backends.

    Concrete subclasses provide four required hooks (`_walk_local`,
    `_record_belongs_to_backend`, `_resolve_local_path`, `_saves_root`)
    plus three optional transform hooks with no-op defaults
    (`_pre_upload_archive`, `_download_io_context`,
    `_local_md5_from_download`) — the latter let folder-saves backends
    like Wii NAND interpose archive/extract steps without overriding
    the dispatch loop.

    `default_slot` is the value the indexer fills in when a server
    record's `slot` field is missing — `"default"` for RetroArch SRAM,
    irrelevant for Dolphin (which always sets a real slot).
    """

    backend_label: str
    default_slot: str = "default"

    def __init__(
        self,
        *,
        api: RommApi,
        device_id: str,
        log: logging.Logger | None = None,
    ) -> None:
        self._api = api
        self._device_id = device_id
        self._logger = log or logger

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def _walk_local(self, state: LibraryState) -> tuple[list[LocalSave], list[str]]:
        """Walk this backend's saves tree and emit LocalSave records + warnings."""

    @abstractmethod
    def _record_belongs_to_backend(self, rom: RomState, emulator: str) -> bool:
        """True iff this `(rom, emulator)` pair is owned by this backend.

        Takes both rom and emulator so backends sharing an emulator tag
        (Dolphin GameCube vs Wii — both report `"dolphin"`) can split
        records by `rom.platform_slug`. Predicates that only care about
        the emulator string (RetroArch) just ignore the rom param.
        """

    @abstractmethod
    def _resolve_local_path(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        save_filename: str,
        result: SaveSyncResult | None = None,
    ) -> Path | None:
        """Where on disk does this save live? Returns None when undeterminable.

        The optional `result` lets subclasses route their own warnings
        into the sync result (e.g. RetroArch's "cannot determine local
        path" hint).
        """

    @abstractmethod
    def _saves_root(self) -> Path:
        """Trash relpath base — `delete_for_rom` writes to `<trash>/saves/<rel>`."""

    # ------------------------------------------------------------------
    # Optional transform hooks — pass-through defaults; override for
    # backends whose on-wire format differs from on-disk shape (e.g. Wii
    # NAND saves, which are folders bundled into a single zip per save).
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _pre_upload_archive(self, rom: RomState, local: LocalSave) -> Iterator[Path]:
        """Yield the path the upload should send. Default: pass-through.

        Folder-saves backends override to materialize a transient archive
        in a tmp dir on entry and clean up on exit. The yielded path is
        what gets POSTed to RomM; `local.local_path` is what the walker
        observed on disk (for Wii: the save folder).
        """
        yield local.local_path

    @contextlib.contextmanager
    def _download_io_context(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        save_filename: str,
        final_dest: Path,
    ) -> Iterator[Path]:
        """Yield the path bytes should be downloaded to.

        Default: pass-through (the bytes land directly at `final_dest`).
        Folder-saves backends override to yield a temp file path and,
        on normal exit, transform that into `final_dest` (e.g. Wii
        extracts the temp zip into the save folder). On exception the
        manager cleans up; the caller treats the failure as a download
        error.
        """
        yield final_dest

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
        """Compute what to store in `SaveRecord.last_sync_md5`.

        Default: `download.md5` — byte-md5 of the streamed response,
        matching the local file's bytes for non-archive saves (RA SRAM,
        GC GCI). Wii overrides to `server.content_hash` (RomM's manifest
        hash for zips, equal to `folder_content_hash` of the extracted
        folder by construction); falls back to `download.md5` if the
        server didn't surface a content_hash.

        Future simplification: once RomM ≥ the version that fixed
        PUT-without-content_hash (memory: project_romm_481_hash_bug —
        fixed post-2026-04-07 but unreleased at time of writing) is the
        supported floor, this hook can be deleted. The base class can
        then use `server.content_hash or download.md5` for all backends
        — equivalent to today's `download.md5` for non-archives, and the
        right manifest hash for zip-bundled saves. The hook exists today
        only to keep `download.md5` as the safe fallback for RA/GC
        during the 4.8.1 transition window where stale
        `server.content_hash` could otherwise produce spurious uploads.
        """
        return download.md5

    # ------------------------------------------------------------------
    # delete_for_rom
    # ------------------------------------------------------------------

    def delete_for_rom(self, rom: RomState, trash_dir: Path) -> tuple[int, list[str]]:
        """Move every local save file for *rom* into the rom's trash dir.

        Server-side saves are NOT deleted — they remain on RomM as a
        backup. Mirror across both backends; the only thing each
        subclass differs on is what the saves root is for relpath
        computation.
        """
        local_saves, walker_warnings = self._walk_local_for([rom])
        # Defensive: walker may emit saves for ROMs other than the one
        # asked about (RetroArch indexes ALL stems and matches by name).
        rom_local_saves = [ls for ls in local_saves if ls.rom_id == rom.rom_id]
        if not rom_local_saves:
            return 0, walker_warnings

        saves_subdir = trash_dir / "saves"
        saves_subdir.mkdir(parents=True, exist_ok=True)
        count = 0
        warnings = list(walker_warnings)
        saves_root = self._saves_root()
        for ls in rom_local_saves:
            try:
                rel = ls.local_path.relative_to(saves_root)
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

    def _walk_local_for(self, roms: list[RomState]) -> tuple[list[LocalSave], list[str]]:
        """Walk only the given ROMs.

        Default delegates to `_walk_local` with a state synthesized from
        the rom list. Subclasses whose walker accepts a roms iterable
        directly can override for efficiency.
        """
        synthetic = LibraryState(roms={r.rom_id: r for r in roms})
        return self._walk_local(synthetic)

    # ------------------------------------------------------------------
    # Shared dispatch infrastructure
    # ------------------------------------------------------------------

    def _walk_inputs(
        self, state: LibraryState, *, rom_filter: RomState | None = None
    ) -> tuple[
        dict[tuple[int, str, str], LocalSave],
        dict[tuple[int, str, str], SaveRecord],
        list[str],
    ]:
        """Walk local saves and index prior records. No I/O against RomM.

        `rom_filter` narrows the walk to a single ROM — used by
        `sync_for_rom` for the launch-wrapper fast path. Walker
        "could not match" warnings are filtered out in that mode
        (the walker was given one ROM's stem index, so unrelated
        files in the saves dir aren't real findings).
        """
        if rom_filter is not None:
            local_saves, walker_warnings = self._walk_local_for([rom_filter])
            walker_warnings = [w for w in walker_warnings if "could not match" not in w]
            prev_by_key = {
                (rom_filter.rom_id, sr.emulator, sr.slot): sr
                for sr in rom_filter.saves
                if self._record_belongs_to_backend(rom_filter, sr.emulator)
            }
        else:
            local_saves, walker_warnings = self._walk_local(state)
            prev_by_key = index_prior_records(
                state.roms.values(), record_belongs=self._record_belongs_to_backend
            )
        local_by_key = {(ls.rom_id, ls.emulator, ls.slot): ls for ls in local_saves}
        return local_by_key, prev_by_key, walker_warnings

    def _fetch_server_index(
        self,
        state: LibraryState,
        *,
        rom_id: int | None = None,
    ) -> dict[tuple[int, str, str], dict[str, Any]]:
        """Fetch + index server saves. Raises RommApiError on fetch failure."""
        server_saves = self._api.list_saves(rom_id=rom_id, device_id=self._device_id or None)
        return index_server_saves(
            server_saves,
            record_belongs=self._record_belongs_to_backend,
            lookup_rom=state.roms.get,
            default_slot=self.default_slot,
        )

    # ------------------------------------------------------------------
    # sync
    # ------------------------------------------------------------------

    def sync(self, state: LibraryState) -> SaveSyncResult:
        """Run a full save-sync pass against `state.roms`."""
        return self._run_sync(state, lookup_rom=state.roms.get, rom_filter=None)

    def sync_for_rom(self, rom: RomState, state: LibraryState) -> SaveSyncResult:
        """Run save sync narrowed to a single ROM.

        Used by launch-wrapper hooks (`ferry sync --rom`) for fast per-game
        pre/post sync — one narrow GET, one walker call, no full-library
        scan.

        If `rom` isn't in `state.roms` (orphan, not tracked by ferry yet),
        returns an empty result silently — the launch wrapper proceeds and
        the user can `ferry sync` to register the ROM properly.
        """
        if rom.rom_id not in state.roms:
            return SaveSyncResult()
        return self._run_sync(
            state,
            lookup_rom=lambda rid: rom if rid == rom.rom_id else None,
            rom_filter=rom,
        )

    def _run_sync(
        self,
        state: LibraryState,
        *,
        lookup_rom: Callable[[int], RomState | None],
        rom_filter: RomState | None,
    ) -> SaveSyncResult:
        """Shared implementation of `sync` and `sync_for_rom`.

        `lookup_rom` decides which ROM to associate with each key —
        full-sync uses `state.roms.get`; per-rom sync uses a closure
        that returns the passed-in rom (or None for keys that don't
        belong to it). `rom_filter` narrows the walker + server fetch
        in the per-rom case.
        """
        local_by_key, prev_by_key, walker_warnings = self._walk_inputs(state, rom_filter=rom_filter)
        try:
            server_by_key = self._fetch_server_index(
                state,
                rom_id=rom_filter.rom_id if rom_filter is not None else None,
            )
        except RommApiError as exc:
            return SaveSyncResult(
                failed=[f"could not list server saves: {exc}"],
                warnings=list(walker_warnings),
            )

        result = SaveSyncResult(warnings=list(walker_warnings))
        rom_save_updates: dict[int, dict[tuple[str, str], SaveRecord | None]] = {}

        for key in sorted(set(local_by_key) | set(server_by_key) | set(prev_by_key)):
            rom_id, emulator, slot = key
            rom = lookup_rom(rom_id)
            if rom is None:
                # Server has saves for a ROM not in our state — happens when
                # ROMs have been removed locally but saves linger server-side.
                continue
            should_update, outcome = self._process_key(
                rom,
                emulator,
                slot,
                local_by_key.get(key),
                server_by_key.get(key),
                prev_by_key.get(key),
                result,
            )
            if should_update:
                rom_save_updates.setdefault(rom_id, {})[(emulator, slot)] = outcome

        for rom_id, updates in rom_save_updates.items():
            rom = lookup_rom(rom_id)
            if rom is None:
                continue  # defensive — lookup_rom was happy a moment ago
            new_saves = merge_save_records(rom.saves, updates)
            result.updated_roms[rom_id] = replace(rom, saves=new_saves)

        return result

    # ------------------------------------------------------------------
    # plan (read-only)
    # ------------------------------------------------------------------

    def plan(self, state: LibraryState) -> SavePlan:
        """Compute what `.sync(state)` would do, without performing it.

        Read-only: walks local saves, fetches server saves (one GET; no
        device_id required for listing), and runs the same per-key
        decision logic recording each intended action instead of
        executing.
        """
        local_by_key, prev_by_key, walker_warnings = self._walk_inputs(state)
        try:
            server_by_key = self._fetch_server_index(state)
        except RommApiError as exc:
            return SavePlan(
                backend_label=self.backend_label,
                failed=(f"could not list server saves: {exc}",),
                warnings=tuple(walker_warnings),
            )
        all_keys = set(local_by_key) | set(server_by_key) | set(prev_by_key)

        to_upload: list[PlannedSaveAction] = []
        to_download: list[PlannedSaveAction] = []
        ambiguous: list[str] = []
        skipped = 0
        drop_count = 0
        conflict_count = 0

        for key in sorted(all_keys):
            rom_id, emulator, slot = key
            rom = state.roms.get(rom_id)
            if rom is None:
                continue
            local = local_by_key.get(key)
            server = server_by_key.get(key)
            prev = prev_by_key.get(key)
            path_exists, path_mtime, candidate = self._probe_local_path_for_server_only(
                rom, emulator, slot, local, server, prev
            )
            decision = classify_for(
                local,
                server,
                prev,
                local_path_exists=path_exists,
                local_path_mtime=path_mtime,
            )
            if decision.conflict_resolved:
                conflict_count += 1
            if decision.ambiguous_message is not None:
                filename = local.save_filename if local else (candidate or "?")
                ambiguous.append(
                    f"{rom.name} — {filename} (rom_id={rom_id}, "
                    f"emulator={emulator}, slot={slot}, "
                    f"{decision.ambiguous_message})"
                )
            if decision.action == "skip":
                skipped += 1
                continue
            if decision.action == "drop_prior":
                drop_count += 1
                continue
            if decision.action == "ambiguous":
                continue
            save_filename, slot_for_display = filename_and_slot_for_action(
                decision.action, local, server, slot
            )
            entry = PlannedSaveAction(
                rom_id=rom_id,
                rom_name=rom.name,
                emulator=emulator,
                slot=slot_for_display,
                save_filename=save_filename,
                direction=decision.action,
                reason=decision.reason,
            )
            if decision.action == "upload":
                to_upload.append(entry)
            else:
                to_download.append(entry)

        return SavePlan(
            backend_label=self.backend_label,
            to_upload=tuple(to_upload),
            to_download=tuple(to_download),
            skipped=skipped,
            conflicts_resolved=conflict_count,
            drop_prior_count=drop_count,
            ambiguous=tuple(ambiguous),
            warnings=tuple(walker_warnings),
        )

    # ------------------------------------------------------------------
    # Per-key dispatch
    # ------------------------------------------------------------------

    def _probe_local_path_for_server_only(
        self,
        rom: RomState,
        emulator: str,
        slot: str,
        local: LocalSave | None,
        server: dict[str, Any] | None,
        prev: SaveRecord | None,
    ) -> tuple[bool | None, float | None, str | None]:
        """Probe the resolved local path when the walker found nothing
        for this key but a server save exists.

        Returns `(exists, mtime_or_None, candidate_filename_or_None)`.
        The third element is the filename ferry would resolve for this
        key — useful to surface in dry-run output for the
        ambiguous/server-only cases where we don't have a `LocalSave`
        to read the filename off. `(None, None, None)` is returned
        when the case doesn't apply or the path can't be resolved
        (e.g. Dolphin disc header unreadable). Pre-probe callers
        (older sites) get all-None and fall back to the prior-based
        reasoning.

        For the server-only case: derive the candidate save filename
        from the prior record (preferred — it's the filename ferry
        last wrote) or from the server response (fallback). Resolve
        through the backend's `_resolve_local_path` and stat. Failures
        return `(False, None, candidate_filename)` for "path doesn't
        exist" semantics — the caller decides whether that means
        download-to-restore or skip.
        """
        if local is not None or server is None:
            return None, None, None
        candidate_filename = (
            prev.save_filename
            if prev is not None
            else strip_datetime_tag(server.get("file_name") or "") or None
        )
        if not candidate_filename:
            return None, None, None
        try:
            dest = self._resolve_local_path(rom, emulator, slot, candidate_filename, result=None)
        except Exception:
            logger.exception(
                "_resolve_local_path raised while probing for orphan key %r",
                (rom.rom_id, emulator, slot),
            )
            return None, None, candidate_filename
        if dest is None:
            return None, None, candidate_filename
        try:
            stat = dest.stat()
        except FileNotFoundError:
            return False, None, candidate_filename
        except OSError:
            logger.warning(
                "could not stat resolved local path %s for orphan key %r",
                dest,
                (rom.rom_id, emulator, slot),
            )
            return True, None, candidate_filename
        return True, stat.st_mtime, candidate_filename

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
        path_exists, path_mtime, candidate = self._probe_local_path_for_server_only(
            rom, emulator, slot, local, server, prev
        )
        decision = classify_for(
            local,
            server,
            prev,
            local_path_exists=path_exists,
            local_path_mtime=path_mtime,
        )
        if decision.conflict_resolved:
            result.conflicts_resolved += 1
        if decision.ambiguous_message is not None:
            filename = local.save_filename if local else (candidate or "?")
            result.ambiguous.append(
                f"{rom.name} — {filename} (rom_id={rom.rom_id}, "
                f"emulator={emulator}, slot={slot}, "
                f"{decision.ambiguous_message})"
            )

        if decision.action == "skip":
            result.skipped += 1
            return False, None
        if decision.action == "ambiguous":
            return False, None
        if decision.action == "drop_prior":
            return True, None
        if decision.action == "upload":
            assert local is not None
            outcome = self._do_upload(rom, local, prev, server, result)
            return (outcome is not None, outcome)
        if decision.action == "download":
            assert server is not None
            outcome = self._do_download(rom, emulator, slot, server, result)
            return (outcome is not None, outcome)
        return False, None  # defensive

    # ------------------------------------------------------------------
    # Upload / Download — shared logic over hookable path resolution
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
            with self._pre_upload_archive(rom, local) as upload_path:
                response = self._api.upload_save(
                    rom.rom_id,
                    upload_path,
                    emulator=local.emulator,
                    save_id=save_id,
                    device_id=self._device_id,
                    slot=local.slot,
                )
        except RommConflictError:
            # Server-as-arbiter: another device uploaded since this device's
            # last sync. Preserve the prior verbatim — next sync re-classifies
            # with fresh server state and naturally pivots to download.
            result.upload_conflicts += 1
            result.warnings.append(
                f"upload {rom.name} ({local.save_filename}): server has a newer "
                f"save than your last sync; will reconcile on next sync"
            )
            return prev
        except RommApiError as exc:
            result.failed.append(f"upload {rom.name} ({local.save_filename}): {exc}")
            return prev
        except OSError as exc:
            # `_pre_upload_archive` may raise OSError while materializing a
            # transient archive (Wii's zip-build, future folder backends).
            # Mirrors RommApiError handling: surfaces as `failed`, prior preserved.
            result.failed.append(f"upload {rom.name} ({local.save_filename}): {exc}")
            return prev
        result.uploaded += 1
        return save_record_from_server(response, local_md5=local.local_md5)

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

        local_filename = strip_datetime_tag(server_filename)
        final_dest = self._resolve_local_path(rom, emulator, slot, local_filename, result)
        if final_dest is None:
            return None  # subclass wrote a `failed` entry into result

        try:
            with self._download_io_context(
                rom, emulator, slot, local_filename, final_dest
            ) as download_path:
                download_path.parent.mkdir(parents=True, exist_ok=True)
                download = self._api.download_save(
                    save_id,
                    download_path,
                    device_id=self._device_id,
                )
        except RommApiError as exc:
            result.failed.append(f"download {rom.name} ({local_filename}): {exc}")
            return None
        except OSError as exc:
            result.failed.append(f"download {rom.name} ({local_filename}): {exc}")
            return None

        # v3.5 server-as-arbiter: bytes are on disk (atomic via .part rename),
        # but the SaveRecord — which represents "properly synced" locally —
        # is only written after `confirm_download` succeeds. RomM's
        # `device.last_synced_at` for this slot only advances on confirm,
        # so a failure here leaves both sides at their previous state and
        # the next sync re-tries. The local file stays on disk and will be
        # atomically overwritten by the next download.
        try:
            self._api.confirm_download(save_id, self._device_id)
        except RommApiError as exc:
            result.failed.append(
                f"download {rom.name} ({local_filename}): bytes written but "
                f"confirm failed ({exc}); next sync will retry"
            )
            return None

        last_sync_md5 = self._local_md5_from_download(
            download=download,
            server=server,
            rom=rom,
            emulator=emulator,
            slot=slot,
            final_dest=final_dest,
        )
        result.downloaded += 1
        return SaveRecord(
            emulator=emulator,
            slot=slot,
            save_filename=local_filename,
            last_sync_md5=last_sync_md5,
            last_sync_server_size=server.get("file_size_bytes") or download.size,
            last_sync_server_updated_at=server.get("updated_at") or "",
            last_synced_at=now_iso(),
            server_save_id=save_id,
        )
