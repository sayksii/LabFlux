"""Command line entry point; commands are added alongside each milestone."""

from pathlib import Path

import typer

from labflux import __version__
from labflux.config import load_settings
from labflux.logging import configure_logging

app = typer.Typer(no_args_is_help=True, help="LabFlux resource manager and job scheduler")


@app.callback()
def main(ctx: typer.Context, config: Path | None = typer.Option(None, "--config", "-c")) -> None:
    """Read a TOML configuration, then run a command."""
    ctx.obj = load_settings(config)
    configure_logging()


@app.command()
def version() -> None:
    """Show the installed package version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
