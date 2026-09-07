"""`rli.archive.coverage` — per-company stats and Markdown report generation."""

from __future__ import annotations

import datetime as dt

from rli.archive.coverage import compute_coverage, write_coverage_report
from rli.models.time import to_utc_z
from rli.probes.persist import record_capture_attempt, save_board_snapshot


def _insert_company(conn, company_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (company_id, name, company_id, to_utc_z(dt.datetime.now(dt.UTC))),
    )
    conn.commit()


def _dt(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


def test_compute_coverage_median_and_longest_gap(conn) -> None:
    _insert_company(conn, "acme.com", "Acme")

    # Captures on Jan 1, Jan 11 (10-day gap), Jan 41 (30-day gap).
    save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=_dt(2025, 1, 1),
        coverage_status="complete",
        jobs=[],
        source="archive",
    )
    save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=_dt(2025, 1, 11),
        coverage_status="complete",
        jobs=[],
        source="archive",
    )
    save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=_dt(2025, 2, 10),
        coverage_status="complete",
        jobs=[],
        source="archive",
    )
    record_capture_attempt(
        conn,
        company_id="acme.com",
        target="https://example.com/bad",
        attempted_at=_dt(2025, 1, 5),
        source="archive",
        ok=False,
        error="boom",
        retryable=True,
    )

    stats = compute_coverage(conn)
    assert len(stats) == 1
    s = stats[0]
    assert s.company_id == "acme.com"
    assert s.capture_count == 3
    assert s.first_captured_at.startswith("2025-01-01")
    assert s.last_captured_at.startswith("2025-02-10")
    # gaps: 10 days, 30 days -> median 20.0, longest 30.0
    assert s.median_gap_days == 20.0
    assert s.longest_gap_days == 30.0
    assert s.failed_attempts == 1


def test_compute_coverage_ignores_own_source_snapshots(conn) -> None:
    _insert_company(conn, "acme.com", "Acme")
    save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=_dt(2025, 1, 1),
        coverage_status="complete",
        jobs=[],
        source="own",
    )

    stats = compute_coverage(conn)
    assert stats[0].capture_count == 0
    assert stats[0].median_gap_days is None
    assert stats[0].longest_gap_days is None


def test_compute_coverage_company_with_no_captures_still_listed(conn) -> None:
    _insert_company(conn, "ghost.com", "Ghost Co")
    record_capture_attempt(
        conn,
        company_id="ghost.com",
        target="https://example.com/x",
        attempted_at=_dt(2025, 1, 1),
        source="archive",
        ok=False,
        error="down",
        retryable=True,
    )

    stats = compute_coverage(conn)
    assert len(stats) == 1
    s = stats[0]
    assert s.capture_count == 0
    assert s.first_captured_at is None
    assert s.longest_gap_days is None
    assert s.failed_attempts == 1


def test_compute_coverage_single_capture_has_no_gaps(conn) -> None:
    _insert_company(conn, "solo.com", "Solo")
    save_board_snapshot(
        conn,
        company_id="solo.com",
        captured_at=_dt(2025, 1, 1),
        coverage_status="complete",
        jobs=[],
        source="archive",
    )
    stats = compute_coverage(conn)
    assert stats[0].capture_count == 1
    assert stats[0].median_gap_days is None
    assert stats[0].longest_gap_days is None


def test_write_coverage_report_markdown(tmp_path, conn) -> None:
    _insert_company(conn, "acme.com", "Acme")
    save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=_dt(2025, 1, 1),
        coverage_status="complete",
        jobs=[],
        source="archive",
    )
    save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=_dt(2025, 1, 21),
        coverage_status="complete",
        jobs=[],
        source="archive",
    )

    out_path = tmp_path / "report.md"
    result_path = write_coverage_report(conn, out_path)

    assert result_path == out_path
    text = out_path.read_text(encoding="utf-8")
    assert "acme.com" in text
    assert "Acme" in text
    assert "| 2 |" in text  # capture_count column
    assert "20.0" in text  # median/longest gap of 20 days
