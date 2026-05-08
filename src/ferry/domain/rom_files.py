"""Resolve the on-disk filename for a ROM from RomM's API shape.

For nested-single-file ROMs — a single file inside a per-game folder
on the RomM server (e.g. `roms/psx/Resident Evil/Resident Evil.chd`) —
RomM sets `has_nested_single_file=True`, returns the parent folder
name in `fs_name`, and the real filename (with extension) in
`files[0].file_name`. Treating `fs_name` as the local filename in that
case drops the extension and breaks launchers.

Lifted in spirit from decky-romm-sync's `services/downloads.py`
(GPLv3) per DESIGN.md §6, where this was originally tracked as
decky-romm-sync issue #226.
"""

from __future__ import annotations

import logging
import os
from typing import Any


def resolve_local_filename(
    rom_data: dict[str, Any],
    *,
    logger: logging.Logger,
) -> str:
    """Return the on-disk filename for a ROM described by *rom_data*.

    *rom_data* is one entry from RomM's `/api/roms` response. For most
    layouts `fs_name` is already the correct filename (with extension).
    For nested-single-file ROMs `fs_name` is the parent folder; the
    real filename lives in `files[0].file_name`. Path traversal in
    that field is sanitized via `os.path.basename`.

    Falls back to `fs_name` (or a synthetic `rom-<id>`) and logs a
    warning when `has_nested_single_file=True` but the `files` list is
    missing, empty, or malformed — preserves whatever filename ferry
    can construct so the sync still produces a file the user can
    inspect, even if its name is wrong.
    """
    rom_id = rom_data.get("id", "unknown")
    fs_name = rom_data.get("fs_name") or f"rom-{rom_id}"
    if not rom_data.get("has_nested_single_file"):
        return fs_name
    files = rom_data.get("files") or []
    if not files or not isinstance(files[0], dict):
        logger.warning(
            "rom_id=%s: has_nested_single_file=true but files list is empty; "
            "falling back to fs_name=%r",
            rom_id,
            fs_name,
        )
        return fs_name
    nested = files[0].get("file_name")
    if not isinstance(nested, str) or not nested:
        logger.warning(
            "rom_id=%s: has_nested_single_file=true but files[0].file_name is "
            "missing; falling back to fs_name=%r",
            rom_id,
            fs_name,
        )
        return fs_name
    return os.path.basename(nested)
