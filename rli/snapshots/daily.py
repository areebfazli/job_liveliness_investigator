"""Daily board-snapshot cron job (spec.md §4/§5; PLAN.md M1 bullet 2).

`run_daily_snapshot` captures each target company's current open jobs once
per day via `rli.probes.board_snapshot`, and maintains per-posting lifecycle
state (`first_observed`, `last_seen_open`, `first_seen_absent`,
`reappeared_at`) per spec.md §5. A failed/throttled capture is recorded as a
`capture_attempts` coverage gap and never treated as an absence (spec.md
§4): on failure, no `postings`/`posting_snapshots`/`board_snapshots` rows are
touched for that company at all this run.

Idempotency: this job is **idempotent by skipping** a company that already
has a `board_snapshots` row for today (UTC calendar day), not by upserting
the header. A same-day rerun therefore makes zero network calls and zero
lifecycle writes for companies already captured — it does not "top up" or
overwrite that day's capture.

Each capture also keeps the ATS's own stated dates per job (Greenhouse
`first_published` / `updated_at`, Ashby `publishedAt`) in
`board_snapshot_jobs`, and after all captures a bounded pass reads JSON-LD
`datePosted` from the job pages of Lever postings captured this run that have
no date yet (`rli.snapshots.page_dates`; `[page_dates]` in config.toml). A
page that cannot be fetched is a coverage gap only — the pass never fails the
snapshot and never touches posting lifecycle state.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from rli.config import Config
from rli.models.time import to_utc_z
from rli.net import NetClient
from rli.probes.base import ProbeContext
from rli.probes.board_snapshot import BoardJob, board_snapshot
from rli.probes.persist import record_capture_attempt, save_board_snapshot
from rli.snapshots.page_dates import PageDateSummary, collect_lever_page_dates
from rli.snapshots.targets import Target

__all__ = ["DailySnapshotSummary", "run_daily_snapshot"]

# Fallback canonical URL patterns, matching rli.resolvers.detect's canonical
# URL construction per ATS, used only when a BoardJob has no url of its own.
_FALLBACK_URL = {
    "greenhouse": "https://boards.greenhouse.io/{tenant}/jobs/{job_id}",
    "ashby": "https://jobs.ashbyhq.com/{tenant}/{job_id}",
    "lever": "https://jobs.lever.co/{tenant}/{job_id}",
}


@dataclass
class DailySnapshotSummary:
    """Outcome counters for one `run_daily_snapshot` call."""

    companies_ok: int = 0
    companies_failed: int = 0
    companies_skipped: int = 0
    postings_new: int = 0
    postings_absent: int = 0
    postings_reappeared: int = 0
    page_dates: PageDateSummary = field(default_factory=PageDateSummary)

    def describe(self) -> str:
        # The first segment's exact shape is parsed by scripts/daily.sh; the
        # page-date counters are appended after it, never inserted into it.
        return (
            f"companies: ok={self.companies_ok} failed={self.companies_failed} "
            f"skipped={self.companies_skipped} | postings: new={self.postings_new} "
            f"absent={self.postings_absent} reappeared={self.postings_reappeared} "
            f"| {self.page_dates.describe()}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _day_bounds_utc_z(now: datetime) -> tuple[str, str]:
    """Return `[start_of_day, start_of_next_day)` as `to_utc_z` strings.

    The calendar day is the UTC day (normalize `now` to UTC first), so
    idempotency-by-skip is well-defined regardless of `now`'s original tz.
    """
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return to_utc_z(start), to_utc_z(end)


def _already_captured_today(conn: sqlite3.Connection, company_id: str, now: datetime) -> bool:
    day_start, day_end = _day_bounds_utc_z(now)
    row = conn.execute(
        """
        SELECT 1 FROM board_snapshots
        WHERE company_id = ? AND captured_at >= ? AND captured_at < ?
        LIMIT 1
        """,
        (company_id, day_start, day_end),
    ).fetchone()
    return row is not None


def _canonical_url(ats: str, tenant: str, job: BoardJob) -> str:
    if job.url:
        return job.url
    return _FALLBACK_URL[ats].format(tenant=tenant, job_id=job.job_id)


def _apply_posting_lifecycle(
    conn: sqlite3.Connection,
    *,
    target: Target,
    jobs: list[BoardJob],
    now: datetime,
    summary: DailySnapshotSummary,
) -> None:
    now_str = to_utc_z(now)
    today_job_ids = {job.job_id for job in jobs}

    for job in jobs:
        posting_id = f"{target.ats}:{target.tenant}:{job.job_id}"
        canonical_url = _canonical_url(target.ats, target.tenant, job)

        existing = conn.execute(
            "SELECT posting_id, first_seen_absent, reappeared_at "
            "FROM postings WHERE posting_id = ?",
            (posting_id,),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO postings (
                    posting_id, company_id, ats, ats_tenant_id, ats_job_id,
                    canonical_url, title, team, location,
                    created_at, updated_at, first_observed, last_seen_open
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    posting_id,
                    target.company_id,
                    target.ats,
                    target.tenant,
                    job.job_id,
                    canonical_url,
                    job.title,
                    job.team,
                    job.location,
                    now_str,
                    now_str,
                    now_str,
                    now_str,
                ),
            )
            summary.postings_new += 1
        else:
            conn.execute(
                """
                UPDATE postings SET
                    canonical_url = ?, title = ?, team = ?, location = ?,
                    updated_at = ?, last_seen_open = ?
                WHERE posting_id = ?
                """,
                (canonical_url, job.title, job.team, job.location, now_str, now_str, posting_id),
            )
            # Reappearance rule (single-cycle model; multi-cycle repost
            # detection via replacement_job_id is explicit M2 scope):
            # currently-absent means first_seen_absent is set and
            # reappeared_at is not. Only that state transitions here; a
            # posting that already reappeared once, or was never absent, is
            # left untouched.
            if existing["first_seen_absent"] is not None and existing["reappeared_at"] is None:
                conn.execute(
                    "UPDATE postings SET reappeared_at = ? WHERE posting_id = ?",
                    (now_str, posting_id),
                )
                summary.postings_reappeared += 1

        conn.execute(
            """
            INSERT INTO posting_snapshots
                (posting_id, captured_at, source, status, content_hash, capture_url)
            VALUES (?, ?, 'own', 'open', ?, ?)
            """,
            (posting_id, now_str, job.description_hash, job.url),
        )

    # Absence detection: postings for this exact (company_id, ats, tenant)
    # that are "currently open" (never absent, or absent-then-reappeared)
    # but whose ats_job_id is not in today's set.
    currently_open_rows = conn.execute(
        """
        SELECT posting_id, ats_job_id, canonical_url, first_seen_absent
        FROM postings
        WHERE company_id = ? AND ats = ? AND ats_tenant_id = ?
          AND (first_seen_absent IS NULL OR reappeared_at IS NOT NULL)
        """,
        (target.company_id, target.ats, target.tenant),
    ).fetchall()

    for row in currently_open_rows:
        if row["ats_job_id"] in today_job_ids:
            continue
        # Transition to absent. Never overwrite an existing first_seen_absent
        # (deliberate M1 simplification: a posting that reappeared once and
        # disappears again keeps its ORIGINAL absence date on postings, even
        # though it gets a fresh 'absent' posting_snapshots row below).
        if row["first_seen_absent"] is None:
            conn.execute(
                "UPDATE postings SET first_seen_absent = ? WHERE posting_id = ?",
                (now_str, row["posting_id"]),
            )
        conn.execute(
            """
            INSERT INTO posting_snapshots
                (posting_id, captured_at, source, status, content_hash, capture_url)
            VALUES (?, ?, 'own', 'absent', NULL, ?)
            """,
            (row["posting_id"], now_str, row["canonical_url"]),
        )
        summary.postings_absent += 1


def run_daily_snapshot(
    conn: sqlite3.Connection,
    cfg: Config,
    targets: list[Target],
    now: datetime,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> DailySnapshotSummary:
    """Capture today's board for every target and update posting lifecycle state.

    Builds exactly ONE `NetClient` for the whole run via
    `NetClient.from_config(cfg, probe="board_snapshot", sleep=sleep)`,
    deliberately without `conn=` — so the generic `tool_cache` (1-hour TTL
    against real wall-clock time, shared with the future live agent loop) is
    never consulted here: a daily snapshot must always fetch fresh data, and
    must not cross-contaminate that shared cache table. The single client is
    reused across every target so the per-host `RateLimiter` state is shared
    across companies on the same ATS host.
    """
    summary = DailySnapshotSummary()
    net_client = NetClient.from_config(cfg, probe="board_snapshot", sleep=sleep)
    # Lever companies captured successfully THIS run: the only ones whose
    # postings the page-date pass may visit (see rli.snapshots.page_dates).
    lever_captured: list[str] = []
    try:
        for target in targets:
            if _already_captured_today(conn, target.company_id, now):
                summary.companies_skipped += 1
                continue

            ctx = ProbeContext(
                conn=conn,
                config=cfg,
                net_client_factory=lambda _name: net_client,
                now=lambda: now,
            )
            result = board_snapshot(target.ats, target.tenant, ctx)

            if not result.ok:
                record_capture_attempt(
                    conn,
                    company_id=target.company_id,
                    target=f"{target.ats}:{target.tenant}",
                    attempted_at=now,
                    source="own",
                    ok=False,
                    error=result.error,
                    retryable=result.retryable,
                )
                summary.companies_failed += 1
                continue

            jobs: list[BoardJob] = result.data["jobs"]
            save_board_snapshot(
                conn,
                company_id=target.company_id,
                captured_at=now,
                coverage_status="complete",
                jobs=jobs,
                source="own",
            )
            _apply_posting_lifecycle(conn, target=target, jobs=jobs, now=now, summary=summary)
            conn.commit()
            summary.companies_ok += 1
            if target.ats == "lever":
                lever_captured.append(target.company_id)

        # After every board capture is committed, so nothing here can delay
        # or undo one. Contained: a page-date problem is a coverage gap, never
        # a failed snapshot (rli.snapshots.page_dates).
        try:
            summary.page_dates = collect_lever_page_dates(
                conn, cfg, net_client, company_ids=lever_captured, now=now
            )
        except Exception:  # noqa: BLE001 - belt to page_dates' own braces
            conn.rollback()
    finally:
        net_client.close()

    return summary
