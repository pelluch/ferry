"""Pure-domain logic for `ferry reconcile` — walk for orphan files,
classify them against a per-platform RomM index, synthesize `RomState`
entries for confident matches.

No HTTP, no sidecar I/O, no state.json writes — those live in
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

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ferry.adapters.orphan_hash import hash_file_bytes, hash_orphan_file
from ferry.adapters.sidecar import (
    SIDECAR_SUFFIX,
    sidecar_path_for,
)
from ferry.domain.state import LibraryState, RomState, TransformedOutput


@dataclass(frozen=True, slots=True)
class OrphanCandidate:
    """A local file with no tracked sidecar — a reconcile candidate."""

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
    sidecars_root: Path,
    state: LibraryState,
    platform_filter: str | None = None,
) -> list[OrphanCandidate]:
    """Walk `roms_base` and return files that aren't tracked by ferry.

    Excludes:
      - files tracked in `state.roms[*].outputs[*].path`,
      - files whose canonical sidecar exists under `sidecars_root`,
      - sidecar files themselves (`*.ferry.json`),
      - dotfiles (KDE droppings, legacy v2 sidecars that survived the
        migration sweep, etc.),
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
            if path.name.endswith(SIDECAR_SUFFIX):
                continue
            if path in tracked_paths:
                continue
            try:
                rel = path.relative_to(roms_base)
            except ValueError:  # symlinks pointing outside the tree
                continue
            if sidecar_path_for(path, roms_base=roms_base, sidecars_root=sidecars_root).exists():
                # Sidecar present but not in state — recovery will pick this
                # up next sync; reconcile shouldn't double-claim it.
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
) -> tuple[dict[str, list[MatchedFile]], dict[str, list[MatchedFile]]]:
    """Return `(by_name, by_hash)` maps over RomM's per-platform rom listing.

    Each rom in `roms` should have a `files: list[RomFileSchema]` field;
    we index every file twice — once by `file_name`, once by md5.
    Files lacking `md5_hash` (RomM hadn't computed one yet, or platform
    is in `NON_HASHABLE_PLATFORMS`) are still indexed by name so
    name-only classification still works for them.
    """
    by_name: dict[str, list[MatchedFile]] = {}
    by_hash: dict[str, list[MatchedFile]] = {}
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
            if md5:
                by_hash.setdefault(md5, []).append(mf)
    return by_name, by_hash


def classify(
    orphan: OrphanCandidate,
    by_name: dict[str, list[MatchedFile]],
    by_hash: dict[str, list[MatchedFile]],
) -> Classification:
    """Classify one orphan against a per-platform RomM index.

    Hashing follows RomM's "largest inner file of an archive" rule —
    see `adapters/orphan_hash.py:hash_orphan_file`. For non-archive
    files this collapses to a direct md5.
    """
    local_md5 = hash_orphan_file(orphan.abs_path)
    name_matches = by_name.get(orphan.abs_path.name, [])
    hash_matches = by_hash.get(local_md5, []) if local_md5 else []

    name_keys = {(m.rom_id, m.file_id) for m in name_matches}
    hash_keys = {(m.rom_id, m.file_id) for m in hash_matches}
    confident_keys = name_keys & hash_keys

    if confident_keys:
        confidents = [m for m in name_matches if (m.rom_id, m.file_id) in confident_keys]
        unique_rom_ids = {m.rom_id for m in confidents}
        if len(unique_rom_ids) > 1:
            return Ambiguous(orphan=orphan, matches=tuple(confidents), local_md5=local_md5 or "")
        # Exactly one rom matches; if multiple files within the same rom
        # both match, take the first (extremely rare — would imply RomM
        # has two files with identical name AND md5 inside the same rom).
        return Confident(orphan=orphan, match=confidents[0], local_md5=local_md5 or "")

    if name_matches:
        return NameOnly(orphan=orphan, candidates=tuple(name_matches), local_md5=local_md5)
    if hash_matches:
        return HashOnly(orphan=orphan, candidates=tuple(hash_matches), local_md5=local_md5 or "")
    return NoMatch(orphan=orphan, local_md5=local_md5)


# ---------------------------------------------------------------------------
# State synthesis
# ---------------------------------------------------------------------------


def synthesize_state(
    confident: Confident,
    *,
    roms_base: Path,
    transforms_for_platform: tuple[str, ...],
    now_iso: str | None = None,
) -> RomState:
    """Build a `RomState` for a confident orphan match, ready to persist.

    `output.md5` is the direct md5 of the local file bytes — what
    ferry's sync executor would have computed had it produced this
    file via download+pipeline. That's distinct from `confident.local_md5`,
    which mirrors RomM's largest-inner-file convention for archive
    pass-throughs.

    `source_md5` borrows RomM's per-file `md5_hash` (same as
    `confident.local_md5` after our match). It's not strictly the
    "ZIP bytes md5" the planner would have computed during a real
    download — but the planner only uses `source_updated_at` for
    change detection, so this is fine.
    """
    rom_data = confident.match.rom_data
    file_data = confident.match.file_data
    abs_path = confident.orphan.abs_path
    output_md5 = hash_file_bytes(abs_path)
    return RomState(
        rom_id=confident.match.rom_id,
        platform_slug=str(rom_data.get("platform_slug") or "?"),
        name=str(rom_data.get("name") or rom_data.get("fs_name") or "?"),
        source_filename=str(rom_data.get("fs_name") or ""),
        source_md5=str(file_data.get("md5_hash") or ""),
        source_size=_safe_int(rom_data.get("fs_size_bytes")) or 0,
        source_updated_at=str(rom_data.get("updated_at") or ""),
        transforms=transforms_for_platform,
        outputs=(
            TransformedOutput(
                path=str(confident.orphan.rel_path),
                md5=output_md5,
                size=abs_path.stat().st_size,
            ),
        ),
        primary_output_index=0,
        synced_at=now_iso or _now_iso(),
    )


def _safe_int(value: Any) -> int | None:
    """RomM API returns ints sometimes as strings; tolerate both."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
