"""Streaming file-hash helpers.

Five sites used to roll their own md5-of-a-file: each wrote a few lines
opening the file, looping `read(64*1024)` chunks, and updating the
hasher. Half passed `usedforsecurity=False` (correct for integrity
hashes — works on FIPS-mode hosts where MD5 is otherwise blocked); the
other half didn't. One site used sha256 with the same shape.

Pure stdlib, no I/O above the file read. `usedforsecurity=False` is
hardcoded for both helpers — these are integrity hashes (RomM's
content_hash, our state.json output hash, the launch-hooks bundled-XML
fingerprint), never authentication.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import IO

# 64 KB strikes a balance: large enough that per-chunk overhead is
# negligible against the underlying read syscall, small enough that
# memory residence stays bounded for any concurrent hashing.
CHUNK_SIZE = 64 * 1024


def md5_file(path: Path) -> str:
    """Streaming MD5 hex digest of *path*'s bytes."""
    h = hashlib.md5(usedforsecurity=False)
    _update_from_file(h, path)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 hex digest of *path*'s bytes."""
    h = hashlib.sha256(usedforsecurity=False)
    _update_from_file(h, path)
    return h.hexdigest()


def md5_stream(stream: IO[bytes]) -> str:
    """Streaming MD5 hex digest of an open binary stream.

    The caller owns the stream's lifecycle (open / close); we only
    read until EOF.
    """
    h = hashlib.md5(usedforsecurity=False)
    for chunk in _iter_chunks(stream):
        h.update(chunk)
    return h.hexdigest()


def _update_from_file(h, path: Path) -> None:
    with path.open("rb") as f:
        for chunk in _iter_chunks(f):
            h.update(chunk)


def _iter_chunks(stream: IO[bytes]) -> Iterable[bytes]:
    while chunk := stream.read(CHUNK_SIZE):
        yield chunk
