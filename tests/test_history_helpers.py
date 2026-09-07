"""Synthetic board-history builders shared by the `rli.history` tests.

Named `test_history_helpers` so it sits inside the `tests/test_history*.py`
family; it contains no tests of its own. Everything here writes through
`rli.probes.persist.save_board_snapshot` or plain SQL against the temp-DB
`conn` fixture — no network, no archive code, and never the live
`data/rli.db`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from rli.models.time import to_utc_z
from rli.probes.board_snapshot import BoardJob
from rli.probes.persist import record_capture_attempt, save_board_snapshot

COMPANY = "acme.com"
OTHER_COMPANY = "globex.com"

# Fixed origin so every derived day offset is an exact whole number of days.
DAY0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def at(days: float) -> datetime:
    """`DAY0` shifted by `days` (fractions allowed)."""
    return DAY0 + timedelta(days=days)


def job(
    job_id: str,
    *,
    title: str | None = None,
    team: str | None = None,
    location: str | None = None,
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
        (company_id, company_id, company_id, to_utc_z(DAY0)),
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


def add_attempt(
    conn: sqlite3.Connection,
    attempted_at: datetime,
    *,
    company_id: str = COMPANY,
    source: str = "own",
    ok: bool = False,
) -> None:
    add_company(conn, company_id)
    record_capture_attempt(
        conn,
        company_id=company_id,
        target="greenhouse:acme",
        attempted_at=attempted_at,
        source=source,
        ok=ok,
        error=None if ok else "throttled",
        retryable=True,
    )


def add_posting(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    company_id: str = COMPANY,
    ats: str = "greenhouse",
    tenant: str | None = "acme",
    posting_id: str | None = None,
    title: str | None = None,
    team: str | None = None,
    location: str | None = None,
    first_observed: datetime | None = None,
    last_seen_open: datetime | None = None,
    first_seen_absent: datetime | None = None,
    reappeared_at: datetime | None = None,
) -> str:
    """Insert a `postings` row shaped exactly like `rli.snapshots.daily` writes."""
    add_company(conn, company_id)
    resolved_id = posting_id or f"{ats}:{tenant}:{job_id}"
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
            f"https://boards.greenhouse.io/{tenant}/jobs/{job_id}",
            title,
            team,
            location,
            to_utc_z(DAY0),
            to_utc_z(DAY0),
            None if first_observed is None else to_utc_z(first_observed),
            None if last_seen_open is None else to_utc_z(last_seen_open),
            None if first_seen_absent is None else to_utc_z(first_seen_absent),
            None if reappeared_at is None else to_utc_z(reappeared_at),
        ),
    )
    conn.commit()
    return resolved_id


def posting_row(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM postings WHERE posting_id = ?", (posting_id,)).fetchone()
    assert row is not None, f"no posting row for {posting_id!r}"
    return row


def interval_by_job(intervals: list, job_id: str):
    matches = [i for i in intervals if i.job_id == job_id]
    assert len(matches) == 1, f"expected exactly one interval for {job_id!r}, got {len(matches)}"
    return matches[0]
