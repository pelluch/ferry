"""Pack/unpack a Wii NAND save folder as a single zip + content-hash mirror.

Wii saves at `<saves_root>/title/<TID_HIGH>/<TID_LOW>/data/` are folders
of small binaries (`save.bin`, `banner.bin`, sometimes nested subdirs
for game-specific data). RomM's `/api/saves` takes one blob per save
record, so ferry bundles the folder as a zip per save.

**The zip's bytes are deliberately NOT byte-stable.** We let Python's
`zipfile` defaults (mtimes from disk, external_attr from file mode,
ZIP_STORED for tiny files) do the talking. Hash matching across machines
goes through `compute_content_hash`, which mirrors RomM's
`assets_handler._compute_zip_hash` (sorted-name manifest of inner
content), making archive-byte determinism unnecessary. See DESIGN.md
§5.3 — `per_game_bundle` archetype.

`compute_content_hash` is the ONLY identity function for these archives.
A future contributor reaching for `md5_file(zip)` thinking it's stable
would be wrong; the docstring on `archive_save_folder` flags this.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import zipfile
from pathlib import Path

from ferry.transforms.unzip import is_unsafe_zip_member, is_within_dir

logger = logging.getLogger(__name__)

# Files OS file managers and macOS sprinkle into folders. Never part of
# Wii NAND state Dolphin wrote; safe to drop both on archive and on
# extract so a save zipped on one machine doesn't carry .DS_Store noise
# back onto another.
_IGNORED_NAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_IGNORED_DIR_NAMES: frozenset[str] = frozenset({"__MACOSX"})


def archive_save_folder(src_folder: Path, dest_zip: Path) -> None:
    """Build a zip of *src_folder* at *dest_zip*, recursive.

    Entries are added in relpath-sorted order for stable test output.
    OS-cruft files (`.DS_Store`, `Thumbs.db`, `desktop.ini`) and
    `__MACOSX/` subtrees are skipped. Otherwise Python's `zipfile`
    defaults apply: mtimes from disk, external_attr from file mode,
    ZIP_STORED.

    The resulting bytes are NOT guaranteed identical across machines or
    Python versions. Use `compute_content_hash` for stable identity.
    """
    entries = sorted(
        path.relative_to(src_folder)
        for path in src_folder.rglob("*")
        if path.is_file() and not _is_ignored(path.relative_to(src_folder))
    )
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        for relpath in entries:
            zf.write(src_folder / relpath, arcname=relpath.as_posix())


def extract_save_zip(src_zip: Path, dest_folder: Path) -> None:
    """Extract *src_zip* into *dest_folder*, creating dirs as needed.

    Idempotent: existing files at the same paths are overwritten.
    Ignored files (see `_IGNORED_NAMES` / `_IGNORED_DIR_NAMES`) are
    silently skipped on extract too — symmetric with archive — so a
    zip carrying `.DS_Store` from a misbehaving uploader can't pollute
    Dolphin's NAND tree. Refuses path-traversal entries.
    """
    dest_folder.mkdir(parents=True, exist_ok=True)
    output_root = dest_folder.resolve()
    with zipfile.ZipFile(src_zip, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if is_unsafe_zip_member(info.filename):
                raise ValueError(
                    f"refusing to extract unsafe path from {src_zip.name}: {info.filename!r}"
                )
            if _is_ignored(Path(info.filename)):
                continue
            target = (dest_folder / info.filename).resolve()
            if not is_within_dir(target, output_root):
                raise ValueError(
                    f"refusing to extract {info.filename!r} from {src_zip.name}: "
                    f"escapes destination directory"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_f, target.open("wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)


def compute_content_hash(zip_path: Path) -> str:
    """Mirror of RomM's `assets_handler._compute_zip_hash`.

    For each non-directory entry in name-sorted order:
        line = f"{name}:{md5(zf.read(name)).hexdigest()}"
    Returns md5("\\n".join(lines).encode()).hexdigest().

    Same content packed two different ways → same hash. Used as
    `LocalSave.local_md5` for Wii saves so it matches the server's
    `content_hash` exactly when content is unchanged, regardless of
    archive-byte drift.

    Must stay in lockstep with RomM's algorithm — if RomM ever changes
    the per-entry separator, sort key, or outer hash, ferry's
    classify-time hash equality silently breaks (falls through to
    mtime/last_sync_md5, still correct but degraded).
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        file_hashes: list[str] = []
        for name in sorted(zf.namelist()):
            if name.endswith("/"):
                continue
            content = zf.read(name)
            file_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
            file_hashes.append(f"{name}:{file_hash}")
    combined = "\n".join(file_hashes)
    return hashlib.md5(combined.encode(), usedforsecurity=False).hexdigest()


def _is_ignored(relpath: Path) -> bool:
    """True iff *relpath* (relative to a save folder root) should be skipped."""
    if relpath.name in _IGNORED_NAMES:
        return True
    return any(part in _IGNORED_DIR_NAMES for part in relpath.parts)
