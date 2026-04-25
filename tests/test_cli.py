from click.testing import CliRunner

from ferry import __version__
from ferry.cli import app


def test_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ferry" in result.output
    assert "ping" in result.output


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_ping_placeholder_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["ping"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert "checkpoint 3" in result.output
