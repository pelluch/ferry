import logging
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
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase logging verbosity (-v: info, -vv: debug).",
)
@click.version_option(version=__version__, prog_name="ferry")
@click.pass_context
def app(ctx: click.Context, config_path: Path | None, verbose: int) -> None:
    """ferry — sync a self-hosted RomM library to a local ES-DE install."""
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def _configure_logging(verbosity: int) -> None:
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
    root = logging.getLogger("ferry")
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
        root.addHandler(handler)


app.add_command(ping)
