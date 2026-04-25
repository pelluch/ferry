from pathlib import Path

import click

from ferry.adapters.detect import DetectedCandidate, detect_candidates
from ferry.domain.destination import PRESETS


@click.command()
def detect() -> None:
    """Probe the filesystem for known destination presets and suggest config."""
    candidates = detect_candidates()

    if not candidates:
        _print_no_candidates()
        return

    plural = "candidate" if len(candidates) == 1 else "candidates"
    click.echo(f"Found {len(candidates)} {plural}:")
    click.echo("")
    for c in candidates:
        _print_candidate(c)

    click.echo("")
    if len(candidates) == 1:
        click.echo("To use this destination, append to ~/.config/ferry/config.toml:")
        chosen = candidates[0].preset
    else:
        click.echo("Pick one and append to ~/.config/ferry/config.toml:")
        chosen = "<choice>"
    click.echo("")
    click.echo("    [destination]")
    click.echo(f'    preset = "{chosen}"')


def _print_candidate(c: DetectedCandidate) -> None:
    click.echo(f"  {c.preset}")
    click.echo(f"    ROMs:    {c.roms_base} {_status(c.roms_base)}")
    if c.bios_base is None:
        click.echo("    BIOS:    (per-emulator — no centralized BIOS root)")
    else:
        click.echo(f"    BIOS:    {c.bios_base} {_status(c.bios_base)}")
    click.echo("    Signals:")
    for sig in c.signals:
        click.echo(f"      - {sig}")
    click.echo("")


def _print_no_candidates() -> None:
    known = ", ".join(sorted(PRESETS))
    click.echo("No candidates detected.")
    click.echo("")
    click.echo("Set [destination] explicitly in ~/.config/ferry/config.toml:")
    click.echo("")
    click.echo("    [destination]")
    click.echo('    roms_base = "/path/to/roms"')
    click.echo('    # bios_base = "/path/to/bios"   # optional')
    click.echo("")
    click.echo(f"Or pick a known preset: {known}")


def _status(path: Path) -> str:
    if not path.exists():
        return "(missing)"
    if not path.is_dir():
        return "(not a directory)"
    return "(exists)"
