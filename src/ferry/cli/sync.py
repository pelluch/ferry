import logging
from typing import Any

import click

from ferry.adapters.romm import (
    RommApi,
    RommApiError,
    RommAuthError,
    RommHttpAdapter,
)
from ferry.adapters.state_store import (
    default_state_path,
    ensure_sidecars,
    load_state,
    recover_state_from_sidecars,
    save_state,
)
from ferry.config import ConfigError, SyncConfig, load_config
from ferry.config.schema import Config
from ferry.domain.platforms import resolve_platform_dir
from ferry.domain.sync_plan import (
    AddAction,
    DeleteAction,
    SyncPlan,
    UpdateAction,
    compute_plan,
)
from ferry.services.sync_executor import (
    ExecutionResult,
    default_scratch_root,
    execute_plan,
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
    help="Print every entry in each section instead of truncating (dry-run only).",
)
@click.pass_context
def sync(ctx: click.Context, dry_run: bool, full: bool) -> None:
    """Sync the configured collection from RomM to the destination."""
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

            state_path = default_state_path()
            state = load_state(state_path)
            if not state.roms and config.destination is not None:
                recovered = recover_state_from_sidecars([config.destination.roms_base])
                if recovered.roms:
                    click.echo(f"recovered {len(recovered.roms)} ROM(s) from on-disk sidecars")
                    state = recovered
                    save_state(state, state_path)
            if config.destination is not None:
                regenerated = ensure_sidecars(state, config.destination)
                if regenerated:
                    click.echo(f"regenerated {regenerated} missing sidecar(s) from state")
            plan = compute_plan(
                current_roms=current_roms,
                state=state,
                destination=config.destination,
            )

            if dry_run:
                _print_plan(plan, full=full, config=config)
                return

            _print_plan_summary(plan)
            if plan.is_empty:
                click.echo("")
                click.echo("Nothing to do — local state matches RomM.")
                return

            click.echo("")
            click.echo("Executing plan…")
            click.echo("")
            scratch_root = default_scratch_root()
            result = execute_plan(
                plan=plan,
                config=config,
                api=api,
                state=state,
                state_path=state_path,
                scratch_root=scratch_root,
                progress=click.echo,
            )
            _print_execution_summary(plan, result)
    except RommAuthError as e:
        raise click.ClickException(
            f"{e}\n\ncheck the API key — it may be expired or revoked."
        ) from e
    except RommApiError as e:
        raise click.ClickException(str(e)) from e


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


def _print_plan_summary(plan: SyncPlan) -> None:
    click.echo("")
    click.echo("Sync plan:")
    click.echo(f"  Add:        {len(plan.to_add)}")
    click.echo(f"  Update:     {len(plan.to_update)}")
    click.echo(f"  Delete:     {len(plan.to_delete)}")
    click.echo(f"  Unchanged:  {plan.unchanged_count}")


def _print_plan(plan: SyncPlan, *, full: bool, config: Config) -> None:
    _print_plan_summary(plan)

    cap = None if full else _DEFAULT_PREVIEW
    _print_section("To add", plan.to_add, "+", cap, config)
    _print_section("To update", plan.to_update, "↻", cap, config)
    _print_section("To delete", plan.to_delete, "-", cap, config)

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
    config: Config,
) -> None:
    if not items:
        return
    click.echo("")
    click.echo(f"{title} ({len(items)}):")
    shown = items if cap is None else items[:cap]
    for a in shown:
        line = f"  {sigil} {a.name} ({a.platform_slug}, rom_id={a.rom_id})"
        details = _format_action_destination(a, config)
        if details:
            line += f" → {details}"
        click.echo(line)
    if cap is not None and len(items) > cap:
        click.echo(f"  ... and {len(items) - cap} more (run with --full to list all)")


def _format_action_destination(
    action: AddAction | UpdateAction | DeleteAction,
    config: Config,
) -> str:
    """Render the on-disk path + pipeline summary for a planned action."""
    if config.destination is None:  # guarded earlier; defensive
        return ""
    roms_base = config.destination.roms_base

    if isinstance(action, DeleteAction):
        # Show the existing primary output so the user knows what would go.
        primary = action.previous.outputs[action.previous.primary_output_index]
        return str(roms_base / primary.path)

    # Add / Update: source filename → resolved platform dir
    fs_name = action.rom_data.get("fs_name") or f"rom-{action.rom_id}"
    platform_dir = roms_base / resolve_platform_dir(action.platform_slug)
    pipeline = config.transforms.for_platform(action.platform_slug)
    pipeline_str = f" [{' → '.join(pipeline)}]" if pipeline else ""
    return f"{platform_dir / fs_name}{pipeline_str}"


def _print_execution_summary(plan: SyncPlan, result: ExecutionResult) -> None:
    click.echo("")
    click.echo("Sync complete:")
    click.echo(f"  Synced:  {len(result.succeeded)}")
    click.echo(f"  Failed:  {len(result.failed)}")
    if result.skipped_deletes:
        click.echo(
            f"  Pending deletes: {result.skipped_deletes} "
            f"(delete-on-remove not yet implemented; ROMs remain on disk)"
        )
    if result.failed:
        click.echo("")
        click.echo("Failures:")
        for f in result.failed:
            click.echo(f"  ✗ {f.name} ({f.platform_slug}, rom_id={f.rom_id}): {f.error}")
        click.echo("")
        click.echo("Re-running sync will retry failed ROMs as they're still in `to_add`.")
    if plan.to_delete:
        click.echo("")
        click.echo("ROMs no longer in collection (would delete):")
        for d in plan.to_delete[:_DEFAULT_PREVIEW]:
            click.echo(f"  - {d.name} ({d.platform_slug}, rom_id={d.rom_id})")
        if len(plan.to_delete) > _DEFAULT_PREVIEW:
            click.echo(f"  ... and {len(plan.to_delete) - _DEFAULT_PREVIEW} more")
