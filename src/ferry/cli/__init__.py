import click

from ferry import __version__
from ferry.cli.ping import ping


@click.group()
@click.version_option(version=__version__, prog_name="ferry")
def app() -> None:
    """ferry — sync a self-hosted RomM library to a local ES-DE install."""


app.add_command(ping)
