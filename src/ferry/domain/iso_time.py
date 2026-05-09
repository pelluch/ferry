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


def same_iso_instant(a: str | None, b: str | None) -> bool:
    """True iff *a* and *b* represent the same ISO-8601 instant (second precision).

    Use this — not lexical string compare — for "did this timestamp
    change?" checks. RomM's serialization isn't stable across endpoints:
    the rom-list endpoint truncates to seconds (`...T12:14:09+00:00`),
    while save POST/PUT responses keep microseconds
    (`...T12:14:09.123456+00:00`); the same instant via different
    serializations would otherwise compare unequal and cause spurious
    "to update" / "to upload" flags on every sync.

    Falls back to lexical equality when either string is unparseable —
    equivalent strings stay equivalent regardless of parse support, so
    None==None and ""=="" still report True.
    """
    if a == b:
        return True
    dt_a = parse_iso(a)
    dt_b = parse_iso(b)
    if dt_a is None or dt_b is None:
        return False
    return dt_a.replace(microsecond=0) == dt_b.replace(microsecond=0)
