"""Tests for `domain.rom_files.resolve_local_filename`."""

from __future__ import annotations

import logging

from ferry.domain.rom_files import resolve_local_filename


def test_simple_single_file_uses_fs_name() -> None:
    """Most ROMs: `fs_name` is already the on-disk filename."""
    rom = {"id": 1, "fs_name": "Game.gba", "has_nested_single_file": False}
    assert resolve_local_filename(rom, logger=logging.getLogger("test")) == "Game.gba"


def test_missing_has_nested_flag_treats_as_simple() -> None:
    """Older RomM responses may not include the flag at all — treat as simple."""
    rom = {"id": 1, "fs_name": "Game.gba"}
    assert resolve_local_filename(rom, logger=logging.getLogger("test")) == "Game.gba"


def test_nested_single_file_uses_files_entry() -> None:
    """`fs_name` is the parent folder; real filename lives in `files[0].file_name`."""
    rom = {
        "id": 7,
        "fs_name": "Resident Evil",
        "has_nested_single_file": True,
        "files": [{"file_name": "Resident Evil.chd"}],
    }
    assert resolve_local_filename(rom, logger=logging.getLogger("test")) == "Resident Evil.chd"


def test_nested_single_file_empty_files_falls_back(caplog) -> None:
    """`has_nested_single_file=True` + empty list → fs_name fallback + warning."""
    rom = {
        "id": 8,
        "fs_name": "My Game",
        "has_nested_single_file": True,
        "files": [],
    }
    with caplog.at_level(logging.WARNING):
        result = resolve_local_filename(rom, logger=logging.getLogger("test"))
    assert result == "My Game"
    assert any("has_nested_single_file" in rec.message for rec in caplog.records)


def test_nested_single_file_missing_files_key_falls_back(caplog) -> None:
    """`has_nested_single_file=True` + no `files` key → fs_name fallback + warning."""
    rom = {
        "id": 9,
        "fs_name": "My Game",
        "has_nested_single_file": True,
        # no "files" key
    }
    with caplog.at_level(logging.WARNING):
        result = resolve_local_filename(rom, logger=logging.getLogger("test"))
    assert result == "My Game"
    assert any("has_nested_single_file" in rec.message for rec in caplog.records)


def test_nested_single_file_malformed_files_entry_falls_back(caplog) -> None:
    """`files[0]` not a dict (server returned weird shape) → fs_name fallback."""
    rom = {
        "id": 10,
        "fs_name": "My Game",
        "has_nested_single_file": True,
        "files": ["not-a-dict"],
    }
    with caplog.at_level(logging.WARNING):
        result = resolve_local_filename(rom, logger=logging.getLogger("test"))
    assert result == "My Game"
    assert any("has_nested_single_file" in rec.message for rec in caplog.records)


def test_nested_single_file_empty_filename_falls_back(caplog) -> None:
    """`files[0].file_name` empty string → fs_name fallback + warning."""
    rom = {
        "id": 11,
        "fs_name": "My Game",
        "has_nested_single_file": True,
        "files": [{"file_name": ""}],
    }
    with caplog.at_level(logging.WARNING):
        result = resolve_local_filename(rom, logger=logging.getLogger("test"))
    assert result == "My Game"
    assert any("has_nested_single_file" in rec.message for rec in caplog.records)


def test_nested_single_file_traversal_sanitized() -> None:
    """Path traversal in `files[0].file_name` is stripped via `os.path.basename`."""
    rom = {
        "id": 13,
        "fs_name": "Evil",
        "has_nested_single_file": True,
        "files": [{"file_name": "../../etc/evil.chd"}],
    }
    assert resolve_local_filename(rom, logger=logging.getLogger("test")) == "evil.chd"


def test_missing_fs_name_synthesizes_from_id() -> None:
    """Defensive: no `fs_name` AND simple-single-file → synthetic `rom-<id>`."""
    rom = {"id": 42}
    assert resolve_local_filename(rom, logger=logging.getLogger("test")) == "rom-42"
