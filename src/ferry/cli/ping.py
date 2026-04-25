import click

from ferry import __version__
from ferry.config import ConfigError, load_config


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
    click.echo("ping: RomM client not yet wired up — landing in checkpoint 3.")


def _mask(token: str) -> str:
    if len(token) <= 6:
        return "(set)"
    return f"{token[:4]}…{token[-3:]}"
