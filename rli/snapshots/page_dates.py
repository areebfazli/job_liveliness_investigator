"""Lever job-page publish dates, collected by the daily snapshot (spec.md §3).

Lever's board API exposes no trusted date (spec.md §3: "undocumented date
fields are not trusted as ATS-native publish dates"), but the human-facing job
page at `jobs.lever.co/{tenant}/{id}` carries a schema.org `JobPosting`
JSON-LD block whose `datePosted` spec.md §3 ranks as `page_structured`. This
module reads it, once per posting, and stores it in `posting_page_dates` so
the replay builder (`rli.replay.build.capture_date_claims`) can cite it at
any `T >= fetched_at`.

Rules, each load-bearing:

* **Availability is the fetch time.** `fetched_at` is the `NetResult`'s own
  timestamp, i.e. when we actually had the page. spec.md §3: "Do not
  backdate current discoveries merely because the underlying event happened
  earlier" — `date_posted` is the EVENT time (`source_event_at`), never the
  availability time.
* **Only postings today's capture listed.** Candidates are Lever postings of
  companies captured successfully in THIS run whose `last_seen_open` is this
  run's `now`. So the pass only ever reads a page we just saw listed, and a
  same-day rerun (every company skipped) makes zero page fetches — the
  collector's idempotency-by-skip contract (`rli.snapshots.daily`) holds.
* **Fetched once.** A row with status `ok` (date obtained) or `no_date`
  (page fetched fine, no usable `datePosted`) is never refetched.
* **A failed fetch is a coverage gap, nothing more.** It is recorded as
  status `failed` with the error, retried on a later run until
  `[page_dates].max_attempts_per_posting`, and NEVER touches `postings`,
  `posting_snapshots` or `capture_attempts` (the last feeds
  `rli.history.features.coverage_window`, which must keep meaning board
  coverage). No exception escapes this module into the snapshot.
* **Bounded.** At most `[page_dates].max_fetches_per_run` fetches, newest
  `first_observed` first (so new postings are served first and the remainder
  of the cap gradually catches up older still-open ones), and the pass stops
  after `max_consecutive_failures` failures in a row.

The page is fetched through the daily run's own `NetClient` (probe
`board_snapshot`, whose allowlist includes `jobs.lever.co`), so the per-host
rate limit and the retry/backoff policy are shared with the board fetches.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from rli.config import Config
from rli.models.time import to_utc_z
from rli.net import NetClient
from rli.resolvers import jsonld

__all__ = ["PageDateSummary", "collect_lever_page_dates", "page_date_candidates"]


@dataclass
class PageDateSummary:
    """Outcome counters for one page-date pass."""

    attempted: int = 0
    dated: int = 0
    no_date: int = 0
    failed: int = 0
    stopped_early: bool = False

    def describe(self) -> str:
        text = (
            f"lever_page_dates: fetched={self.attempted} dated={self.dated} "
            f"no_date={self.no_date} failed={self.failed}"
        )
        return text + (" (stopped: consecutive failures)" if self.stopped_early else "")


def page_date_candidates(
    conn: sqlite3.Connection,
    *,
    company_ids: Iterable[str],
    now: datetime,
    limit: int,
    max_attempts: int,
) -> list[sqlite3.Row]:
    """Lever postings listed in this run's capture that still need a page date.

    Newest `first_observed` first; `posting_id` breaks ties so the order never
    depends on the query plan. A posting is a candidate when it has no
    `posting_page_dates` row yet, or only a `failed` one with attempts left.
    """
    ids = sorted(set(company_ids))
    if not ids or limit <= 0:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"""
        SELECT p.posting_id, p.canonical_url
        FROM postings AS p
        LEFT JOIN posting_page_dates AS d ON d.posting_id = p.posting_id
        WHERE p.ats = 'lever'
          AND p.company_id IN ({placeholders})
          AND p.last_seen_open = ?
          AND p.canonical_url LIKE 'https://%'
          AND (d.posting_id IS NULL OR (d.status = 'failed' AND d.attempts < ?))
        ORDER BY p.first_observed DESC, p.posting_id
        LIMIT ?
        """,
        (*ids, to_utc_z(now), max_attempts, limit),
    ).fetchall()


def _record(
    conn: sqlite3.Connection,
    *,
    posting_id: str,
    page_url: str,
    status: str,
    attempted_at: datetime,
    date_posted_raw: str | None = None,
    date_posted: datetime | None = None,
    fetched_at: datetime | None = None,
    error: str | None = None,
) -> None:
    # The conflict branch only ever fires for a `failed` row (candidates
    # exclude `ok` / `no_date`); the WHERE makes that structural, so an
    # obtained date can never be overwritten by a later attempt.
    conn.execute(
        """
        INSERT INTO posting_page_dates
            (posting_id, page_url, status, date_posted_raw, date_posted, fetched_at,
             attempts, last_attempt_at, last_error)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT (posting_id) DO UPDATE SET
            page_url = excluded.page_url,
            status = excluded.status,
            date_posted_raw = excluded.date_posted_raw,
            date_posted = excluded.date_posted,
            fetched_at = excluded.fetched_at,
            attempts = posting_page_dates.attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            last_error = excluded.last_error
        WHERE posting_page_dates.status = 'failed'
        """,
        (
            posting_id,
            page_url,
            status,
            date_posted_raw,
            to_utc_z(date_posted) if date_posted is not None else None,
            to_utc_z(fetched_at) if fetched_at is not None else None,
            to_utc_z(attempted_at),
            error,
        ),
    )
    conn.commit()


def collect_lever_page_dates(
    conn: sqlite3.Connection,
    cfg: Config,
    net: NetClient,
    *,
    company_ids: Iterable[str],
    now: datetime,
) -> PageDateSummary:
    """Fetch and store JSON-LD `datePosted` for this run's Lever candidates.

    Never raises: any unexpected error is contained per posting (recorded as
    a `failed` attempt) and, failing that, for the whole pass — the board
    captures this runs after are already committed and must not be undone or
    reported as failed because a page could not be read.
    """
    summary = PageDateSummary()
    settings = cfg.page_dates
    if not settings.enabled or settings.max_fetches_per_run <= 0:
        return summary

    try:
        candidates = page_date_candidates(
            conn,
            company_ids=company_ids,
            now=now,
            limit=settings.max_fetches_per_run,
            max_attempts=settings.max_attempts_per_posting,
        )
    except sqlite3.Error:
        return summary

    consecutive_failures = 0
    for row in candidates:
        posting_id = str(row["posting_id"])
        url = str(row["canonical_url"])
        summary.attempted += 1
        try:
            fetch = jsonld.fetch_job_posting(net, url)
            if not fetch.ok:
                summary.failed += 1
                consecutive_failures += 1
                _record(
                    conn,
                    posting_id=posting_id,
                    page_url=url,
                    status="failed",
                    attempted_at=fetch.fetched_at,
                    error=fetch.error or f"HTTP {fetch.status}",
                )
            else:
                consecutive_failures = 0
                posting = fetch.data
                if posting is not None and posting.date_posted is not None:
                    summary.dated += 1
                    _record(
                        conn,
                        posting_id=posting_id,
                        page_url=fetch.url,
                        status="ok",
                        attempted_at=fetch.fetched_at,
                        date_posted_raw=posting.date_posted_raw,
                        date_posted=posting.date_posted,
                        fetched_at=fetch.fetched_at,
                    )
                else:
                    summary.no_date += 1
                    _record(
                        conn,
                        posting_id=posting_id,
                        page_url=fetch.url,
                        status="no_date",
                        attempted_at=fetch.fetched_at,
                        date_posted_raw=posting.date_posted_raw if posting else None,
                        fetched_at=fetch.fetched_at,
                        error=(
                            "no JobPosting JSON-LD on the page"
                            if posting is None
                            else "JobPosting JSON-LD without a parseable datePosted"
                        ),
                    )
        except Exception as exc:  # noqa: BLE001 - a page must never fail the snapshot
            summary.failed += 1
            consecutive_failures += 1
            try:
                conn.rollback()
                _record(
                    conn,
                    posting_id=posting_id,
                    page_url=url,
                    status="failed",
                    attempted_at=now,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except sqlite3.Error:
                pass
        if consecutive_failures >= settings.max_consecutive_failures:
            summary.stopped_early = True
            break
    return summary
