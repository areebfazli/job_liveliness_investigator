"""rli command-line interface."""

from __future__ import annotations

import typer

from rli.db import init_db

app = typer.Typer(
    name="rli",
    help="Role-Liveness Investigator — evidence-backed job posting liveness analysis.",
    no_args_is_help=True,
)


@app.callback()
def _main() -> None:
    """Role-Liveness Investigator CLI."""


@app.command("init-db")
def init_db_command(
    path: str = typer.Option(
        "./data/rli.db",
        "--path",
        help="Path to the SQLite database file to create/initialize.",
    ),
) -> None:
    """Create the SQLite schema at PATH (idempotent)."""
    init_db(path)
    typer.echo(f"Initialized database at {path}")


if __name__ == "__main__":
    app()
