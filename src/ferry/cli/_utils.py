"""Shared CLI rendering helpers.

Tiny pure functions that several CLI command modules used to redefine
locally — `_mask`, `_path_status`, plus the dry-run preview cap.
Pulling them here keeps the per-command modules focused on their own
logic and avoids subtle drift between copies.

`format_bytes` lives in `ferry.domain.format` because `services/` calls
it too (sync_executor's progress messages); CLI code can re-export from
here for grouped imports.
"""

from __future__ import annotations

from pathlib import Path

from ferry.domain.format import format_bytes

__all__ = ["DEFAULT_PREVIEW", "format_bytes", "mask_token", "path_status"]

# Rows per section in dry-run / preview output before truncating with
# "... and N more". `--full` opts out. Conservative default keeps a
# typical sync's stdout to a single screen.
DEFAULT_PREVIEW = 20


def mask_token(token: str) -> str:
    """Show first 4 + last 3 chars of an API token, eliding the middle.

    Tokens shorter than 7 chars are reported as `(set)` — not enough
    surface to mask meaningfully, but the reader still needs to know
    a value is present.
    """
    if len(token) <= 6:
        return "(set)"
    return f"{token[:4]}…{token[-3:]}"


def path_status(path: Path) -> str:
    """Parenthesized one-word annotation for a configured path.

    `(missing)` — doesn't exist; `(not a directory)` — exists but is
    a file; `(exists)` — exists and is a directory. Used in `ferry
    ping` / `ferry status` / `ferry detect` output where the user
    wants a fast read on whether their config matches reality.
    """
    if not path.exists():
        return "(missing)"
    if not path.is_dir():
        return "(not a directory)"
    return "(exists)"
