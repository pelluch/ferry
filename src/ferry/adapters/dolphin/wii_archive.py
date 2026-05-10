"""Pack/unpack Dolphin saves as a single wrapper-prefixed zip + content-hash mirror.

Originally Wii-only (v3.6); v3.7 ck2 added the file-list helpers
(`archive_files`, `files_content_hash`) for the GameCube
per-rom-bundle archetype, where the source isn't a folder but an
ad-hoc set of `.gci` files matched across region subfolders / Card A
+ Card B. Both helpers share the same wrapper-prefix layout and
manifest-hash algorithm — the only difference is how they enumerate
their inputs. Module name kept as `wii_archive.py` for ck2; rename to
`dolphin_archive.py` is deferred to ck4 cleanup.

Wii saves at `<saves_root>/title/<TID_HIGH>/<TID_LOW>/` are folders
of small binaries (`data/save.bin`, `data/banner.bin`, plus `content/`
and any other subdirs Dolphin populates per title). RomM's `/api/saves`
takes one blob per save record, so ferry bundles the title parent as a
zip per save.

**Zip layout — single wrapping directory at root.** Argosy's
`unzipDirect` strips the first-level prefix unconditionally; a flat
zip would have its first sub-dir mistakenly stripped on the Argosy
side. Mirror Argosy's `zipFolderRecursive(folder, folder.name, zos)` —
prefix every entry with `<src_folder.name>/`. Wrapper name is
irrelevant to readers (gets stripped by name-anonymous prefix matching)
but its presence is required.

**The zip's bytes are deliberately NOT byte-stable.** We let Python's
`zipfile` defaults (mtimes from disk, external_attr from file mode,
ZIP_STORED for tiny files) do the talking. Hash matching across machines
goes through `compute_content_hash`, which mirrors RomM's
`assets_handler._compute_zip_hash` (sorted-name manifest of inner
content) AND Argosy's `SaveArchiver.calculateFolderAsZipHash`. Three
independent implementations converge on the same hash for the same
folder content — that three-way invariant is what enables cross-tool
dedup on RomM. See DESIGN.md §5.3 — `per_game_bundle` archetype.

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

from ferry.domain.hashing import md5_file
from ferry.transforms.unzip import is_unsafe_zip_member, is_within_dir

logger = logging.getLogger(__name__)

# Files OS file managers and macOS sprinkle into folders. Never part of
# Wii NAND state Dolphin wrote; safe to drop both on archive and on
# extract so a save zipped on one machine doesn't carry .DS_Store noise
# back onto another.
_IGNORED_NAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_IGNORED_DIR_NAMES: frozenset[str] = frozenset({"__MACOSX"})


def archive_save_folder(src_folder: Path, dest_zip: Path) -> None:
    """Build a zip of *src_folder* at *dest_zip*, recursive, with wrapping dir.

    Every entry's arcname is prefixed with `<src_folder.name>/` to
    produce the single-wrapping-dir layout Argosy's `unzipDirect`
    requires. Wrapper name itself is irrelevant — Argosy and ferry's
    `extract_save_zip` both strip whatever the first entry's top-level
    dir is — but its presence is required.

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
        if path.is_file() and not is_save_path_ignored(path.relative_to(src_folder))
    )
    wrapper = src_folder.name
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        for relpath in entries:
            arcname = f"{wrapper}/{relpath.as_posix()}"
            zf.write(src_folder / relpath, arcname=arcname)


def extract_save_zip(src_zip: Path, dest_folder: Path) -> None:
    """Extract *src_zip* into *dest_folder*, stripping the wrapping dir.

    Mirror of Argosy's `SaveArchiver.unzipDirect`: capture the first
    entry's top-level directory and strip that prefix from every
    subsequent entry. Wrapper name is irrelevant; presence is what
    matters. Entries that don't share the captured prefix are written
    as-is (matches Argosy's tolerance for mixed layouts).

    Idempotent: existing files at the same paths are overwritten.
    Ignored files (see `_IGNORED_NAMES` / `_IGNORED_DIR_NAMES`) are
    silently skipped on extract too — symmetric with archive — so a
    zip carrying `.DS_Store` from a misbehaving uploader can't pollute
    Dolphin's NAND tree. Refuses path-traversal entries up-front
    (`is_unsafe_zip_member` runs on the original entry name, before
    strip — catches `wrapper/../../escape` because `..` is in parts).
    """
    dest_folder.mkdir(parents=True, exist_ok=True)
    output_root = dest_folder.resolve()
    with zipfile.ZipFile(src_zip, "r") as zf:
        wrapper: str | None = None
        for info in zf.infolist():
            if info.is_dir():
                continue
            if is_unsafe_zip_member(info.filename):
                raise ValueError(
                    f"refusing to extract unsafe path from {src_zip.name}: {info.filename!r}"
                )
            # Cruft check runs on the ORIGINAL filename — otherwise a
            # `__MACOSX/save.bin` entry would set wrapper="__MACOSX",
            # then strip leaves "save.bin", and the cruft marker is
            # gone. Ignored check must see the path before strip.
            if is_save_path_ignored(Path(info.filename)):
                continue
            if wrapper is None and "/" in info.filename:
                wrapper = info.filename.split("/", 1)[0]
            relname = info.filename
            if wrapper is not None and relname.startswith(f"{wrapper}/"):
                relname = relname[len(wrapper) + 1 :]
            if not relname:
                continue
            target = (dest_folder / relname).resolve()
            if not is_within_dir(target, output_root):
                raise ValueError(
                    f"refusing to extract {info.filename!r} from {src_zip.name}: "
                    f"escapes destination directory"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_f, target.open("wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)


def folder_content_hash(src_folder: Path, *, wrapper: str | None = None) -> str:
    """Compute RomM-style content_hash directly from a folder.

    Equivalent to `compute_content_hash(archive_save_folder(folder))`
    by construction: same sorted-by-relpath traversal, same per-file
    md5, same `name:hash` join, same outer md5. Same dotfile filter.

    `wrapper` (default `src_folder.name`) is the wrapping-directory
    prefix that `archive_save_folder` adds to every entry's arcname.
    The manifest format includes that prefix per entry — so the hash
    matches what RomM computes on the wrapped zip and what Argosy's
    `calculateFolderAsZipHash` produces for the same folder. Pass an
    explicit wrapper for round-trip tests where the producing folder
    name differs from the canonical save folder.

    Used by the Wii walker so each `LocalSave.local_md5` matches what
    RomM would store on the corresponding zip upload — without paying
    the cost of zipping every walker iteration.
    """
    if wrapper is None:
        wrapper = src_folder.name
    entries = sorted(
        path.relative_to(src_folder)
        for path in src_folder.rglob("*")
        if path.is_file() and not is_save_path_ignored(path.relative_to(src_folder))
    )
    file_hashes = [
        f"{wrapper}/{relpath.as_posix()}:{md5_file(src_folder / relpath)}" for relpath in entries
    ]
    combined = "\n".join(file_hashes)
    return hashlib.md5(combined.encode(), usedforsecurity=False).hexdigest()


def archive_files(files: list[Path], dest_zip: Path, *, wrapper: str) -> None:
    """Build a wrapper-prefixed zip from an explicit list of source files.

    Sibling of `archive_save_folder` for the GameCube per-rom-bundle
    archetype: the source isn't a single folder, it's an ad-hoc set of
    `.gci` files matched across region subfolders / Card A + Card B.
    Each file lands at zip path `<wrapper>/<file.name>` (flat under
    the wrapper) — Argosy's GC bundle layout. Uses each file's basename
    only; duplicates in the input list trigger an early ValueError to
    catch caller-side bugs (the producing walker is responsible for
    de-duping; see Card A + Card B clash handling in `gamecube_saves`).

    Files are added in name-sorted order for stable test output.
    Otherwise the zip-byte non-determinism caveat from
    `archive_save_folder` applies — use `files_content_hash` for stable
    identity, never `md5_file(zip)`.
    """
    sorted_files = sorted(files, key=lambda p: p.name)
    seen: set[str] = set()
    for f in sorted_files:
        if f.name in seen:
            raise ValueError(f"archive_files: duplicate basename {f.name!r} — caller must dedupe")
        seen.add(f.name)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        for f in sorted_files:
            zf.write(f, arcname=f"{wrapper}/{f.name}")


def files_content_hash(files: list[Path], *, wrapper: str) -> str:
    """Compute RomM-style content_hash directly from a list of files.

    Equivalent to `compute_content_hash(archive_files(files, ...))` by
    construction: same name-sorted traversal, same per-file md5, same
    `<wrapper>/<name>:<hash>` join, same outer md5. Used by the GC
    walker so each `LocalSave.local_md5` matches what RomM (and Argosy)
    would compute on the corresponding bundle without paying the cost
    of zipping every walker iteration. Same dedup contract as
    `archive_files` — duplicate basenames raise.
    """
    sorted_files = sorted(files, key=lambda p: p.name)
    seen: set[str] = set()
    for f in sorted_files:
        if f.name in seen:
            raise ValueError(
                f"files_content_hash: duplicate basename {f.name!r} — caller must dedupe"
            )
        seen.add(f.name)
    file_hashes = [f"{wrapper}/{f.name}:{md5_file(f)}" for f in sorted_files]
    combined = "\n".join(file_hashes)
    return hashlib.md5(combined.encode(), usedforsecurity=False).hexdigest()


def compute_content_hash(zip_path: Path) -> str:
    """Mirror of RomM's `assets_handler._compute_zip_hash`.

    For each non-directory entry in name-sorted order:
        line = f"{name}:{md5(zf.read(name)).hexdigest()}"
    Returns md5("\\n".join(lines).encode()).hexdigest().

    Same content packed two different ways → same hash. Used as
    `LocalSave.local_md5` for Wii saves so it matches the server's
    `content_hash` exactly when content is unchanged, regardless of
    archive-byte drift.

    **Three-way invariant.** This function, RomM's
    `assets_handler._compute_zip_hash`, and Argosy's
    `SaveArchiver.calculateFolderAsZipHash` must all produce the same
    digest for the same folder content. That convergence is what makes
    cross-tool dedup possible on RomM. If any party changes the
    per-entry separator, sort key, or outer hash, classify-time hash
    equality silently breaks (falls through to mtime/last_sync_md5,
    still correct but degraded) and cross-tool dedup stops working.
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


def is_save_path_ignored(relpath: Path) -> bool:
    """True iff *relpath* (relative to a save folder root) should be skipped.

    Single source of truth for the dotfile filter shared between the
    archiver, the extractor, and the Wii walker. Anything classified
    as ignored here disappears from the content_hash, the zip, and the
    walker's size/mtime aggregates — symmetric handling everywhere.
    """
    if relpath.name in _IGNORED_NAMES:
        return True
    return any(part in _IGNORED_DIR_NAMES for part in relpath.parts)
