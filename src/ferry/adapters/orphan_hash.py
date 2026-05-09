"""Compute the identifying hash of a local file the way RomM does.

When ferry's `reconcile` flow walks the ROM tree for orphan files
(not in state.json), it needs to compute an md5 it can
match against `RomFileSchema.md5_hash` from RomM's API. RomM's
hashing convention (see `selfhosted/romm/backend/handler/filesystem/
roms_handler.py:_calculate_rom_hashes`) is:

  - Archive (.zip / .tar / .tar.gz / .gz / .bz2): hash the LARGEST
    file inside, by uncompressed size. The "largest" rule is uniform
    across all platforms, including DOS multi-file zips — RomM never
    hashes the archive-as-blob and never aggregates inner-file
    hashes. Tie-breaking (rare) defers to the first-in-listing
    member, matching `max(...)`'s behavior.
  - Non-archive: streaming md5 of the file's bytes.

ferry mirrors this rule exactly so that local files match
`RomFileSchema.md5_hash` without server-side cooperation.

Scope cuts (intentional, to keep ck1 tight):

  - `.7z` not supported. RomM uses `py7zr` via `utils.archive_7zip`;
    ferry doesn't have a 7z dependency today and adding one for
    reconcile alone is over-scoped. Files with `.7z` extension fall
    through to direct-bytes hashing, which won't match RomM's
    inner-file hash — those orphans drop into the name-only category
    and the user can opt in via `--include-name-only`.
  - `.chd` not supported. RomM uses the embedded SHA1 from the v5
    CHD header (not md5), so hash-matching CHDs needs a separate
    sha1-based path. Future work; for v1 reconcile, CHDs land in
    name-only territory.
  - Bad-archive fallback mirrors RomM: if `zipfile.ZipFile` /
    `tarfile.open` raise, hash the file bytes directly. Matches RomM
    exactly and avoids surprising "no hash" failures on borderline
    archives.
"""

from __future__ import annotations

import bz2
import gzip
import logging
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import IO

from ferry.domain.hashing import md5_file, md5_stream

logger = logging.getLogger(__name__)


def hash_file_bytes(path: Path) -> str:
    """Streaming md5 of the file bytes — direct, no archive interpretation.

    Used by `reconcile` to compute the `output.md5` field that ferry's
    sync executor stores after a successful download. Distinct from
    `hash_orphan_file` because for archive-shaped pass-through ROMs
    (cartridge `.zip`s), the matching hash (largest-inner-file) and
    the stored output hash (zip-bytes md5) are different — adoption
    needs both.
    """
    return md5_file(path)


def hash_orphan_file(path: Path) -> str | None:
    """Return the md5 hex string ferry should use to match against RomM.

    Dispatches by extension first, then by archive-detection on the
    bytes when extensions are ambiguous (e.g., a `.zip` that's
    actually a `.tar.gz` rename — caller-side error, but we tolerate
    it). Non-archive files and unsupported archive types fall through
    to direct byte hashing.

    Returns None only on filesystem errors (permission denied, file
    disappeared mid-walk). Bad archives are NOT None — they fall back
    to direct byte hashing per RomM's `BadZipFile` / `ReadError`
    behaviour.
    """
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".zip":
            return _hash_largest_zip_member(path)
        if suffix in {".tar", ".tgz"} or _is_tar_gz_compound(path):
            return _hash_largest_tar_member(path, mode="r:*")
        if suffix == ".gz":
            return _hash_gz_or_largest_tar(path)
        if suffix == ".bz2":
            return _hash_decompressed(path, bz2.open)
        return md5_file(path)
    except OSError as e:
        logger.warning("could not hash orphan %s: %s", path, e)
        return None


def _is_tar_gz_compound(path: Path) -> bool:
    """`.tar.gz`, `.tar.bz2`, `.tar.xz` — match the compound suffix."""
    name = path.name.lower()
    return name.endswith((".tar.gz", ".tar.bz2", ".tar.xz"))


# ---------------------------------------------------------------------------
# Archive helpers — three steps with shared shape:
#   1. Open the archive (may raise the format's bad-archive exception).
#   2. Pick the largest regular member; fall back if the archive is empty
#      or the member can't be opened.
#   3. Hash the member's stream.
# `_hash_largest_member` and `_hash_decompressed` factor steps 2+3; the
# per-format helpers handle step 1 + the bad-archive exception.
# ---------------------------------------------------------------------------


def _hash_largest_member[M](
    path: Path,
    *,
    members: list[M],
    size_of: Callable[[M], int],
    open_member: Callable[[M], IO[bytes] | None],
    fallback: Callable[[Path], str] = md5_file,
) -> str:
    """Hash the largest member, falling back when the archive is empty
    or the member can't be opened (tarfile returns None for special
    files, links, etc.)."""
    if not members:
        return fallback(path)
    largest = max(members, key=size_of)
    stream = open_member(largest)
    if stream is None:
        return fallback(path)
    with stream as fp:
        return md5_stream(fp)


def _hash_decompressed(
    path: Path, opener: Callable[..., IO[bytes]], *, fallback: Callable[[Path], str] = md5_file
) -> str:
    """md5 of bytes from a single-file decompressor (gzip, bz2). Falls
    back to direct file bytes when the decompressor raises."""
    try:
        with opener(path, "rb") as f:
            return md5_stream(f)
    except (OSError, EOFError):
        return fallback(path)


def _hash_largest_zip_member(path: Path) -> str:
    """Mirror of RomM's `read_zip_file`. Falls back to direct bytes on bad zips."""
    try:
        with zipfile.ZipFile(path, "r") as z:
            return _hash_largest_member(
                path,
                members=z.infolist(),
                size_of=lambda m: m.file_size,
                open_member=lambda m: z.open(m, "r"),
            )
    except zipfile.BadZipFile:
        return md5_file(path)


def _hash_largest_tar_member(
    path: Path,
    *,
    mode: str,
    fallback: Callable[[Path], str] = md5_file,
) -> str:
    """Mirror of RomM's `read_tar_file`. Largest regular file by uncompressed size."""
    try:
        with tarfile.open(path, mode) as t:  # type: ignore[arg-type]
            return _hash_largest_member(
                path,
                members=[m for m in t.getmembers() if m.isfile()],
                size_of=lambda m: m.size,
                open_member=t.extractfile,
                fallback=fallback,
            )
    except tarfile.ReadError:
        return fallback(path)


def _hash_gz_or_largest_tar(path: Path) -> str:
    """`.gz` is RomM's `read_gz_file`, which is `read_tar_file(..., 'r:gz')`.

    A bare `.gz` (no inner tar) raises `tarfile.ReadError`; we catch
    and stream the gunzipped bytes directly — that's what RomM's
    fallback does in practice for plain-`.gz` ROMs.
    """
    return _hash_largest_tar_member(
        path, mode="r:gz", fallback=lambda p: _hash_decompressed(p, gzip.open)
    )
