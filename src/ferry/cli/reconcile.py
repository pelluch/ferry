"""`ferry reconcile` — adopt orphan ROM files into ferry's tracked state.

Walks the configured `roms_base` for files that aren't tracked
(no canonical sidecar, not in state.json), classifies each against
RomM's per-platform listing using name + md5, and writes sidecars +
state.json entries for **confident** matches (name AND hash both
match a single rom). Other classifications (name-only, hash-only,
ambiguous, no-match) are reported but not adopted by default —
ck3 will add `--include-name-only` / `--include-renames` flags for
opt-in.

Default mode runs the adoption. `--dry-run` previews without writes,
matching ferry's existing convention. `--platform <slug>` scopes the
walk to one RomM platform.

Hash-matching uses RomM's largest-inner-file convention — see
`adapters/orphan_hash.py`. For pass-through cartridge `.zip`s, this
hashes the inner ROM. For unzipped `.iso`s, it hashes the file
directly. Both match RomM's `RomFile.md5_hash` without server-side
cooperation.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

import click

from ferry.adapters.romm import RommApi, RommApiError, RommAuthError, RommHttpAdapter
from ferry.adapters.sidecar import (
    default_sidecars_root,
    migrate_legacy_sidecars,
    write_sidecar,
)
from ferry.adapters.state_store import (
    default_state_path,
    ensure_sidecars,
    load_state,
    recover_state_from_sidecars,
    save_state,
)
from ferry.config import ConfigError, load_config
from ferry.config.schema import Config
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.state import LibraryState
from ferry.services.reconcile import (
    Ambiguous,
    Classification,
    Confident,
    HashOnly,
    NameOnly,
    NoMatch,
    OrphanCandidate,
    build_index,
    classify,
    find_orphans,
    synthesize_state,
)
from ferry.services.sync_lock import LockHeld, acquire_sync_lock, default_lock_path

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be adopted without modifying state or sidecars.",
)
@click.option(
    "--platform",
    "platform_slug",
    type=str,
    default=None,
    help="Scope the walk to a single RomM platform slug (e.g., `gba`, `gc`).",
)
@click.pass_context
def reconcile(ctx: click.Context, dry_run: bool, platform_slug: str | None) -> None:
    """Adopt orphan ROM files: name+hash-match them against RomM, write sidecars."""
    try:
        loaded = load_config(ctx.obj.get("config_path"))
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    config = loaded.config

    if config.destination is None:
        raise click.ClickException(
            "[destination] is required for reconcile. Run `ferry detect` for help."
        )

    try:
        with acquire_sync_lock(default_lock_path()):
            _run_reconcile(config, dry_run=dry_run, platform_slug=platform_slug)
    except LockHeld as e:
        raise click.ClickException(
            f"another ferry sync is already running (pid {e.pid}, lock at {e.lock_path})."
        ) from e


def _run_reconcile(config: Config, *, dry_run: bool, platform_slug: str | None) -> None:
    assert config.destination is not None
    roms_base = config.destination.roms_base
    sidecars_root = default_sidecars_root()
    state_path = default_state_path()
    state = load_state(state_path)

    # Mirror sync's preamble — make sure state-vs-sidecar is converged before
    # we walk for orphans, otherwise the same file could classify as orphan in
    # one run and tracked in the next.
    migrated = migrate_legacy_sidecars(roms_base=roms_base)
    if migrated:
        click.echo(f"migrated {migrated} legacy sidecar(s) to {sidecars_root}")
    if not state.roms:
        recovered = recover_state_from_sidecars(roms_base)
        if recovered.roms:
            click.echo(f"recovered {len(recovered.roms)} ROM(s) from on-disk sidecars")
            state = recovered
            if not dry_run:
                save_state(state, state_path)
    regenerated = ensure_sidecars(state, config.destination)
    if regenerated:
        click.echo(f"regenerated {regenerated} missing sidecar(s) from state")

    # Resolve --platform <slug> to its on-disk dir name (the walker
    # filter is dir-based, not slug-based, since dir is what we
    # actually walk).
    platform_dir_filter: str | None = None
    if platform_slug is not None:
        platform_dir_filter = resolve_platform_dir(platform_slug)

    orphans = find_orphans(
        roms_base=roms_base,
        sidecars_root=sidecars_root,
        state=state,
        platform_filter=platform_dir_filter,
    )
    if not orphans:
        if platform_slug is not None:
            click.echo(f"No orphans found under {roms_base}/{platform_dir_filter}/.")
        else:
            click.echo(f"No orphans found under {roms_base}/.")
        return

    click.echo(f"Found {len(orphans)} orphan file(s) under {roms_base}.")

    # Group by platform_dir for batched API calls. We need a slug per dir;
    # walk RomM's platform list to map dir → slug.
    click.echo(f"connecting to {config.romm.url}…")
    try:
        with RommHttpAdapter(config.romm, logger) as http:
            api = RommApi(http)
            platforms = api.list_platforms()
            classifications = _classify_all_orphans(orphans, api, platforms)
    except RommAuthError as e:
        raise click.ClickException(
            f"{e}\n\ncheck the API key — it may be expired or revoked."
        ) from e
    except RommApiError as e:
        raise click.ClickException(str(e)) from e

    confident = [c for c in classifications if isinstance(c, Confident)]
    ambiguous = [c for c in classifications if isinstance(c, Ambiguous)]
    name_only = [c for c in classifications if isinstance(c, NameOnly)]
    hash_only = [c for c in classifications if isinstance(c, HashOnly)]
    no_match = [c for c in classifications if isinstance(c, NoMatch)]

    _print_summary(
        confident=confident,
        ambiguous=ambiguous,
        name_only=name_only,
        hash_only=hash_only,
        no_match=no_match,
    )

    if dry_run:
        click.echo("")
        click.echo("(dry run — no sidecars or state written)")
        return

    if not confident:
        click.echo("")
        click.echo("Nothing to adopt — no confident matches.")
        return

    adopted = _adopt_confident(confident, config=config, state=state, state_path=state_path)
    click.echo("")
    click.echo(f"Adopted {adopted} ROM(s). Wrote sidecars + state.json entries.")


def _classify_all_orphans(
    orphans: list[OrphanCandidate],
    api: RommApi,
    platforms: list[dict[str, Any]],
) -> list[Classification]:
    """Fetch RomM's per-platform rom listing once per dir-with-orphans
    and classify each orphan against the resulting index.

    Platform dirs that don't map to any RomM platform are reported as
    NoMatch wholesale (no warning here — caller surfaces them via the
    standard summary).
    """
    by_dir: dict[str, list[OrphanCandidate]] = defaultdict(list)
    for o in orphans:
        by_dir[o.platform_dir].append(o)

    # Build dir → platform_id map by resolving each RomM platform's slug
    # through `resolve_platform_dir` (matches the convention sync uses for
    # its disk layout).
    dir_to_platform: dict[str, dict[str, Any]] = {}
    for platform in platforms:
        slug = platform.get("slug")
        if not slug:
            continue
        dir_to_platform[resolve_platform_dir(slug)] = platform

    out: list[Classification] = []
    for platform_dir, orphans_in_dir in sorted(by_dir.items()):
        platform = dir_to_platform.get(platform_dir)
        if platform is None:
            # No RomM platform maps to this disk dir; everything is NoMatch.
            for orphan in orphans_in_dir:
                out.append(classify(orphan, by_name={}, by_hash={}, by_stem={}))
            continue
        platform_id = platform.get("id")
        if not isinstance(platform_id, int):
            for orphan in orphans_in_dir:
                out.append(classify(orphan, by_name={}, by_hash={}, by_stem={}))
            continue
        click.echo(
            f"  fetching ROMs for platform {platform.get('slug')!r} "
            f"({len(orphans_in_dir)} orphan(s))…"
        )
        roms = api.list_roms(platform_ids=[platform_id])
        by_name, by_hash, by_stem = build_index(roms)
        for orphan in orphans_in_dir:
            out.append(classify(orphan, by_name=by_name, by_hash=by_hash, by_stem=by_stem))
    return out


def _adopt_confident(
    confident: list[Confident],
    *,
    config: Config,
    state: LibraryState,
    state_path: Path,
) -> int:
    """Write sidecar + state.json entry for each confident match."""
    assert config.destination is not None
    roms_base = config.destination.roms_base
    adopted = 0
    for c in confident:
        transforms = config.transforms.for_platform(c.match.rom_data.get("platform_slug") or "")
        rom_state = synthesize_state(c, roms_base=roms_base, transforms_for_platform=transforms)
        write_sidecar(c.orphan.abs_path, rom_state, roms_base=roms_base)
        state.roms[rom_state.rom_id] = rom_state
        adopted += 1
    if adopted:
        save_state(state, state_path)
    return adopted


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

_PREVIEW_CAP = 20


def _print_summary(
    *,
    confident: list[Confident],
    ambiguous: list[Ambiguous],
    name_only: list[NameOnly],
    hash_only: list[HashOnly],
    no_match: list[NoMatch],
) -> None:
    click.echo("")
    click.echo("Reconcile classification:")
    click.echo(f"  Confident:  {len(confident)} (name + hash both match — would adopt)")
    click.echo(f"  Name-only:  {len(name_only)} (filename matches; hash differs)")
    click.echo(f"  Hash-only:  {len(hash_only)} (hash matches; renamed locally)")
    click.echo(f"  Ambiguous:  {len(ambiguous)} (multiple RomM rom_ids match — skipped)")
    click.echo(f"  No match:   {len(no_match)} (not in RomM)")

    if confident:
        click.echo("")
        click.echo(f"Confident matches ({len(confident)}):")
        shown = confident[:_PREVIEW_CAP]
        for c in shown:
            click.echo(f"  ✓ {c.orphan.rel_path} → {c.match.rom_name} (rom_id={c.match.rom_id})")
        if len(confident) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(confident) - _PREVIEW_CAP} more")

    if name_only:
        click.echo("")
        click.echo(f"Name-only matches ({len(name_only)}) — pass --include-name-only to adopt:")
        for c in name_only[:_PREVIEW_CAP]:
            click.echo(
                f"  ? {c.orphan.rel_path} → matches name of "
                f"{c.candidates[0].rom_name} (rom_id={c.candidates[0].rom_id}) "
                f"but local hash {c.local_md5 or '(unhashable)'} != "
                f"server md5"
            )
        if len(name_only) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(name_only) - _PREVIEW_CAP} more")

    if hash_only:
        click.echo("")
        click.echo(f"Hash-only matches ({len(hash_only)}) — pass --include-renames to adopt:")
        for c in hash_only[:_PREVIEW_CAP]:
            click.echo(
                f"  ↻ {c.orphan.rel_path} → matches hash of "
                f"{c.candidates[0].rom_name} (rom_id={c.candidates[0].rom_id}, "
                f"server file_name={c.candidates[0].file_name!r})"
            )
        if len(hash_only) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(hash_only) - _PREVIEW_CAP} more")

    if ambiguous:
        click.echo("")
        click.echo(f"Ambiguous ({len(ambiguous)}) — name + hash match multiple rom_ids:")
        for c in ambiguous[:_PREVIEW_CAP]:
            ids = sorted({m.rom_id for m in c.matches})
            click.echo(f"  ⚠ {c.orphan.rel_path} → rom_ids {ids}")
        if len(ambiguous) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(ambiguous) - _PREVIEW_CAP} more")

    if no_match:
        click.echo("")
        click.echo(f"No match ({len(no_match)}) — not present in RomM:")
        for c in no_match[:_PREVIEW_CAP]:
            click.echo(f"  - {c.orphan.rel_path}")
        if len(no_match) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(no_match) - _PREVIEW_CAP} more")
