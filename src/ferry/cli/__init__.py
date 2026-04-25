from pathlib import Path

import click

from ferry import __version__
from ferry.cli.ping import ping


@click.group()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="FERRY_CONFIG",
    help="Path to config.toml (default: $XDG_CONFIG_HOME/ferry/config.toml).",
)
@click.version_option(version=__version__, prog_name="ferry")
@click.pass_context
def app(ctx: click.Context, config_path: Path | None) -> None:
    """ferry — sync a self-hosted RomM library to a local ES-DE install."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


app.add_command(ping)
