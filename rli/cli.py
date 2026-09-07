"""rli command-line interface."""

from __future__ import annotations

import typer

from rli.archive.cli import app as archive_app
from rli.config import load_config
from rli.db import connect, init_db
from rli.models.time import now_utc
from rli.snapshots.daily import run_daily_snapshot
from rli.snapshots.targets import DEFAULT_TARGETS_PATH, load_targets, upsert_companies

app = typer.Typer(
    name="rli",
    help="Role-Liveness Investigator — evidence-backed job posting liveness analysis.",
    no_args_is_help=True,
)
app.add_typer(archive_app, name="archive")

# Default DB path shared by every command below, for consistency.
_DEFAULT_DB_PATH = "./data/rli.db"


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


@app.command("load-targets")
def load_targets_command(
    targets: str = typer.Option(
        str(DEFAULT_TARGETS_PATH),
        "--targets",
        help="Path to the targets CSV (company_name, website_domain, ats, tenant, ...).",
    ),
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
) -> None:
    """Load verified ATS targets from CSV and upsert their `companies` rows."""
    init_db(db)
    conn = connect(db)
    try:
        loaded = load_targets(targets)
        count = upsert_companies(conn, loaded, now=now_utc())
        typer.echo(f"Upserted {count} companies from {targets}")
    finally:
        conn.close()


def _matches_only(target_tenant: str, only: list[str] | None) -> bool:
    if not only:
        return True
    tenant_lower = target_tenant.lower()
    # Each --only value may itself be a comma-separated list, so both
    # `--only a --only b` and `--only a,b` are accepted.
    wanted = {
        item.strip().lower()
        for value in only
        for item in value.split(",")
        if item.strip()
    }
    return tenant_lower in wanted


@app.command("snapshot")
def snapshot_command(
    targets: str = typer.Option(
        str(DEFAULT_TARGETS_PATH),
        "--targets",
        help="Path to the targets CSV (company_name, website_domain, ats, tenant, ...).",
    ),
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help="Restrict to these tenants (repeatable, and/or comma-separated), matched "
        "case-insensitively against the targets CSV's tenant column.",
    ),
) -> None:
    """Run the daily board-snapshot capture for the given targets (spec.md §4/§5)."""
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        loaded = load_targets(targets)
        filtered = [t for t in loaded if _matches_only(t.tenant, only)]
        upsert_companies(conn, filtered, now=now_utc())
        summary = run_daily_snapshot(conn, cfg, filtered, now=now_utc())
        typer.echo(summary.describe())
    finally:
        conn.close()


@app.command("snapshot-status")
def snapshot_status_command(
    db: str = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite database file.",
    ),
) -> None:
    """Print each company's most recent board-snapshot capture, plus the coverage-gap count."""
    init_db(db)
    conn = connect(db)
    try:
        rows = conn.execute(
            """
            SELECT
                c.company_id AS company_id,
                latest.captured_at AS last_captured_at,
                latest.coverage_status AS coverage_status
            FROM companies c
            LEFT JOIN (
                SELECT bs.company_id, bs.captured_at, bs.coverage_status
                FROM board_snapshots bs
                INNER JOIN (
                    SELECT company_id, MAX(captured_at) AS max_captured_at
                    FROM board_snapshots
                    GROUP BY company_id
                ) m ON m.company_id = bs.company_id AND m.max_captured_at = bs.captured_at
            ) latest ON latest.company_id = c.company_id
            ORDER BY c.company_id
            """
        ).fetchall()
        for row in rows:
            typer.echo(
                f"{row['company_id']}: last_captured_at={row['last_captured_at']} "
                f"coverage_status={row['coverage_status']}"
            )

        gap_count = conn.execute(
            "SELECT COUNT(*) AS n FROM capture_attempts WHERE ok = 0"
        ).fetchone()["n"]
        typer.echo(f"coverage gaps (failed capture_attempts): {gap_count}")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
