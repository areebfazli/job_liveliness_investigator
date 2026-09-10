"""Synthetic corpus builders shared by the `tests/test_eval_*.py` files.

Named `test_eval_helpers` so it sits inside the `tests/test_eval*.py` glob
(the suite that owns `rli/eval`); it contains no tests of its own, exactly
like `tests/test_history_helpers.py`. Everything here writes through
`rli.probes.persist.save_board_snapshot` or plain SQL against the temp-DB
`conn` fixture — no network, and never the live `data/rli.db`.

Unlike `tests/test_history_helpers.py` (anchored at a fixed `DAY0` in
January 2026), every builder here takes explicit `datetime`s so a test can
anchor its history relative to its own `now` — most `rli.eval` scenarios
need history that is deep enough to be "usable" (`min_history_days`) but
NOT old enough to look `long_lived` (`long_lived_days`), which a fixed
faraway anchor would make easy to get wrong by accident.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from rli.events.store import (
    CollectionStatus,
    CompanyEvent,
    upsert_event,
    write_collection_status_csv,
)
from rli.models.time import to_utc_z
from rli.probes.board_snapshot import BoardJob
from rli.probes.persist import save_board_snapshot

COMPANY = "acme.com"
OTHER_COMPANY = "globex.com"

# Fixed, arbitrary "created_at" stamp for rows whose creation time is not
# load-bearing for any assertion in these tests.
_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def job(
    job_id: str,
    *,
    title: str | None = "Backend Engineer",
    team: str | None = "Engineering",
    location: str | None = "Remote",
    description_hash: str | None = None,
    url: str | None = None,
) -> BoardJob:
    return BoardJob(
        job_id=job_id,
        title=title,
        team=team,
        location=location,
        description_hash=description_hash,
        url=url,
    )


def add_company(conn: sqlite3.Connection, company_id: str = COMPANY) -> str:
    conn.execute(
        """
        INSERT OR IGNORE INTO companies (company_id, name, website_domain, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (company_id, company_id, company_id, to_utc_z(_EPOCH)),
    )
    conn.commit()
    return company_id


def add_capture(
    conn: sqlite3.Connection,
    captured_at: datetime,
    jobs: list[BoardJob],
    *,
    company_id: str = COMPANY,
    source: str = "own",
    coverage_status: str = "complete",
) -> int:
    add_company(conn, company_id)
    return save_board_snapshot(
        conn,
        company_id=company_id,
        captured_at=captured_at,
        coverage_status=coverage_status,
        jobs=jobs,
        source=source,
    )


def add_posting(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    company_id: str = COMPANY,
    ats: str = "greenhouse",
    tenant: str | None = "acme",
    posting_id: str | None = None,
    title: str | None = "Backend Engineer",
    team: str | None = "Engineering",
    location: str | None = "Remote",
    canonical_url: str | None = None,
    created_at: datetime = _EPOCH,
    first_observed: datetime | None = None,
    last_seen_open: datetime | None = None,
    first_seen_absent: datetime | None = None,
    reappeared_at: datetime | None = None,
) -> str:
    """Insert a `postings` row shaped exactly like `rli.snapshots.daily` writes."""
    add_company(conn, company_id)
    resolved_id = posting_id or f"{ats}:{tenant}:{job_id}"
    url = canonical_url or f"https://boards.greenhouse.io/{tenant}/jobs/{job_id}"
    conn.execute(
        """
        INSERT INTO postings (
            posting_id, company_id, ats, ats_tenant_id, ats_job_id, canonical_url,
            title, team, location, created_at, updated_at,
            first_observed, last_seen_open, first_seen_absent, reappeared_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resolved_id,
            company_id,
            ats,
            tenant,
            job_id,
            url,
            title,
            team,
            location,
            to_utc_z(created_at),
            to_utc_z(created_at),
            None if first_observed is None else to_utc_z(first_observed),
            None if last_seen_open is None else to_utc_z(last_seen_open),
            None if first_seen_absent is None else to_utc_z(first_seen_absent),
            None if reappeared_at is None else to_utc_z(reappeared_at),
        ),
    )
    conn.commit()
    return resolved_id


def add_company_event(
    conn: sqlite3.Connection,
    *,
    event_date: date,
    available_at: datetime,
    company_id: str = COMPANY,
    event_type: str = "layoff",
    headline: str = "Acme lays off 20% of staff",
    materiality: str = "material",
    source_url: str = "https://news.example.com/acme-layoff",
) -> None:
    """Insert one `company_events` row (creating the `companies` row it needs).

    `available_at` is the article's own publish time, not collection time —
    see `rli.events.store`'s module docstring for why that is not backdating
    and why point-in-time replay depends on it.
    """
    add_company(conn, company_id)
    upsert_event(
        conn,
        CompanyEvent(
            company_id=company_id,
            event_type=event_type,  # type: ignore[arg-type]
            event_date=event_date,
            available_at=available_at,
            source_url=source_url,
            headline=headline,
            materiality=materiality,  # type: ignore[arg-type]
            collected_at=available_at,
        ),
    )


def write_status_csv(
    path: Path, searched_at: datetime | None, *, company_id: str = COMPANY, events_found: int = 1
) -> str:
    """Write a `collection_status.csv` and return its path as a string.

    `searched_at=None` writes an EMPTY file: "collection has run, but not for
    this company", which `rli.probes.company_events` reads as UNKNOWN and for
    which it emits no `company_events_searched` claim. Pin one of these on the
    run's `ProbeContext` (`collection_status_csv=`) so a test never depends on
    what the real `data/events/collection_status.csv` happens to contain.
    """
    rows = (
        []
        if searched_at is None
        else [
            CollectionStatus(
                company_id=company_id,
                searched_at=searched_at,
                queries_run=5,
                events_found=events_found,
            )
        ]
    )
    write_collection_status_csv(rows, path)
    return str(path)


def decision_branch(conn: sqlite3.Connection, run_id: str) -> str:
    """The spec.md §5 policy branch this run recorded, from its canonical trace.

    `rli.eval.runner.decide_and_finish` writes
    `policy_decision:<branch>:<quality rule>` as a controller step (spec.md §7
    makes `run_steps` the canonical trace), and the branch is deliberately not
    a field on `Decision` — so a test that wants to pin WHICH §5 row fired
    reads it from there rather than re-deriving it from the inputs, which
    would just be a second copy of the policy.
    """
    row = conn.execute(
        "SELECT decision_type FROM run_steps WHERE run_id = ? AND decision_type LIKE ?",
        (run_id, "policy_decision:%"),
    ).fetchone()
    assert row is not None, f"no policy_decision step for run {run_id!r}"
    return str(row["decision_type"]).split(":")[1]


def run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None, f"no runs row for {run_id!r}"
    return row


def run_steps(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index", (run_id,)
    ).fetchall()


def evidence_rows(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
