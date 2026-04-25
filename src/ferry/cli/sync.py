import logging
from typing import Any

import click

from ferry.adapters.romm import (
    RommApi,
    RommApiError,
    RommAuthError,
    RommHttpAdapter,
)
from ferry.adapters.state_store import load_state
from ferry.config import ConfigError, SyncConfig, load_config
from ferry.domain.sync_plan import (
    AddAction,
    DeleteAction,
    SyncPlan,
    UpdateAction,
    compute_plan,
)

logger = logging.getLogger(__name__)

# How many entries per section to print before truncating with "... and N more".
_DEFAULT_PREVIEW = 20


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would happen without modifying anything.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Print every entry in each section instead of truncating.",
)
@click.pass_context
def sync(ctx: click.Context, dry_run: bool, full: bool) -> None:
    """Sync the configured collection from RomM to the destination."""
    if not dry_run:
        raise click.ClickException(
            "execution lands in a later checkpoint; pass --dry-run to preview the plan."
        )

    try:
        loaded = load_config(ctx.obj.get("config_path"))
    except ConfigError as e:
        raise click.ClickException(str(e)) from e

    config = loaded.config
    if config.sync is None:
        raise click.ClickException(
            "[sync].collection is required for sync. Add to your config:\n\n"
            "    [sync]\n"
            '    collection = "Steam Deck"'
        )
    if config.destination is None:
        raise click.ClickException(
            "[destination] is required for sync. Run `ferry detect` for help."
        )

    sync_cfg: SyncConfig = config.sync
    click.echo(f"connecting to {config.romm.url}…")
    try:
        with RommHttpAdapter(config.romm, logger) as http:
            api = RommApi(http)
            collection = _resolve_collection(api, sync_cfg.collection)
            click.echo(f"✓ resolved collection: {collection['name']} (id={collection['id']})")
            click.echo("fetching ROM listing…")
            current_roms = api.list_roms_in_collection(
                collection["id"],
                primary_only=sync_cfg.primary_version_only,
            )
            click.echo(f"✓ {len(current_roms)} ROM(s) returned")
    except RommAuthError as e:
        raise click.ClickException(
            f"{e}\n\ncheck the API key — it may be expired or revoked."
        ) from e
    except RommApiError as e:
        raise click.ClickException(str(e)) from e

    state = load_state()
    plan = compute_plan(current_roms=current_roms, state=state)
    _print_plan(plan, full=full)


def _resolve_collection(api: RommApi, name: str) -> dict[str, Any]:
    collections = api.list_collections()
    matches = [c for c in collections if c.get("name") == name]
    if not matches:
        names = sorted(c.get("name", "?") for c in collections)
        raise click.ClickException(
            f"collection {name!r} not found in RomM.\n"
            f"available: {', '.join(names) if names else '(none)'}"
        )
    if len(matches) > 1:
        raise click.ClickException(
            f"multiple collections named {name!r} found "
            f"(ids: {[c.get('id') for c in matches]}). "
            f"matching by id is not yet supported; rename one in RomM."
        )
    return matches[0]


def _print_plan(plan: SyncPlan, *, full: bool) -> None:
    click.echo("")
    click.echo("Sync plan:")
    click.echo(f"  Add:        {len(plan.to_add)}")
    click.echo(f"  Update:     {len(plan.to_update)}")
    click.echo(f"  Delete:     {len(plan.to_delete)}")
    click.echo(f"  Unchanged:  {plan.unchanged_count}")

    cap = None if full else _DEFAULT_PREVIEW
    _print_section("To add", plan.to_add, "+", cap)
    _print_section("To update", plan.to_update, "↻", cap)
    _print_section("To delete", plan.to_delete, "-", cap)

    click.echo("")
    if plan.is_empty:
        click.echo("Nothing to do — local state matches RomM.")
    else:
        click.echo("(dry run — no files modified)")


def _print_section(
    title: str,
    items: list[AddAction] | list[UpdateAction] | list[DeleteAction],
    sigil: str,
    cap: int | None,
) -> None:
    if not items:
        return
    click.echo("")
    click.echo(f"{title} ({len(items)}):")
    shown = items if cap is None else items[:cap]
    for a in shown:
        click.echo(f"  {sigil} {a.name} ({a.platform_slug}, rom_id={a.rom_id}) — {a.reason}")
    if cap is not None and len(items) > cap:
        click.echo(f"  ... and {len(items) - cap} more (run with --full to list all)")
