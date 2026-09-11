"""`rli.archive` CLI — archive backfill sub-app (PLAN.md M1 bullet 4).

Not wired into the main `rli` entrypoint yet — a different agent owns
`rli/cli.py` and will mount this sub-app there. Until then, run it directly:

    uv run python -m rli.archive.cli backfill --months 12
    uv run python -m rli.archive.cli coverage

To mount into `rli/cli.py`::

    from rli.archive.cli import app as archive_app
    app.add_typer(archive_app, name="archive")
"""

from __future__ import annotations

from pathlib import Path

import typer

from rli.archive.backfill import filter_targets, read_targets_csv, run_backfill
from rli.archive.coverage import write_coverage_report
from rli.config import REPO_ROOT, load_config
from rli.db import connect, init_db

__all__ = ["app"]

app = typer.Typer(
    name="archive",
    help="Wayback Machine archive backfill for historical board snapshots.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

DEFAULT_TARGETS_PATH = REPO_ROOT / "scripts" / "targets.csv"
DEFAULT_DB_PATH = "./data/rli.db"
DEFAULT_COVERAGE_PATH = "./data/archive_coverage.md"


@app.command("backfill")
def backfill_command(
    months: int = typer.Option(
        12, "--months", help="How many months back from now to search Wayback captures."
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Filter targets by tenant/company_name/website_domain (case-insensitive substring).",
    ),
    db: str = typer.Option(
        DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file (created if missing)."
    ),
    limit_captures: int | None = typer.Option(
        None,
        "--limit-captures",
        help="Cap how many captures (most recent first) are fetched+parsed per company.",
    ),
    targets_path: Path = typer.Option(
        DEFAULT_TARGETS_PATH, "--targets", help="Path to the targets CSV to read."
    ),
) -> None:
    """Backfill historical board snapshots from Wayback Machine captures."""
    init_db(db)
    conn = connect(db)
    cfg = load_config()

    try:
        targets = read_targets_csv(targets_path)
        targets = filter_targets(targets, only)

        if not targets:
            typer.echo("No targets matched.")
            raise typer.Exit(code=0)

        summaries = run_backfill(cfg, conn, targets, months=months, limit_captures=limit_captures)

        total_found = total_parsed = total_failed = 0
        for s in summaries:
            pattern_note = f" [{s.pattern_used}]" if s.pattern_used else " [no usable captures]"
            typer.echo(
                f"{s.company_id} ({s.ats}/{s.tenant}): "
                f"found={s.captures_found} parsed={s.captures_parsed} "
                f"failed={s.captures_failed}{pattern_note}"
            )
            total_found += s.captures_found
            total_parsed += s.captures_parsed
            total_failed += s.captures_failed

        typer.echo(
            f"\nTotals: companies={len(summaries)} found={total_found} "
            f"parsed={total_parsed} failed={total_failed}"
        )
    finally:
        conn.close()


@app.command("coverage")
def coverage_command(
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
    out: Path = typer.Option(
        Path(DEFAULT_COVERAGE_PATH), "--out", help="Path to write the coverage report."
    ),
) -> None:
    """Write a per-company archive coverage report as Markdown."""
    init_db(db)
    conn = connect(db)
    try:
        out_path = write_coverage_report(conn, out)
        typer.echo(f"Wrote coverage report to {out_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
