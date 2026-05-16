"""Tests for `ferry.adapters.dolphin.dolphin_archive`.

The load-bearing property is `compute_content_hash`: same content
packed two different ways must produce the same hash, because that's
what makes ferry's classify-time hash compare against RomM's
`content_hash` work without us fighting zip-byte determinism. See
`test_compute_content_hash_invariant_to_archive_bytes` and
`test_compute_content_hash_invariant_to_entry_order`.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from pathlib import Path

import pytest

from ferry.adapters.dolphin.dolphin_archive import (
    archive_save_folder,
    compute_content_hash,
    extract_save_zip,
    folder_content_hash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate(folder: Path, files: dict[str, bytes]) -> None:
    """Create *files* under *folder*; intermediate dirs are created."""
    for relpath, content in files.items():
        target = folder / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _read_tree(folder: Path) -> dict[str, bytes]:
    """Inverse of `_populate`: relpath → bytes for every file under *folder*."""
    return {
        str(p.relative_to(folder).as_posix()): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# archive_save_folder + extract_save_zip
# ---------------------------------------------------------------------------


def test_archive_extract_round_trip_preserves_content(tmp_path: Path) -> None:
    src = tmp_path / "src"
    files = {
        "save.bin": b"main save bytes",
        "banner.bin": b"\x89PNG\x00\x01\x02banner",
        "nested/extra.dat": b"nested content",
    }
    _populate(src, files)

    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)

    dest = tmp_path / "extracted"
    extract_save_zip(archive, dest)

    assert _read_tree(dest) == files


def test_archive_skips_dotfiles(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _populate(
        src,
        {
            "save.bin": b"real save",
            ".DS_Store": b"mac cruft",
            "Thumbs.db": b"windows cruft",
            "desktop.ini": b"windows ini",
            "__MACOSX/save.bin": b"macosx shadow",
            "subdir/__MACOSX/x": b"deeper macosx shadow",
        },
    )

    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    # Every entry is prefixed with the source folder name (wrapper dir
    # required for Argosy compat).
    assert names == {"src/save.bin"}


def test_archive_wraps_every_entry_with_source_folder_name(tmp_path: Path) -> None:
    """Argosy's `unzipDirect` strips the first-level dir unconditionally;
    a flat zip would have its first sub-dir mistakenly stripped on the
    Argosy side. Wrapper presence is what matters."""
    src = tmp_path / "RSPE"  # mimics a Wii TID_LOW folder
    _populate(
        src,
        {
            "data/save.bin": b"actual state",
            "data/banner.bin": b"banner",
            "content/title.tmd": b"vc title content",
            "top.txt": b"depth-0 file",
        },
    )

    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)

    with zipfile.ZipFile(archive) as zf:
        names = sorted(zf.namelist())
    assert names == [
        "RSPE/content/title.tmd",
        "RSPE/data/banner.bin",
        "RSPE/data/save.bin",
        "RSPE/top.txt",
    ]


def test_extract_skips_dotfiles_in_received_archive(tmp_path: Path) -> None:
    """Archives uploaded by misbehaving clients shouldn't pollute NAND."""
    archive = tmp_path / "in.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("save.bin", b"real")
        zf.writestr(".DS_Store", b"cruft")
        zf.writestr("__MACOSX/save.bin", b"shadow")

    dest = tmp_path / "out"
    extract_save_zip(archive, dest)

    assert _read_tree(dest) == {"save.bin": b"real"}


def test_archive_handles_nested_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _populate(
        src,
        {
            "a/b/c.bin": b"deeply nested",
            "a/sibling.bin": b"sibling at depth 1",
            "top.bin": b"shallow",
        },
    )

    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)
    dest = tmp_path / "extracted"
    extract_save_zip(archive, dest)

    assert _read_tree(dest) == _read_tree(src)


def test_archive_empty_folder_produces_empty_zip(tmp_path: Path) -> None:
    src = tmp_path / "empty"
    src.mkdir()

    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)

    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == []


def test_extract_refuses_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.bin", b"oops")

    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="unsafe path"):
        extract_save_zip(archive, dest)


def test_extract_refuses_post_strip_traversal(tmp_path: Path) -> None:
    """Wrapper-strip happens after the unsafe-member check, but a
    malicious zip could still try `wrapper/../../escape` — the leading
    `..` lives in the entry's path parts, so `is_unsafe_zip_member`
    catches it before the wrapper is even captured."""
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("wrapper/../../escape.bin", b"oops")

    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="unsafe path"):
        extract_save_zip(archive, dest)


def test_extract_strips_arbitrary_wrapper_name(tmp_path: Path) -> None:
    """Argosy's `unzipDirect` strips whatever the first entry's
    top-level dir is — wrapper name is irrelevant. Mirror that: a zip
    produced by some other tool with a different wrapper name should
    still extract cleanly, with the wrapper stripped."""
    archive = tmp_path / "argosy-style.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        # Wrapper name doesn't match any Wii TID — could be anything.
        zf.writestr("Wii Sports/data/save.bin", b"actual save")
        zf.writestr("Wii Sports/data/banner.bin", b"banner")
        zf.writestr("Wii Sports/content/empty.placeholder", b"")

    dest = tmp_path / "out"
    extract_save_zip(archive, dest)

    assert _read_tree(dest) == {
        "data/save.bin": b"actual save",
        "data/banner.bin": b"banner",
        "content/empty.placeholder": b"",
    }


def test_extract_handles_flat_zip_without_wrapper(tmp_path: Path) -> None:
    """Defensive: a zip with no top-level dir (flat entries) gets no
    prefix stripped. Argosy's `unzipDirect` does the same — wrapper is
    only captured when the first entry contains '/'."""
    archive = tmp_path / "flat.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("save.bin", b"flat save")
        zf.writestr("banner.bin", b"flat banner")

    dest = tmp_path / "out"
    extract_save_zip(archive, dest)

    assert _read_tree(dest) == {
        "save.bin": b"flat save",
        "banner.bin": b"flat banner",
    }


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------


def _expected_romm_hash(entries: dict[str, bytes]) -> str:
    """Recompute RomM's `_compute_zip_hash` independently of our code.

    Lifted from `assets_handler.py:78-87`. Used as the golden reference
    so a drift in either side would surface here.
    """
    file_hashes = []
    for name in sorted(entries):
        content_hash = hashlib.md5(entries[name], usedforsecurity=False).hexdigest()
        file_hashes.append(f"{name}:{content_hash}")
    combined = "\n".join(file_hashes)
    return hashlib.md5(combined.encode(), usedforsecurity=False).hexdigest()


def test_compute_content_hash_matches_romm_algorithm(tmp_path: Path) -> None:
    """Golden test: ferry's hash matches a hand-computed RomM-style hash."""
    entries = {
        "save.bin": b"main save",
        "banner.bin": b"banner bytes",
        "nested/x.dat": b"deep",
    }
    archive = tmp_path / "in.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)

    assert compute_content_hash(archive) == _expected_romm_hash(entries)


def test_compute_content_hash_invariant_to_archive_bytes(tmp_path: Path) -> None:
    """The whole point: zip-byte drift doesn't change content_hash."""
    entries = {
        "save.bin": b"identical content",
        "banner.bin": b"another file",
    }

    # Archive A: written via archive_save_folder (default mtimes etc.).
    src = tmp_path / "src"
    _populate(src, entries)
    archive_a = tmp_path / "a.zip"
    archive_save_folder(src, archive_a)

    # Force a different mtime on the source so a re-archive picks up
    # a different DOS time field. Then archive B with explicit mtimes
    # set to a fixed point in the past, simulating a different machine.
    # Use the same wrapping prefix as archive_save_folder so the hash
    # input is the same regardless of zip-byte differences.
    time.sleep(0.01)  # avoid stat-mtime equality
    archive_b = tmp_path / "b.zip"
    with zipfile.ZipFile(archive_b, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            info = zipfile.ZipInfo(f"src/{name}", date_time=(2000, 6, 15, 12, 0, 0))
            info.external_attr = 0o600 << 16
            zf.writestr(info, content)

    assert archive_a.read_bytes() != archive_b.read_bytes(), (
        "test setup is wrong: archives are accidentally byte-identical"
    )
    assert compute_content_hash(archive_a) == compute_content_hash(archive_b)


def test_compute_content_hash_changes_when_content_changes(tmp_path: Path) -> None:
    archive_a = tmp_path / "a.zip"
    with zipfile.ZipFile(archive_a, "w") as zf:
        zf.writestr("save.bin", b"version 1")

    archive_b = tmp_path / "b.zip"
    with zipfile.ZipFile(archive_b, "w") as zf:
        zf.writestr("save.bin", b"version 2")

    assert compute_content_hash(archive_a) != compute_content_hash(archive_b)


def test_compute_content_hash_invariant_to_entry_order(tmp_path: Path) -> None:
    """RomM sorts inner entries by name; entry add-order shouldn't matter."""
    archive_a = tmp_path / "a.zip"
    with zipfile.ZipFile(archive_a, "w") as zf:
        zf.writestr("a.bin", b"first")
        zf.writestr("b.bin", b"second")

    archive_b = tmp_path / "b.zip"
    with zipfile.ZipFile(archive_b, "w") as zf:
        zf.writestr("b.bin", b"second")
        zf.writestr("a.bin", b"first")

    assert compute_content_hash(archive_a) == compute_content_hash(archive_b)


def test_compute_content_hash_ignores_directory_entries(tmp_path: Path) -> None:
    """Some zip writers emit explicit dir entries (`name/`); RomM skips
    them, so we must too — otherwise an archive with explicit dir
    entries would hash differently from one without."""
    archive_a = tmp_path / "a.zip"
    with zipfile.ZipFile(archive_a, "w") as zf:
        zf.writestr("subdir/", b"")  # directory entry
        zf.writestr("subdir/file.bin", b"content")

    archive_b = tmp_path / "b.zip"
    with zipfile.ZipFile(archive_b, "w") as zf:
        zf.writestr("subdir/file.bin", b"content")

    assert compute_content_hash(archive_a) == compute_content_hash(archive_b)


def test_compute_content_hash_empty_zip(tmp_path: Path) -> None:
    """Empty zip → md5 of empty string."""
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w"):
        pass
    expected = hashlib.md5(b"", usedforsecurity=False).hexdigest()
    assert compute_content_hash(archive) == expected


# ---------------------------------------------------------------------------
# folder_content_hash
# ---------------------------------------------------------------------------


def test_folder_content_hash_matches_compute_content_hash(tmp_path: Path) -> None:
    """The load-bearing equivalence: hashing a folder directly produces
    the same hex string as hashing the zip we'd build from it. Lets
    the walker emit `LocalSave.local_md5` values that match RomM's
    server-side `content_hash` without ever materializing a zip."""
    src = tmp_path / "src"
    _populate(
        src,
        {
            "save.bin": b"main save content",
            "banner.bin": b"banner png bytes",
            "nested/extra.dat": b"deep state",
            "a/b/c/very/deep.bin": b"the deepest",
        },
    )

    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)

    assert folder_content_hash(src) == compute_content_hash(archive)


def test_folder_content_hash_skips_dotfiles(tmp_path: Path) -> None:
    """Same dotfile filter as `archive_save_folder` — otherwise
    a folder with `.DS_Store` would hash differently from its zip,
    breaking the equivalence. Pin wrapper to isolate the dotfile
    filter from the folder-name-as-wrapper default."""
    src = tmp_path / "src"
    _populate(
        src,
        {
            "save.bin": b"real",
            ".DS_Store": b"cruft",
            "__MACOSX/save.bin": b"shadow",
            "Thumbs.db": b"more cruft",
        },
    )
    cleaned = tmp_path / "cleaned"
    _populate(cleaned, {"save.bin": b"real"})

    assert folder_content_hash(src, wrapper="W") == folder_content_hash(cleaned, wrapper="W")


def test_folder_content_hash_changes_when_content_changes(tmp_path: Path) -> None:
    src_a = tmp_path / "a"
    _populate(src_a, {"save.bin": b"version 1"})
    src_b = tmp_path / "b"
    _populate(src_b, {"save.bin": b"version 2"})

    # Pin wrapper so the assertion isolates content change from
    # folder-name change (which would also flip the hash).
    assert folder_content_hash(src_a, wrapper="W") != folder_content_hash(src_b, wrapper="W")


def test_folder_content_hash_empty_folder(tmp_path: Path) -> None:
    src = tmp_path / "empty"
    src.mkdir()
    expected = hashlib.md5(b"", usedforsecurity=False).hexdigest()
    # Wrapper irrelevant for empty folder — manifest has no entries.
    assert folder_content_hash(src) == expected


def test_folder_content_hash_changes_when_wrapper_changes(tmp_path: Path) -> None:
    """The wrapper name participates in the manifest — same content
    under a different wrapper hashes differently. Catches regressions
    where the wrapper accidentally drops out of the manifest."""
    src = tmp_path / "src"
    _populate(src, {"save.bin": b"identical content"})

    assert folder_content_hash(src, wrapper="alpha") != folder_content_hash(src, wrapper="beta")


def test_folder_content_hash_default_wrapper_is_folder_name(tmp_path: Path) -> None:
    """Verify the default — `archive_save_folder` uses the source
    folder's name as wrapper, and `folder_content_hash` must match
    that when no explicit wrapper is passed."""
    src = tmp_path / "RSPE"
    _populate(src, {"save.bin": b"content"})

    archive = tmp_path / "out.zip"
    archive_save_folder(src, archive)

    # Default wrapper (= src.name = "RSPE") must produce the same hash
    # as compute_content_hash on the wrapper-prefixed zip.
    assert folder_content_hash(src) == compute_content_hash(archive)
    # And explicitly: passing wrapper="RSPE" matches the default.
    assert folder_content_hash(src) == folder_content_hash(src, wrapper="RSPE")
