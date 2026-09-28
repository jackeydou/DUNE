from typing import Annotated

import typer

from swarmeval import __version__

app = typer.Typer(
    name="swarm",
    help="Launch agent swarms against safety cases and inspect their trajectories.",
    no_args_is_help=True,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"swarm {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the swarm version and exit.",
        ),
    ] = False,
) -> None:
    """Launch agent swarms against safety cases and inspect their trajectories."""
