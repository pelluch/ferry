import click

from ferry import __version__


@click.command()
def ping() -> None:
    """Smoke-test the configured RomM connection."""
    click.echo(f"ferry {__version__}")
    click.echo("ping: RomM client not yet wired up — landing in checkpoint 3.")
