import logging
from pathlib import Path

import click

from ferry import __version__
from ferry.adapters.romm import RommApi, RommApiError, RommAuthError, RommHttpAdapter
from ferry.config import ConfigError, Destination, load_config

logger = logging.getLogger(__name__)


@click.command()
@click.pass_context
def ping(ctx: click.Context) -> None:
    """Smoke-test the configured RomM connection."""
    click.echo(f"ferry {__version__}")

    try:
        loaded = load_config(ctx.obj.get("config_path"))
    except ConfigError as e:
        raise click.ClickException(str(e)) from e

    romm = loaded.config.romm
    click.echo(f"config:                  {loaded.config_path}")
    click.echo(f"romm.url:                {romm.url}")
    click.echo(f"romm.api_key:            {_mask(romm.api_key)} (from {loaded.api_key_source})")
    click.echo(f"romm.allow_insecure_ssl: {romm.allow_insecure_ssl}")
    _print_destination(loaded.config.destination)
    click.echo("")
    click.echo("connecting…")

    try:
        with RommHttpAdapter(romm, logger) as http:
            api = RommApi(http)
            user = api.get_me()
            collections = api.list_collections()
    except RommAuthError as e:
        raise click.ClickException(
            f"{e}\n\ncheck the API key — it may be expired or revoked."
        ) from e
    except RommApiError as e:
        raise click.ClickException(str(e)) from e

    username = user.get("username", "?")
    user_id = user.get("id", "?")
    scopes = user.get("oauth_scopes") or []
    click.echo(f"✓ connected as {username} (id={user_id})")
    if scopes:
        click.echo(f"  scopes: {', '.join(scopes)}")

    click.echo(f"✓ {len(collections)} collection(s):")
    for coll in collections:
        name = coll.get("name", "?")
        coll_id = coll.get("id", "?")
        rom_count = coll.get("rom_count")
        suffix = f", {rom_count} ROMs" if rom_count is not None else ""
        click.echo(f"    - {name} (id={coll_id}{suffix})")


def _mask(token: str) -> str:
    if len(token) <= 6:
        return "(set)"
    return f"{token[:4]}…{token[-3:]}"


def _print_destination(dest: Destination | None) -> None:
    if dest is None:
        click.echo("destination:             (not configured — `ferry detect` for help)")
        return
    preset = dest.preset or "(custom)"
    click.echo(f"destination.preset:      {preset}")
    click.echo(f"destination.roms_base:   {dest.roms_base} {_path_status(dest.roms_base)}")
    if dest.bios_base is None:
        click.echo("destination.bios_base:   (per-emulator — no centralized BIOS root)")
    else:
        click.echo(f"destination.bios_base:   {dest.bios_base} {_path_status(dest.bios_base)}")


def _path_status(path: Path) -> str:
    if not path.exists():
        return "(missing)"
    if not path.is_dir():
        return "(not a directory)"
    return "(exists)"
