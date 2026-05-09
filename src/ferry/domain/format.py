"""Pure presentation helpers — value → human-readable string.

No I/O, no CLI dependency. Both `cli/` (status / sync output) and
`services/` (sync_executor's progress messages) consume these.
"""

from __future__ import annotations


def format_bytes(n: int) -> str:
    """Human-friendly byte count with adaptive unit (B / KB / MB / GB / TB).

    Bytes render as integers; everything else with a single decimal.
    Saturates at TB so an unreasonably large input still renders something.
    """
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{int(n)} B"  # unreachable
