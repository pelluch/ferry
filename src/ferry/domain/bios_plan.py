"""BIOS sync plan computation — what would change if we synced firmware now.

Pure functions over the inputs (current RomM firmware listing, stored
LibraryState). The structure deliberately mirrors `domain.sync_plan`:

- the download path → executes `to_add` and `to_update`
- the delete path → executes `to_delete` (gated at runtime by
  `[sync].delete_on_remove`, exactly like ROM deletion)
- `ferry sync --dry-run` → prints the plan and exits

RomM models firmware per-platform and `FirmwareSchema` carries no platform
field, so the caller — which fetched each platform's firmware separately —
passes the listing already grouped: `{platform_slug: [firmware_dict, ...]}`.

Change detection is tiered, same rationale as `sync_plan`:

1. **md5 compare** — RomM's `md5_hash` against the stored `BiosRecord.md5`
   (locally computed on download). Deterministic.
2. **file_size_bytes compare** when md5 is unavailable on either side.
3. **conservative re-sync** when neither is comparable.

A firmware whose content is unchanged but whose computed placement no
longer matches `BiosRecord.path` (e.g. the subfolder map gained an entry
in a ferry upgrade) is also flagged for update so the file is re-placed.

Scope note: the caller only fetches firmware for in-`[sync]`-scope
platforms, and `compute_bios_plan` further drops files excluded by
`[bios.files]`. Anything in stored state that survives neither filter
lands in `to_delete` — that's how "user removed a platform from config"
turns into a (gated) cleanup.

Collision note: flat placement keys on `file_name`, so two platforms with
an identically-named firmware file resolve to the same `target_path`.
RetroDECK's checker is itself filename-based, so this is inherent rather
than a regression; v5.5 does not arbitrate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ferry.domain.bios_placement import placement_for
from ferry.domain.state import BiosRecord, LibraryState


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosAddAction:
    """Firmware in RomM that ferry has never placed before."""

    firmware_id: int
    platform_slug: str
    file_name: str
    target_path: str  # relative to Destination.bios_base
    firmware_data: dict[str, Any]
    unverified: bool  # RomM's is_verified is false — file is not a known-good BIOS
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosUpdateAction:
    """Firmware ferry has placed, but content or placement has moved."""

    firmware_id: int
    platform_slug: str
    file_name: str
    target_path: str
    firmware_data: dict[str, Any]
    previous: BiosRecord
    unverified: bool
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosDeleteAction:
    """Firmware in stored state that's no longer in RomM or in scope."""

    firmware_id: int
    platform_slug: str
    file_name: str
    previous: BiosRecord
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosPlan:
    to_add: list[BiosAddAction]
    to_update: list[BiosUpdateAction]
    to_delete: list[BiosDeleteAction]
    unchanged_count: int

    @property
    def is_empty(self) -> bool:
        return not (self.to_add or self.to_update or self.to_delete)

    @property
    def total_changes(self) -> int:
        return len(self.to_add) + len(self.to_update) + len(self.to_delete)


def compute_bios_plan(
    *,
    firmware_by_platform: dict[str, list[dict[str, Any]]],
    state: LibraryState,
    allowlists: dict[str, tuple[str, ...]] | None = None,
    bios_base: Path | None = None,
) -> BiosPlan:
    """Diff RomM firmware against stored BIOS state.

    `firmware_by_platform` maps platform slug → that platform's firmware
    records (raw RomM dicts). `allowlists` is `[bios.files]` — a platform
    slug present restricts sync to the named files; absent means all.

    `to_delete` is always populated for stored firmware no longer present
    in the (post-allowlist) current set — informational, like
    `sync_plan`. Whether deletions execute is the executor's runtime
    call, governed by `[sync].delete_on_remove`.

    When `bios_base` is given, an unchanged firmware whose on-disk file is
    missing is promoted to `to_update` (the user deleted it; put it back).
    """
    allowlists = allowlists or {}
    to_add: list[BiosAddAction] = []
    to_update: list[BiosUpdateAction] = []
    to_delete: list[BiosDeleteAction] = []
    unchanged = 0

    current_ids: set[int] = set()
    for platform_slug, firmware_list in firmware_by_platform.items():
        allowlist = allowlists.get(platform_slug)
        for fw in firmware_list:
            firmware_id = fw.get("id")
            if not isinstance(firmware_id, int):
                continue  # defensive: skip rows we can't identify
            file_name = fw.get("file_name")
            if not isinstance(file_name, str) or not file_name:
                continue
            if allowlist is not None and file_name not in allowlist:
                continue  # excluded by [bios.files]

            current_ids.add(firmware_id)
            target = placement_for(platform_slug, file_name)
            unverified = not bool(fw.get("is_verified"))
            prev = state.bios.get(firmware_id)

            if prev is None:
                to_add.append(
                    BiosAddAction(
                        firmware_id=firmware_id,
                        platform_slug=platform_slug,
                        file_name=file_name,
                        target_path=target,
                        firmware_data=fw,
                        unverified=unverified,
                        reason="new in RomM",
                    )
                )
                continue

            reason = _update_reason(fw, prev, target, bios_base)
            if reason is not None:
                to_update.append(
                    BiosUpdateAction(
                        firmware_id=firmware_id,
                        platform_slug=platform_slug,
                        file_name=file_name,
                        target_path=target,
                        firmware_data=fw,
                        previous=prev,
                        unverified=unverified,
                        reason=reason,
                    )
                )
            else:
                unchanged += 1

    for firmware_id, prev in state.bios.items():
        if firmware_id not in current_ids:
            to_delete.append(
                BiosDeleteAction(
                    firmware_id=firmware_id,
                    platform_slug=prev.platform_slug,
                    file_name=prev.file_name,
                    previous=prev,
                    reason="no longer in RomM or out of sync scope",
                )
            )

    to_add.sort(key=lambda a: (a.file_name, a.firmware_id))
    to_update.sort(key=lambda a: (a.file_name, a.firmware_id))
    to_delete.sort(key=lambda a: (a.file_name, a.firmware_id))

    return BiosPlan(
        to_add=to_add,
        to_update=to_update,
        to_delete=to_delete,
        unchanged_count=unchanged,
    )


def _update_reason(
    fw: dict[str, Any], prev: BiosRecord, target: str, bios_base: Path | None
) -> str | None:
    """Why *fw* needs re-syncing, or None if it's unchanged.

    Checked in order: content change, placement change, missing on disk.
    """
    if _content_changed(fw, prev):
        return _content_change_reason(fw, prev)
    if prev.path != target:
        return f"placement changed ({prev.path} → {target})"
    if bios_base is not None and not (bios_base / prev.path).exists():
        return "file missing on disk — re-syncing"
    return None


def _content_changed(fw: dict[str, Any], prev: BiosRecord) -> bool:
    """Tiered change detection — md5, then size, then conservative re-sync."""
    server_md5 = fw.get("md5_hash")
    have_prev_md5 = bool(prev.md5)
    have_server_md5 = isinstance(server_md5, str) and bool(server_md5)

    if have_prev_md5 and have_server_md5:
        return prev.md5 != server_md5

    server_size = fw.get("file_size_bytes")
    if isinstance(server_size, int) and prev.size:
        return server_size != prev.size

    return True


def _content_change_reason(fw: dict[str, Any], prev: BiosRecord) -> str:
    server_md5 = fw.get("md5_hash")
    have_prev_md5 = bool(prev.md5)
    have_server_md5 = isinstance(server_md5, str) and bool(server_md5)
    if have_prev_md5 and have_server_md5:
        return f"md5 changed ({prev.md5} → {server_md5})"
    server_size = fw.get("file_size_bytes")
    if isinstance(server_size, int) and prev.size:
        return (
            f"file_size_bytes changed ({prev.size} → {server_size}; md5 unavailable, size fallback)"
        )
    return "no comparable signal — re-syncing conservatively"
