"""`ferry reconcile` — adopt orphan ROM files into ferry's tracked state.

Walks the configured `roms_base` for files that aren't tracked
(not in state.json), classifies each against RomM's per-platform
listing using name + md5, and writes
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
from ferry.adapters.state_store import (
    default_state_path,
    load_state,
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
    synthesize_state_from_match,
)
from ferry.services.sync_lock import LockHeld, acquire_sync_lock, default_lock_path

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be adopted without modifying state.",
)
@click.option(
    "--platform",
    "platform_slug",
    type=str,
    default=None,
    help="Scope the walk to a single RomM platform slug (e.g., `gba`, `gc`).",
)
@click.option(
    "--include-name-only",
    is_flag=True,
    help=(
        "Also adopt orphans whose filename (or stem) matches RomM but whose "
        "bytes don't (different revision/region, post-`extract_xiso` Xbox). "
        "Only single-rom_id matches are adopted; multiple-rom_id matches "
        "remain unadopted."
    ),
)
@click.pass_context
def reconcile(
    ctx: click.Context,
    dry_run: bool,
    platform_slug: str | None,
    include_name_only: bool,
) -> None:
    """Adopt orphan ROM files: name+hash-match them against RomM, add to state."""
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
            _run_reconcile(
                config,
                dry_run=dry_run,
                platform_slug=platform_slug,
                include_name_only=include_name_only,
            )
    except LockHeld as e:
        raise click.ClickException(
            f"another ferry sync is already running (pid {e.pid}, lock at {e.lock_path})."
        ) from e


def _run_reconcile(
    config: Config,
    *,
    dry_run: bool,
    platform_slug: str | None,
    include_name_only: bool,
) -> None:
    assert config.destination is not None
    roms_base = config.destination.roms_base
    state_path = default_state_path()
    state = load_state(state_path)

    # Resolve --platform <slug> to its on-disk dir name (the walker
    # filter is dir-based, not slug-based, since dir is what we
    # actually walk).
    platform_dir_filter: str | None = None
    if platform_slug is not None:
        platform_dir_filter = resolve_platform_dir(platform_slug)

    orphans = find_orphans(
        roms_base=roms_base,
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

    # Split NameOnly by adoptability: single-rom_id matches are adoptable
    # via --include-name-only; multi-rom_id matches remain ambiguous.
    name_only_singular = [c for c in name_only if _name_only_rom_id(c) is not None]
    name_only_ambig = [c for c in name_only if _name_only_rom_id(c) is None]

    _print_summary(
        confident=confident,
        ambiguous=ambiguous,
        name_only_singular=name_only_singular,
        name_only_ambig=name_only_ambig,
        hash_only=hash_only,
        no_match=no_match,
        include_name_only=include_name_only,
    )

    if dry_run:
        click.echo("")
        click.echo("(dry run — no state written)")
        return

    will_adopt_name_only = include_name_only and name_only_singular
    if not confident and not will_adopt_name_only:
        click.echo("")
        if name_only_singular:
            click.echo(
                "Nothing to adopt — confident matches are zero. "
                f"{len(name_only_singular)} name-only match(es) available; "
                "pass --include-name-only to adopt them."
            )
        else:
            click.echo("Nothing to adopt — no confident matches.")
        return

    adopted_confident = _adopt_confident(
        confident, config=config, state=state, state_path=state_path
    )
    adopted_name_only = 0
    if will_adopt_name_only:
        adopted_name_only = _adopt_name_only(
            name_only_singular, config=config, state=state, state_path=state_path
        )

    click.echo("")
    if adopted_name_only:
        click.echo(
            f"Adopted {adopted_confident} confident + {adopted_name_only} name-only "
            f"ROM(s). Wrote sidecars + state.json entries."
        )
    else:
        click.echo(f"Adopted {adopted_confident} ROM(s). Wrote sidecars + state.json entries.")


def _name_only_rom_id(name_only: NameOnly) -> int | None:
    """Return the single rom_id for a NameOnly with one candidate rom_id,
    or None if the candidates span multiple rom_ids (ambiguous)."""
    rom_ids = {c.rom_id for c in name_only.candidates}
    return next(iter(rom_ids)) if len(rom_ids) == 1 else None


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
    """Add a state.json entry for each confident match."""
    assert config.destination is not None
    roms_base = config.destination.roms_base
    adopted = 0
    for c in confident:
        transforms = config.transforms.for_platform(c.match.rom_data.get("platform_slug") or "")
        rom_state = synthesize_state(c, roms_base=roms_base, transforms_for_platform=transforms)
        state.roms[rom_state.rom_id] = rom_state
        adopted += 1
    if adopted:
        save_state(state, state_path)
    return adopted


def _adopt_name_only(
    name_only_singular: list[NameOnly],
    *,
    config: Config,
    state: LibraryState,
    state_path: Path,
) -> int:
    """Add a state.json entry for each single-rom_id NameOnly match.

    Synthesis stores `output.md5` from the local file's bytes (which by
    definition don't match the server's md5 for this category) and
    `source_md5` from the server. The planner uses `source_updated_at`
    for change detection, so a future RomM update will trigger a real
    sync that overwrites this name-only adoption with server bytes —
    which is the right behaviour: name-only is "trust me, this is the
    right rom" until a real update arrives.
    """
    assert config.destination is not None
    adopted = 0
    for n in name_only_singular:
        # _name_only_rom_id confirmed single rom_id; pick the first match.
        match = n.candidates[0]
        transforms = config.transforms.for_platform(match.rom_data.get("platform_slug") or "")
        rom_state = synthesize_state_from_match(n.orphan, match, transforms_for_platform=transforms)
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
    name_only_singular: list[NameOnly],
    name_only_ambig: list[NameOnly],
    hash_only: list[HashOnly],
    no_match: list[NoMatch],
    include_name_only: bool,
) -> None:
    click.echo("")
    click.echo("Reconcile classification:")
    click.echo(f"  Confident:       {len(confident)} (name + hash both match — would adopt)")
    name_only_action = "would adopt" if include_name_only else "pass --include-name-only to adopt"
    click.echo(
        f"  Name-only:       {len(name_only_singular)} "
        f"(filename matches; hash differs — {name_only_action})"
    )
    click.echo(
        f"  Name-only ambig: {len(name_only_ambig)} "
        "(name matches multiple RomM rom_ids — skipped even with --include-name-only)"
    )
    click.echo(
        f"  Hash-only:       {len(hash_only)} "
        "(hash matches; renamed locally — never auto-adopted; see DESIGN.md §7 v9+)"
    )
    click.echo(f"  Ambiguous:       {len(ambiguous)} (multiple RomM rom_ids match — skipped)")
    click.echo(f"  No match:        {len(no_match)} (not in RomM)")

    if confident:
        click.echo("")
        click.echo(f"Confident matches ({len(confident)}):")
        shown = confident[:_PREVIEW_CAP]
        for c in shown:
            click.echo(f"  ✓ {c.orphan.rel_path} → {c.match.rom_name} (rom_id={c.match.rom_id})")
        if len(confident) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(confident) - _PREVIEW_CAP} more")

    if name_only_singular:
        click.echo("")
        sigil = "✓" if include_name_only else "?"
        click.echo(f"Name-only matches ({len(name_only_singular)}):")
        for c in name_only_singular[:_PREVIEW_CAP]:
            click.echo(
                f"  {sigil} {c.orphan.rel_path} → matches name of "
                f"{c.candidates[0].rom_name} (rom_id={c.candidates[0].rom_id}) "
                f"but local hash {c.local_md5 or '(unhashable)'} != server md5"
            )
        if len(name_only_singular) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(name_only_singular) - _PREVIEW_CAP} more")

    if name_only_ambig:
        click.echo("")
        click.echo(
            f"Name-only ambiguous ({len(name_only_ambig)}) — name matches multiple RomM rom_ids:"
        )
        for c in name_only_ambig[:_PREVIEW_CAP]:
            ids = sorted({m.rom_id for m in c.candidates})
            click.echo(f"  ⚠ {c.orphan.rel_path} → rom_ids {ids}")
        if len(name_only_ambig) > _PREVIEW_CAP:
            click.echo(f"  ... and {len(name_only_ambig) - _PREVIEW_CAP} more")

    if hash_only:
        click.echo("")
        click.echo(
            f"Hash-only matches ({len(hash_only)}) — bytes match RomM but "
            "filename differs (locally renamed):"
        )
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
