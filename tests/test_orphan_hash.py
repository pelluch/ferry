"""Unit tests for `hash_orphan_file` — RomM's largest-inner-file convention.

Covers single-file zips, multi-file zips with a clear largest member,
tar/gz/bz2, malformed archives (fall back to file bytes), tie-break
behaviour, and missing/unreadable files. All tests are pure-domain
(no HTTP, no live RomM); the hash convention itself is what we're
verifying matches RomM's `_calculate_rom_hashes`.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

from ferry.adapters.orphan_hash import hash_orphan_file


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Non-archive files: direct md5 of bytes
# ---------------------------------------------------------------------------


def test_basic_file_hashes_bytes_directly(tmp_path: Path) -> None:
    target = tmp_path / "Pikmin.iso"
    payload = b"iso content " * 100
    target.write_bytes(payload)
    assert hash_orphan_file(target) == _md5(payload)


def test_streaming_handles_large_files(tmp_path: Path) -> None:
    target = tmp_path / "big.bin"
    chunk = b"\xab" * 64_000
    target.write_bytes(chunk * 4)  # ~256KB > internal 64KB chunk
    assert hash_orphan_file(target) == _md5(chunk * 4)


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert hash_orphan_file(tmp_path / "no-such.iso") is None


def test_directory_returns_none(tmp_path: Path) -> None:
    """Reconcile walks files; if a Path is somehow a dir, don't crash."""
    target = tmp_path / "dirnotfile"
    target.mkdir()
    assert hash_orphan_file(target) is None


# ---------------------------------------------------------------------------
# Single-file zips
# ---------------------------------------------------------------------------


def test_zip_with_single_member_hashes_inner_content(tmp_path: Path) -> None:
    """Pass-through cartridge platforms: `Pikmin.zip` containing `Pikmin.gba`."""
    inner = b"GBA ROM bytes" * 50
    archive = tmp_path / "Pikmin.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Pikmin.gba", inner)
    assert hash_orphan_file(archive) == _md5(inner)


def test_zip_with_compressed_member_still_hashes_uncompressed_bytes(
    tmp_path: Path,
) -> None:
    """RomM streams via `z.open(...)` which decompresses on the fly. We must
    match its decompressed-bytes hash, not the compressed bytes on disk."""
    inner = b"x" * 4096  # highly compressible
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("game.iso", inner)
    assert hash_orphan_file(archive) == _md5(inner)


# ---------------------------------------------------------------------------
# Multi-file zips: largest wins (the DOS-game case)
# ---------------------------------------------------------------------------


def test_multi_file_zip_picks_largest_member(tmp_path: Path) -> None:
    """The DOS-game pattern: zip contains many small files plus one big CD
    image. RomM hashes the CD image; ferry must match."""
    big = b"BIN" * 10_000  # 30 KB — clearly largest
    small1 = b"exe"
    small2 = b"cfg"
    archive = tmp_path / "USNavyFighters.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("USNFGOLD/run.bat", small1)
        z.writestr("USNFGOLD/USNF.EXE", small2)
        z.writestr("cd/USNFGOLD.bin", big)  # largest
    assert hash_orphan_file(archive) == _md5(big)


def test_zip_tie_breaks_to_first_member_in_listing(tmp_path: Path) -> None:
    """When two members share the maximum byte-size, `max` returns the first.
    Both ferry and RomM follow this rule, so they agree on tied archives."""
    payload_a = b"A" * 1000
    payload_b = b"B" * 1000  # identical size, different bytes
    archive = tmp_path / "tied.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.bin", payload_a)
        z.writestr("b.bin", payload_b)
    # First-in-listing wins on tie. payload_a is added first.
    assert hash_orphan_file(archive) == _md5(payload_a)


def test_zip_one_byte_heavier_wins_outright(tmp_path: Path) -> None:
    """Confirm `max` is on uncompressed size — a single byte difference picks the heavier."""
    archive = tmp_path / "heaviest.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.bin", b"x" * 1000)
        z.writestr("b.bin", b"y" * 1001)  # one byte heavier — wins
    assert hash_orphan_file(archive) == _md5(b"y" * 1001)


def test_empty_zip_falls_back_to_direct_bytes(tmp_path: Path) -> None:
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w"):
        pass  # no members
    assert hash_orphan_file(archive) == _md5(archive.read_bytes())


def test_bad_zip_falls_back_to_direct_bytes(tmp_path: Path) -> None:
    """Malformed `.zip`-named file: hash the raw bytes, matching RomM's
    `BadZipFile` fallback path."""
    archive = tmp_path / "fake.zip"
    payload = b"not actually a zip file"
    archive.write_bytes(payload)
    assert hash_orphan_file(archive) == _md5(payload)


# ---------------------------------------------------------------------------
# Tar / tar.gz
# ---------------------------------------------------------------------------


def test_tar_with_largest_inner_file(tmp_path: Path) -> None:
    big = b"B" * 4096
    archive = tmp_path / "test.tar"
    with tarfile.open(archive, "w") as tf:
        for name, data in (("small.txt", b"hi"), ("big.bin", big)):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    assert hash_orphan_file(archive) == _md5(big)


def test_tar_gz_with_largest_inner_file(tmp_path: Path) -> None:
    big = b"GZ" * 4096
    archive = tmp_path / "test.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, data in (("notes.txt", b"hi"), ("rom.iso", big)):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    assert hash_orphan_file(archive) == _md5(big)


def test_bare_gz_hashes_decompressed_content(tmp_path: Path) -> None:
    """A `.gz` that wraps a single file (not a tarball)."""
    inner = b"single-file gzipped content " * 10
    archive = tmp_path / "rom.iso.gz"
    with gzip.open(archive, "wb") as f:
        f.write(inner)
    assert hash_orphan_file(archive) == _md5(inner)


def test_bad_tar_falls_back_to_direct_bytes(tmp_path: Path) -> None:
    archive = tmp_path / "fake.tar"
    payload = b"not actually a tar"
    archive.write_bytes(payload)
    assert hash_orphan_file(archive) == _md5(payload)


# ---------------------------------------------------------------------------
# bz2
# ---------------------------------------------------------------------------


def test_bz2_hashes_decompressed_content(tmp_path: Path) -> None:
    inner = b"bz2 content " * 30
    archive = tmp_path / "rom.iso.bz2"
    with bz2.open(archive, "wb") as f:
        f.write(inner)
    assert hash_orphan_file(archive) == _md5(inner)


# ---------------------------------------------------------------------------
# Out-of-scope archive types fall through to direct bytes
# ---------------------------------------------------------------------------


def test_seven_zip_falls_through_to_direct_bytes(tmp_path: Path) -> None:
    """`.7z` is intentionally not supported in v1 reconcile (no py7zr dep).
    Hashing falls to direct bytes; the orphan won't hash-match RomM and
    will land in name-only territory."""
    archive = tmp_path / "rom.7z"
    payload = b"would-be 7z bytes"
    archive.write_bytes(payload)
    assert hash_orphan_file(archive) == _md5(payload)


def test_chd_falls_through_to_direct_bytes(tmp_path: Path) -> None:
    """`.chd` similarly punts; RomM uses an embedded SHA1 from the v5 header
    which would need a separate sha1-based path."""
    archive = tmp_path / "disc.chd"
    payload = b"fake chd content"
    archive.write_bytes(payload)
    assert hash_orphan_file(archive) == _md5(payload)
