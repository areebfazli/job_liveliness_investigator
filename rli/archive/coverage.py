"""Per-company archive-backfill coverage reporting (PLAN.md M1 bullet 4).

Per spec.md §4 ("Wayback ... always record capture coverage"), this reports
how much of each company's history the archive backfill actually recovered:
how many usable captures landed as `board_snapshots` rows, how far apart
they are in time, and how many `capture_attempts` failed outright — so a
company with 40 captures spread evenly across a year reads very differently
from one with 40 captures crammed into a single week plus one 300-day gap.

Only `source = 'archive'` rows are considered: this module reports on the
Wayback backfill specifically, not on live (`source = 'own'`) snapshots.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

from rli.models.time import parse_utc

__all__ = ["CompanyCoverage", "compute_coverage", "write_coverage_report"]


@dataclass(frozen=True, slots=True)
class CompanyCoverage:
    """Archive-capture coverage stats for one company."""

    company_id: str
    name: str | None
    capture_count: int
    first_captured_at: str | None
    last_captured_at: str | None
    median_gap_days: float | None
    longest_gap_days: float | None
    failed_attempts: int


def compute_coverage(conn: sqlite3.Connection) -> list[CompanyCoverage]:
    """Per-company archive coverage stats over `board_snapshots`/`capture_attempts`.

    Ordered by `company_id`. A company with zero archive `board_snapshots`
    rows still appears (with `capture_count=0` and `None` gap stats) as long
    as it has a `companies` row, so a company that was attempted but never
    successfully captured is visible rather than silently absent from the
    report.
    """
    companies = conn.execute(
        "SELECT company_id, name FROM companies ORDER BY company_id"
    ).fetchall()

    results: list[CompanyCoverage] = []
    for row in companies:
        company_id = row["company_id"]
        name = row["name"]

        captured_rows = conn.execute(
            """
            SELECT captured_at FROM board_snapshots
            WHERE company_id = ? AND source = 'archive'
            ORDER BY captured_at ASC
            """,
            (company_id,),
        ).fetchall()
        timestamps = [r["captured_at"] for r in captured_rows]

        failed_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM capture_attempts
            WHERE company_id = ? AND source = 'archive' AND ok = 0
            """,
            (company_id,),
        ).fetchone()
        failed_attempts = int(failed_row["n"])

        if not timestamps:
            results.append(
                CompanyCoverage(
                    company_id=company_id,
                    name=name,
                    capture_count=0,
                    first_captured_at=None,
                    last_captured_at=None,
                    median_gap_days=None,
                    longest_gap_days=None,
                    failed_attempts=failed_attempts,
                )
            )
            continue

        parsed = [parse_utc(ts) for ts in timestamps]
        gap_days = [
            (parsed[i] - parsed[i - 1]).total_seconds() / 86400.0 for i in range(1, len(parsed))
        ]
        median_gap = statistics.median(gap_days) if gap_days else None
        longest_gap = max(gap_days) if gap_days else None

        results.append(
            CompanyCoverage(
                company_id=company_id,
                name=name,
                capture_count=len(parsed),
                first_captured_at=timestamps[0],
                last_captured_at=timestamps[-1],
                median_gap_days=median_gap,
                longest_gap_days=longest_gap,
                failed_attempts=failed_attempts,
            )
        )
    return results


def _fmt(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


def write_coverage_report(conn: sqlite3.Connection, path: str | Path) -> Path:
    """Write a Markdown coverage table (one row per company) to `path`.

    Creates parent directories as needed. Returns the resolved output
    `Path`.
    """
    stats = compute_coverage(conn)
    out_path = Path(path)
    if out_path.parent and str(out_path.parent) not in ("", "."):
        out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Archive backfill coverage report",
        "",
        "| company_id | name | captures | first_captured_at | last_captured_at "
        "| median_gap_days | longest_gap_days | failed_attempts |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in stats:
        lines.append(
            f"| {s.company_id} | {s.name or ''} | {s.capture_count} "
            f"| {s.first_captured_at or '-'} | {s.last_captured_at or '-'} "
            f"| {_fmt(s.median_gap_days)} | {_fmt(s.longest_gap_days)} | {s.failed_attempts} |"
        )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
