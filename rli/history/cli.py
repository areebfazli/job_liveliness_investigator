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

ONE COMPANY PER TRANSACTION
---------------------------

Those four steps run per company, inside `BEGIN IMMEDIATE` … `COMMIT`, and
the next company is not started until the previous one has committed.

That is a durability requirement, not a tidiness one. The clear in steps 1-2
is destructive, and on 2026-09-21 a corpus-wide rebuild was OOM-killed
between the clear and the rewrite: `repost_links` was left empty and every
`replacement_job_id` NULL across all 351 companies, with no way to get them
back short of re-deriving. With a transaction per company, an interrupted
rebuild leaves every company either fully re-derived or exactly as it was —
never cleared-but-not-rewritten. Scoping it per company (rather than wrapping
the whole run in one transaction) also keeps the write set small and lets a
long rebuild make durable progress instead of holding the write lock for its
whole duration.

The memory half of that incident is fixed in `rli.history.matching` and
`rli.history.closures`, which hold one company's derived state at a time.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import typer

from rli.config import load_config
from rli.db import connect, init_db
from rli.history.closures import ClosureApplySummary, apply_to_postings
from rli.history.matching import RepostLinkSummary, link_reposts
from rli.history.sample import export_match_sample
from rli.models.time import now_utc

__all__ = ["app", "rebuild"]

app = typer.Typer(
    name="history",
    help="Closure derivation, repost matching and the hand-check match sample.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

DEFAULT_DB_PATH = "./data/rli.db"
DEFAULT_SAMPLE_PATH = "./data/match_sample.csv"


def _clear_derived_links(
    conn: sqlite3.Connection, company_id: str | None, *, commit: bool = True
) -> tuple[int, int]:
    """Clear repost-derived posting columns and `repost_links`.

    Returns `(postings_cleared, links_deleted)`. Scoped to `company_id` when
    given; `repost_links.company_id` is the scope for both statements, so a
    per-company rebuild never touches another company's derived state.

    `commit=False` keeps the clear inside the caller's transaction, which is
    the whole point of the per-company transaction in `rebuild`: a clear
    that commits on its own is a window in which the company has no derived
    state at all.
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
    if commit:
        conn.commit()
    return cleared, deleted


def _rebuild_company_ids(conn: sqlite3.Connection, company_id: str | None) -> list[str]:
    """Every company a full rebuild must visit, in a stable order.

    The UNION matters: a company with `repost_links` but no `board_snapshots`
    (its captures were deleted, say) still has stale derived state that the
    old single-statement clear removed, and skipping it would silently leave
    that state behind. Such a company derives no new links, so visiting it
    is just the clear.
    """
    if company_id is not None:
        return [company_id]
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT company_id FROM board_snapshots
            UNION
            SELECT company_id FROM repost_links
            ORDER BY company_id
            """
        )
    ]


@contextmanager
def _company_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run one company's clear-and-rewrite as a single atomic unit.

    `BEGIN IMMEDIATE` takes the write lock up front rather than on the first
    write, so a concurrent writer is refused (or waits out `busy_timeout`)
    before this company's derived state has been cleared instead of halfway
    through rewriting it.

    `isolation_level = None` hands transaction control to this function for
    the duration: the sqlite3 driver would otherwise open implicit
    transactions of its own around the DML inside, which is exactly the
    behaviour that let an interrupted rebuild commit a clear without its
    rewrite. It is restored afterwards so callers see the connection they
    handed in.
    """
    conn.commit()  # close any implicit transaction the driver has open
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation


def _accumulate[Summary: ClosureApplySummary | RepostLinkSummary](
    total: Summary, part: Summary
) -> Summary:
    """Add every counter of `part` into `total` (both are all-int dataclasses).

    Keeps a per-company rebuild reporting the same totals the corpus-wide
    one always did, without each summary having to grow an `__add__`.
    """
    for field in dataclasses.fields(total):
        setattr(total, field.name, getattr(total, field.name) + getattr(part, field.name))
    return total


def rebuild(
    conn: sqlite3.Connection,
    company_id: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[int, int, ClosureApplySummary, RepostLinkSummary]:
    """Clear repost-derived state, then re-derive closures and repost links.

    Returns `(postings_cleared, links_deleted, closure_summary,
    repost_summary)`, summed over the companies visited. Importable so tests
    (and any future orchestration) can call the operation without going
    through Typer.

    Each company is cleared and rewritten inside its own transaction (see
    the module docstring), so an interruption — an OOM kill, a SIGTERM, a
    raised exception — can never leave a company cleared but not rewritten.

    `now` pins the timestamp written to `postings.updated_at` /
    `repost_links.matched_at` for the whole run, so one rebuild is one
    instant rather than one instant per company.
    """
    cfg = load_config()
    reference = now or now_utc()

    cleared_total = 0
    deleted_total = 0
    closure_summary = ClosureApplySummary()
    repost_summary = RepostLinkSummary()

    for cid in _rebuild_company_ids(conn, company_id):
        with _company_transaction(conn):
            cleared, deleted = _clear_derived_links(conn, cid, commit=False)
            cleared_total += cleared
            deleted_total += deleted
            _accumulate(closure_summary, apply_to_postings(conn, cid, now=reference, commit=False))
            _accumulate(repost_summary, link_reposts(conn, cfg, cid, now=reference, commit=False))

    return cleared_total, deleted_total, closure_summary, repost_summary


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
