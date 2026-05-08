"""Pure-domain logic for `ferry reconcile` — walk for orphan files,
classify them against a per-platform RomM index, synthesize `RomState`
entries for confident matches.

No HTTP, no state.json writes — those live in
`cli/reconcile.py`. This module is the testable core.

**Classification model** (mirrors what `cli/reconcile.py` displays):

  - **Confident** — local file's name matches a `RomFile.file_name`
    AND the local file's identifying hash (per RomM's largest-inner
    convention; see `adapters/orphan_hash.py`) matches that file's
    `md5_hash`. Same `(rom_id, file_id)` shows up in both indexes.
    Safe to adopt by default.
  - **NameOnly** — name matches at least one RomFile but hash
    doesn't. Common cause: user has a different revision/region of
    the same game, or it's a transformed-but-non-deterministic
    platform like Xbox post-`extract_xiso`.
  - **HashOnly** — hash matches but the local filename doesn't match
    any RomFile's `file_name`. Usually means the user renamed the
    file locally.
  - **Ambiguous** — local file's name AND hash both match, but the
    matches resolve to multiple distinct `rom_id`s. Rare; typically a
    duplicate in RomM. Always skipped — adoption can't decide.
  - **NoMatch** — neither name nor hash matches anything in the
    platform's RomM listing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ferry.adapters.orphan_hash import hash_file_bytes, hash_orphan_file
from ferry.domain.rom_files import resolve_local_filename
from ferry.domain.state import LibraryState, RomState, TransformedOutput

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrphanCandidate:
    """A local file not present in state.roms — a reconcile candidate."""

    abs_path: Path
    rel_path: Path  # relative to roms_base
    platform_dir: str  # the immediate dir name under roms_base


@dataclass(frozen=True, slots=True)
class MatchedFile:
    """One (rom, file) pair in RomM that matched an orphan."""

    rom_id: int
    file_id: int
    rom_name: str
    file_name: str
    file_md5: str | None
    rom_data: dict[str, Any]
    file_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Confident:
    """Name AND hash both match a single rom — safe to adopt."""

    orphan: OrphanCandidate
    match: MatchedFile
    local_md5: str  # the matching md5 (largest-inner-file convention)


@dataclass(frozen=True, slots=True)
class NameOnly:
    """Filename matches RomM but hash doesn't."""

    orphan: OrphanCandidate
    candidates: tuple[MatchedFile, ...]
    local_md5: str | None  # may be None if hashing failed


@dataclass(frozen=True, slots=True)
class HashOnly:
    """Hash matches RomM but filename doesn't (renamed local file)."""

    orphan: OrphanCandidate
    candidates: tuple[MatchedFile, ...]
    local_md5: str


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """Name+hash match, but multiple distinct rom_ids — can't adopt."""

    orphan: OrphanCandidate
    matches: tuple[MatchedFile, ...]
    local_md5: str


@dataclass(frozen=True, slots=True)
class NoMatch:
    """No name and no hash match in the platform's RomM listing."""

    orphan: OrphanCandidate
    local_md5: str | None


Classification = Confident | NameOnly | HashOnly | Ambiguous | NoMatch


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def find_orphans(
    *,
    roms_base: Path,
    state: LibraryState,
    platform_filter: str | None = None,
) -> list[OrphanCandidate]:
    """Walk `roms_base` and return files that aren't tracked by ferry.

    Excludes:
      - files tracked in `state.roms[*].outputs[*].path`,
      - dotfiles (KDE droppings, etc.),
      - files at the top level of `roms_base` (must be inside a
        platform-shaped subdir).

    `platform_filter` (when set) limits the walk to one platform
    subdir name — used by `--platform <slug>` after slug→dir
    resolution.
    """
    if not roms_base.is_dir():
        return []

    tracked_paths: set[Path] = set()
    for rom in state.roms.values():
        for output in rom.outputs:
            tracked_paths.add(roms_base / output.path)

    out: list[OrphanCandidate] = []
    for platform_dir_path in sorted(roms_base.iterdir()):
        if not platform_dir_path.is_dir():
            continue
        platform_dir = platform_dir_path.name
        if platform_filter is not None and platform_dir != platform_filter:
            continue
        for path in sorted(platform_dir_path.rglob("*")):
            if not path.is_file():
                continue
            if path.name.startswith("."):
                continue
            if path in tracked_paths:
                continue
            try:
                rel = path.relative_to(roms_base)
            except ValueError:  # symlinks pointing outside the tree
                continue
            out.append(
                OrphanCandidate(
                    abs_path=path,
                    rel_path=rel,
                    platform_dir=platform_dir,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def build_index(
    roms: list[dict[str, Any]],
) -> tuple[
    dict[str, list[MatchedFile]],
    dict[str, list[MatchedFile]],
    dict[str, list[MatchedFile]],
]:
    """Return `(by_name, by_hash, by_stem)` maps over RomM's per-platform
    rom listing.

    Each rom in `roms` should have a `files: list[RomFileSchema]` field;
    we index every file three ways: once by full `file_name`, once by
    md5, once by filename stem (the part before the last extension).
    Stem-indexing handles the unzip case — server file `Game.zip`
    extracts locally to `Game.iso`/`Game.rvz`/`Game.gba`/etc., same
    stem, hash matches against the largest-inner convention.

    Files lacking `md5_hash` (RomM hadn't computed one yet, or platform
    is in `NON_HASHABLE_PLATFORMS`) are still indexed by name and stem
    so name-only classification still works for them.
    """
    by_name: dict[str, list[MatchedFile]] = {}
    by_hash: dict[str, list[MatchedFile]] = {}
    by_stem: dict[str, list[MatchedFile]] = {}
    for rom in roms:
        rom_id = _safe_int(rom.get("id"))
        if rom_id is None:
            continue
        rom_name = str(rom.get("name") or "?")
        for file_data in rom.get("files") or []:
            file_id = _safe_int(file_data.get("id"))
            if file_id is None:
                continue
            file_name = str(file_data.get("file_name") or "")
            md5 = (file_data.get("md5_hash") or "").lower() or None
            mf = MatchedFile(
                rom_id=rom_id,
                file_id=file_id,
                rom_name=rom_name,
                file_name=file_name,
                file_md5=md5,
                rom_data=rom,
                file_data=file_data,
            )
            if file_name:
                by_name.setdefault(file_name, []).append(mf)
                stem = Path(file_name).stem
                if stem and stem != file_name:
                    by_stem.setdefault(stem, []).append(mf)
            if md5:
                by_hash.setdefault(md5, []).append(mf)
    return by_name, by_hash, by_stem


def classify(
    orphan: OrphanCandidate,
    by_name: dict[str, list[MatchedFile]],
    by_hash: dict[str, list[MatchedFile]],
    by_stem: dict[str, list[MatchedFile]] | None = None,
) -> Classification:
    """Classify one orphan against a per-platform RomM index.

    Hashing follows RomM's "largest inner file of an archive" rule —
    see `adapters/orphan_hash.py:hash_orphan_file`. For non-archive
    files this collapses to a direct md5.

    **Name-equivalence** is the union of two relations:
      1. Full filename equality (`Game.gba` ↔ `Game.gba`) — the
         pass-through case.
      2. Stem equality (`Game.rvz` ↔ `Game.zip`) — the unzipped
         transform case. RomM hashes the zip's largest inner file,
         which equals the local `.rvz`.

    Both flavours feed every classification:
      - **Confident**: name-equivalent AND hash-equal → safe adopt.
      - **NameOnly**: name-equivalent without hash agreement
        (different revision/region, or non-deterministic transform
        like `extract_xiso`). Reported only; `--include-name-only`
        opts in to adopt single-rom_id matches.
      - **HashOnly**: hash matches but neither full-name nor stem
        does. Indicates the user renamed the file. Always
        reported only — see DESIGN.md §7 v9+ for why
        `--include-renames` was rejected.
      - **Ambiguous**: Confident match resolves to multiple
        rom_ids.
      - **NoMatch**: nothing.

    `by_stem` is optional for backward compatibility (older
    callers that didn't compute it). Without it, only full-name
    matches contribute.
    """
    local_md5 = hash_orphan_file(orphan.abs_path)
    name_matches = by_name.get(orphan.abs_path.name, [])
    hash_matches = by_hash.get(local_md5, []) if local_md5 else []
    stem_matches = (by_stem or {}).get(orphan.abs_path.stem, [])

    name_keys = {(m.rom_id, m.file_id) for m in name_matches}
    stem_keys = {(m.rom_id, m.file_id) for m in stem_matches}
    hash_keys = {(m.rom_id, m.file_id) for m in hash_matches}

    # Name-equivalence union: full-name OR stem.
    name_equiv_keys = name_keys | stem_keys
    confident_keys = name_equiv_keys & hash_keys

    if confident_keys:
        confidents = _dedup_matches(confident_keys, name_matches + stem_matches + hash_matches)
        unique_rom_ids = {m.rom_id for m in confidents}
        if len(unique_rom_ids) > 1:
            return Ambiguous(orphan=orphan, matches=tuple(confidents), local_md5=local_md5 or "")
        return Confident(orphan=orphan, match=confidents[0], local_md5=local_md5 or "")

    if name_equiv_keys:
        # Name-equivalent candidates exist but no hash match — different
        # bytes for the (presumably) same logical ROM. `--include-name-only`
        # adopts single-rom_id matches; multi-rom_id stays unadopted.
        candidates = _dedup_matches(name_equiv_keys, name_matches + stem_matches)
        return NameOnly(orphan=orphan, candidates=tuple(candidates), local_md5=local_md5)
    if hash_matches:
        return HashOnly(orphan=orphan, candidates=tuple(hash_matches), local_md5=local_md5 or "")
    return NoMatch(orphan=orphan, local_md5=local_md5)


def _dedup_matches(keys: set[tuple[int, int]], candidates: list[MatchedFile]) -> list[MatchedFile]:
    """Return MatchedFiles whose (rom_id, file_id) is in `keys`, deduped
    in source order so the first appearance wins."""
    seen: set[tuple[int, int]] = set()
    out: list[MatchedFile] = []
    for m in candidates:
        key = (m.rom_id, m.file_id)
        if key in keys and key not in seen:
            out.append(m)
            seen.add(key)
    return out


# ---------------------------------------------------------------------------
# State synthesis
# ---------------------------------------------------------------------------


def synthesize_state_from_match(
    orphan: OrphanCandidate,
    match: MatchedFile,
    *,
    transforms_for_platform: tuple[str, ...],
    now_iso: str | None = None,
) -> RomState:
    """Build a `RomState` from one (orphan, MatchedFile) pair.

    Used both by Confident adoption and `--include-name-only` adoption
    (see `synthesize_state`'s wrapper).

    `output.md5` is the direct md5 of the local file bytes — what
    ferry's sync executor would have computed had it produced this
    file via download+pipeline. For Confident matches, this equals
    `RomFile.md5_hash` (the largest-inner-file convention agreed); for
    NameOnly adoptions, the two diverge by definition (the user
    accepted that bytes don't match).

    `source_md5` borrows RomM's per-file `md5_hash` for consistency
    with the rest of state. The planner uses `source_updated_at` (not
    `source_md5`) for change detection, so a name-only adoption's
    "lying" source_md5 isn't load-bearing — and an eventual server-
    side update will refresh it on the first real sync.
    """
    rom_data = match.rom_data
    file_data = match.file_data
    abs_path = orphan.abs_path
    output_md5 = hash_file_bytes(abs_path)
    return RomState(
        rom_id=match.rom_id,
        platform_slug=str(rom_data.get("platform_slug") or "?"),
        name=str(rom_data.get("name") or rom_data.get("fs_name") or "?"),
        source_filename=resolve_local_filename(rom_data, logger=logger),
        source_md5=str(file_data.get("md5_hash") or ""),
        source_size=_safe_int(rom_data.get("fs_size_bytes")) or 0,
        source_updated_at=str(rom_data.get("updated_at") or ""),
        transforms=transforms_for_platform,
        outputs=(
            TransformedOutput(
                path=str(orphan.rel_path),
                md5=output_md5,
                size=abs_path.stat().st_size,
            ),
        ),
        primary_output_index=0,
        synced_at=now_iso or _now_iso(),
    )


def synthesize_state(
    confident: Confident,
    *,
    roms_base: Path,
    transforms_for_platform: tuple[str, ...],
    now_iso: str | None = None,
) -> RomState:
    """Build a `RomState` for a Confident match. Thin wrapper around
    `synthesize_state_from_match`; preserved for backward compat with
    callers that already hold a `Confident`."""
    return synthesize_state_from_match(
        confident.orphan,
        confident.match,
        transforms_for_platform=transforms_for_platform,
        now_iso=now_iso,
    )


def _safe_int(value: Any) -> int | None:
    """RomM API returns ints sometimes as strings; tolerate both."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
