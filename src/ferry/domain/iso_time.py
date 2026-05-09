"""ISO-8601 timestamp parsing + formatting helpers.

Lifted in spirit from decky-romm-sync's `py_modules/lib/iso_time.py`
(GPL-3.0-only) per DESIGN.md §6. Simplified for Python 3.12+: the
upstream module's `replace("Z", "+00:00")` defensive normalisation is
redundant on Python 3.11+ (`datetime.fromisoformat` handles a trailing
`Z` natively), so ferry calls `fromisoformat` directly.

Layer-agnostic: pure stdlib, no I/O. Both `domain` and `services`
modules can import from here.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware datetime, or None on failure.

    Returns None for empty/None input or any parse failure — the caller
    decides how to interpret that (skip, treat as unchanged, etc.).
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def parse_iso_to_epoch(value: str | None) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds, or None on failure.

    Use this — not lexical string compare — when ranking timestamps. RomM
    happens to serve UTC consistently today, but mixed offsets (`Z` vs
    `+02:00`) sort wrong lexically while representing valid same-instant
    timestamps.
    """
    dt = parse_iso(value)
    return dt.timestamp() if dt is not None else None


def now_iso() -> str:
    """Current UTC instant as `YYYY-MM-DDTHH:MM:SSZ` (second precision, `Z` suffix).

    The canonical "wrote-this-now" timestamp ferry stamps onto records
    (state.json `synced_at`, save record `last_synced_at`, launch-hooks
    snapshot `installed_at`, etc.). Lossless under round-trip through
    `parse_iso`.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
