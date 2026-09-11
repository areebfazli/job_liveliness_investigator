"""`rli.history` CLI — closure/repost rebuild + hand-check sample export.

Not wired into the main `rli` entrypoint here — a different agent owns
`rli/cli.py`. Until it is mounted, run it directly::

    uv run python -m rli.history.cli rebuild --db ./data/rli.db
    uv run python -m rli.history.cli sample --db ./data/rli.db --n 50

To mount into `rli/cli.py`::

    from rli.history.cli import app as history_app
    app.add_typer(history_app, name="history")

`rebuild` exists because both derived repost columns are WRITE-ONCE by
design: `rli.history.matching.link_reposts` never overwrites a non-NULL
`postings.replacement_job_id`, which is what makes re-running it safe but
also what makes a threshold change invisible until the previously derived
state is cleared. `rebuild` clears exactly the state that was derived from
`repost_links` and then re-derives it, in the one order that is correct:

1. NULL out `replacement_job_id` / `reappeared_at` on every posting that
   appears as an `old_posting_id` in `repost_links` — i.e. only rows this
   pipeline itself wrote. A posting whose `reappeared_at` came from
   `rli.history.closures` (the same job id listed again) and that was never
   repost-linked is left untouched.
2. Delete those `repost_links` rows.
3. Re-run `apply_to_postings`, which restores any `reappeared_at` that was
   closure-derived rather than repost-derived (it only ever fills a NULL).
4. Re-run `link_reposts`.

Step 3 is what makes the clear in step 1 safe to do bluntly: a
closure-derived `reappeared_at` that step 1 removed is put straight back by
step 3 from the board captures, while a repost-derived one is re-decided by
step 4 under the current `[matching]` thresholds.

Running `rebuild` twice in a row is a no-op on the second run (same counts,
same rows), which is the property `tests/test_history_cli.py` asserts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from rli.config import load_config
from rli.db import connect, init_db
from rli.history.closures import apply_to_postings
from rli.history.matching import link_reposts
from rli.history.sample import export_match_sample

__all__ = ["app", "rebuild"]

app = typer.Typer(
    name="history",
    help="Closure derivation, repost matching and the hand-check match sample.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

DEFAULT_DB_PATH = "./data/rli.db"
DEFAULT_SAMPLE_PATH = "./data/match_sample.csv"


def _clear_derived_links(conn: sqlite3.Connection, company_id: str | None) -> tuple[int, int]:
    """Clear repost-derived posting columns and `repost_links`.

    Returns `(postings_cleared, links_deleted)`. Scoped to `company_id` when
    given; `repost_links.company_id` is the scope for both statements, so a
    per-company rebuild never touches another company's derived state.
    """
    where = "WHERE company_id = ?" if company_id is not None else ""
    params: tuple[str, ...] = (company_id,) if company_id is not None else ()

    cleared = conn.execute(
        f"""
        UPDATE postings
           SET replacement_job_id = NULL,
               reappeared_at = NULL
         WHERE posting_id IN (
                   SELECT old_posting_id FROM repost_links {where}
               )
        """,  # noqa: S608 - `where` is a fixed literal, the value is bound
        params,
    ).rowcount
    deleted = conn.execute(
        f"DELETE FROM repost_links {where}",  # noqa: S608 - see above
        params,
    ).rowcount
    conn.commit()
    return cleared, deleted


def rebuild(
    conn: sqlite3.Connection, company_id: str | None = None
) -> tuple[int, int, object, object]:
    """Clear repost-derived state, then re-derive closures and repost links.

    Returns `(postings_cleared, links_deleted, closure_summary,
    repost_summary)`. Importable so tests (and any future orchestration) can
    call the operation without going through Typer.
    """
    cfg = load_config()
    cleared, deleted = _clear_derived_links(conn, company_id)
    closure_summary = apply_to_postings(conn, company_id)
    repost_summary = link_reposts(conn, cfg, company_id)
    return cleared, deleted, closure_summary, repost_summary


@app.command("rebuild")
def rebuild_command(
    db: str = typer.Option(
        DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file (created if missing)."
    ),
    company: str | None = typer.Option(
        None,
        "--company",
        help="Restrict the rebuild to one company_id (its website domain). Default: all.",
    ),
) -> None:
    """Reset repost-derived state and re-run closures + repost matching."""
    init_db(db)
    conn = connect(db)
    try:
        cleared, deleted, closure_summary, repost_summary = rebuild(conn, company)
        typer.echo(f"cleared derived columns on {cleared} postings; deleted {deleted} repost_links")
        typer.echo(f"closures: {closure_summary}")
        typer.echo(f"reposts:  {repost_summary}")
    finally:
        conn.close()


@app.command("sample")
def sample_command(
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file."),
    out: Path = typer.Option(
        Path(DEFAULT_SAMPLE_PATH), "--out", help="Where to write the hand-check CSV."
    ),
    n: int = typer.Option(50, "--n", help="How many top-ranked candidate pairs to export."),
    company: str | None = typer.Option(
        None, "--company", help="Restrict the sample to one company_id. Default: all."
    ),
    matches_only: bool = typer.Option(
        False,
        "--matches-only",
        help="Export only accepted matches instead of the top-ranked candidates.",
    ),
) -> None:
    """Export the hand-checked match-precision sample (spec.md §4)."""
    cfg = load_config()
    init_db(db)
    conn = connect(db)
    try:
        written = export_match_sample(
            conn, cfg, out, n, company_id=company, matches_only=matches_only
        )
        typer.echo(f"Wrote {written} candidate rows to {out}")
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    app()
