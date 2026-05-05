"""Pure save-file conflict detection and resolution logic.

No I/O, no service/adapter imports. Functions are stateless and operate
only on values passed in.

Lifted from decky-romm-sync's `py_modules/domain/save_conflicts.py`
(GPLv3) per DESIGN.md §6, attribution per GPL §5a. Adapted for ferry:

- Primitive args (timestamps + sizes + hashes), not loose dicts — ferry's
  call sites already have typed dataclasses, so the dict-shaped upstream
  signatures would just unpack-then-rewrap. Cleaner to require the caller
  to extract.
- Dropped `ask_me`/`always_upload`/`always_download` resolution modes —
  v2 ships with `prefer-newer` only (DESIGN.md §5.3). Future modes can
  be added behind a config knob without touching this primitive layer.
- Dropped the `SaveConflict` dataclass and the `build_conflict_dict`
  helper — those exist for Decky's React-frontend prompt UI. Ferry's
  conflict descriptor shape is deferred to the SaveBackend checkpoint
  where we know what the protocol surfaces.
- Renamed for terseness: `check_local_changes` → `local_changed`,
  `check_server_changes_fast` → `server_changed_fast`,
  `resolve_conflict_by_mode` → `resolve_newest`.
- `resolve_newest` returns `"ambiguous"` (not `"ask"`) when the local
  and server timestamps are within the clock-skew tolerance — the term
  is UI-neutral: a CLI caller treats it as skip-with-warning, a future
  UI caller treats it as prompt-the-user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

Action = Literal["skip", "upload", "download", "conflict"]
Resolution = Literal["upload", "download", "ambiguous"]
ClassifiedAction = Literal["upload", "download", "skip", "ambiguous", "drop_prior"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Classification:
    """The full per-key disposition produced by `classify`.

    Pure value type — no side effects. Callers of `classify` apply the
    counters/messages themselves (the executing backend bumps
    `result.conflicts_resolved`; the planner records `reason` for
    display in dry-run output).

    `reason` is a short human-readable phrase suitable for dry-run
    listings. `conflict_resolved` is True iff this action emerged from
    newest-wins on a real conflict (used to count
    `conflicts_resolved`). `ambiguous_message` is set when the action
    is `"ambiguous"`, carrying the explanation the CLI surfaces.
    """

    action: ClassifiedAction
    reason: str
    conflict_resolved: bool = False
    ambiguous_message: str | None = None


def classify(
    *,
    local_md5: str | None,
    local_mtime: float | None,
    local_save_filename: str | None,
    server_md5: str | None,
    server_size: int | None,
    server_updated_at: str | None,
    last_sync_md5: str | None,
    last_sync_server_size: int | None,
    last_sync_server_updated_at: str | None,
) -> Classification:
    """End-to-end disposition for one (rom, emulator, slot) key.

    Combines `local_changed`, `server_changed_fast`, `determine_action`,
    and `resolve_newest` into a single call so both the dry-run planner
    and the real-run executor can share the decision tree without
    duplicating it.

    Inputs are primitive — no LocalSave / SaveRecord / server-dict
    types — so this function stays generic across backends.
    `last_sync_*` come from the prior `SaveRecord` (None when there's
    no prior). All `*_md5` / `*_updated_at` are normalized to
    None-or-non-empty by the caller.
    """
    has_local = local_md5 is not None and local_mtime is not None
    has_server = server_md5 is not None or server_updated_at is not None
    has_prior = last_sync_md5 is not None

    if not has_local and not has_server:
        if has_prior:
            return Classification(action="drop_prior", reason="both sides deleted")
        return Classification(action="skip", reason="nothing to sync")

    if not has_local and has_server:
        return Classification(action="download", reason="new server save")

    if has_local and not has_server:
        return Classification(action="upload", reason="new local save")

    # Both present.
    assert local_md5 is not None and local_mtime is not None
    server_ts = server_updated_at or ""
    s_md5 = server_md5 or ""

    if not has_prior:
        # First-time conflict — never synced before.
        resolution = resolve_newest(local_mtime=local_mtime, server_updated_at=server_ts)
        if resolution == "ambiguous":
            return Classification(
                action="ambiguous",
                reason="first sync — within tolerance",
                ambiguous_message=f"{local_save_filename}: first sync — within tolerance",
            )
        if local_md5 == s_md5:
            return Classification(action="skip", reason="bytes identical")
        return Classification(
            action="upload" if resolution == "upload" else "download",
            reason=(
                "first sync — local newer"
                if resolution == "upload"
                else "first sync — server newer"
            ),
            conflict_resolved=True,
        )

    # We have a prior sync record — full diff.
    l_changed = local_changed(local_md5, last_sync_md5)
    s_changed = server_changed_fast(
        stored_updated_at=last_sync_server_updated_at,
        stored_size=last_sync_server_size,
        server_updated_at=server_ts,
        server_size=server_size,
    )
    if s_changed is None:
        s_changed = s_md5 != last_sync_md5
    action = determine_action(local_changed_=l_changed, server_changed=s_changed)
    if action == "skip":
        return Classification(action="skip", reason="unchanged since last sync")
    if action == "upload":
        return Classification(action="upload", reason="local changed since last sync")
    if action == "download":
        return Classification(action="download", reason="server changed since last sync")

    # Conflict — both sides changed.
    resolution = resolve_newest(local_mtime=local_mtime, server_updated_at=server_ts)
    if resolution == "ambiguous":
        return Classification(
            action="ambiguous",
            reason="conflict within tolerance",
            ambiguous_message=f"{local_save_filename}: conflict within tolerance",
        )
    return Classification(
        action="upload" if resolution == "upload" else "download",
        reason=(
            "conflict resolved — local newer"
            if resolution == "upload"
            else "conflict resolved — server newer"
        ),
        conflict_resolved=True,
    )


def local_changed(local_hash: str | None, last_sync_hash: str | None) -> bool:
    """True iff the local file's hash differs from the last successfully synced hash.

    `None` is treated as "missing" — both-None means the file was missing
    last sync and is still missing now (no change). One-None-one-set
    means the file appeared or disappeared (changed).
    """
    return local_hash != last_sync_hash


def server_changed_fast(
    *,
    stored_updated_at: str | None,
    stored_size: int | None,
    server_updated_at: str | None,
    server_size: int | None,
) -> bool | None:
    """Fast-path detection of server-side changes via timestamp + size only.

    Returns:
        False — server is definitely unchanged (timestamps match AND sizes
            agree, or sizes are unknown but timestamps are equal).
        True  — server has definitely changed (timestamp matches but size
            differs — same RomM record, different content).
        None  — indeterminate; caller must do a slow-path hash comparison
            (timestamp differs, or no stored timestamp to compare against).

    The fast path lets us skip a hash compute on the typical
    "nothing-changed-since-last-sync" case.
    """
    if not stored_updated_at or stored_updated_at != server_updated_at:
        return None
    if stored_size is None or server_size is None:
        # Timestamps match but at least one size is unknown — assume unchanged
        # (the typical case for older state records that didn't track size).
        return False
    return server_size != stored_size


def determine_action(*, local_changed_: bool, server_changed: bool) -> Action:
    """Given local + server change flags, decide what to do.

    Returns one of `"skip"` (neither changed), `"upload"` (only local
    changed), `"download"` (only server changed), or `"conflict"` (both
    changed — caller invokes `resolve_newest` or surfaces to UI).
    """
    if not local_changed_ and not server_changed:
        return "skip"
    if not local_changed_:
        return "download"
    if not server_changed:
        return "upload"
    return "conflict"


def resolve_newest(
    *,
    local_mtime: float,
    server_updated_at: str,
    tolerance_sec: float = 60.0,
) -> Resolution:
    """Newest-wins conflict resolution.

    Compares local file mtime to server's `updated_at`. Within
    `tolerance_sec` (default 60s — covers normal NTP drift between client
    and server) the result is `"ambiguous"`: the caller decides whether
    to skip with a warning (CLI) or prompt the user (future UI).

    Parse failures on `server_updated_at` also return `"ambiguous"`,
    since we can't compare what we can't parse.
    """
    try:
        server_dt = datetime.fromisoformat(server_updated_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "ambiguous"

    local_dt = datetime.fromtimestamp(local_mtime, tz=UTC)
    diff_sec = (local_dt - server_dt).total_seconds()
    if abs(diff_sec) <= tolerance_sec:
        return "ambiguous"
    return "upload" if diff_sec > 0 else "download"
