import logging
from pathlib import Path

import click

from ferry import __version__
from ferry.cli.config_cmd import config_group
from ferry.cli.detect import detect
from ferry.cli.launch_hooks import install_launch_hooks, uninstall_launch_hooks
from ferry.cli.ping import ping
from ferry.cli.reconcile import reconcile
from ferry.cli.status import status
from ferry.cli.sync import sync
from ferry.cli.units import install_units, uninstall_units


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


app.add_command(config_group)
app.add_command(detect)
app.add_command(install_launch_hooks)
app.add_command(install_units)
app.add_command(ping)
app.add_command(reconcile)
app.add_command(status)
app.add_command(sync)
app.add_command(uninstall_launch_hooks)
app.add_command(uninstall_units)
