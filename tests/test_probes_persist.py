"""`rli.probes.persist` — caller-side persistence for probe results (spec.md §7)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from rli.probes.base import ProbeClaim
from rli.probes.board_snapshot import BoardJob
from rli.probes.persist import record_capture_attempt, save_board_snapshot, save_evidence

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _insert_company(conn: sqlite3.Connection, company_id: str = "acme.com") -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?, ?, ?, ?)",
        (company_id, "Acme", company_id, "2026-01-01T00:00:00Z"),
    )
    conn.commit()


def _insert_run(conn: sqlite3.Connection, run_id: str = "r1") -> None:
    conn.execute(
        "INSERT INTO runs (id, posting_id, input_url, system, mode, started_at, status) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?)",
        (run_id, "https://example.com/job/1", "A", "live", "2026-01-01T00:00:00Z", "running"),
    )
    conn.commit()


def test_record_capture_attempt_inserts_row(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    record_capture_attempt(
        conn,
        company_id="acme.com",
        target="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        attempted_at=NOW,
        ok=False,
        error="HTTP 503 after 3 retries",
        retryable=True,
    )
    row = conn.execute("SELECT * FROM capture_attempts").fetchone()
    assert row["company_id"] == "acme.com"
    assert row["ok"] == 0
    assert row["retryable"] == 1
    assert row["error"] == "HTTP 503 after 3 retries"
    assert row["attempted_at"] == "2026-09-07T00:00:00.000000Z"


def test_record_capture_attempt_success_has_no_error(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    record_capture_attempt(
        conn,
        company_id="acme.com",
        target="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        attempted_at=NOW,
        ok=True,
    )
    row = conn.execute("SELECT * FROM capture_attempts").fetchone()
    assert row["ok"] == 1
    assert row["error"] is None
    assert row["retryable"] is None


def test_save_board_snapshot_writes_header_and_job_rows(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    jobs = [
        BoardJob(
            job_id="1",
            title="Backend Engineer",
            team="Engineering",
            location="Remote",
            url="https://boards.greenhouse.io/acme/jobs/1",
            description_hash="abc123",
        ),
        BoardJob(
            job_id="2", title="Recruiter", team=None, location=None, url=None, description_hash=None
        ),
    ]

    snapshot_id = save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=NOW,
        coverage_status="complete",
        jobs=jobs,
    )

    header = conn.execute("SELECT * FROM board_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    assert header["company_id"] == "acme.com"
    assert header["coverage_status"] == "complete"
    assert header["source"] == "own"

    rows = conn.execute(
        "SELECT * FROM board_snapshot_jobs WHERE board_snapshot_id = ? ORDER BY job_id",
        (snapshot_id,),
    ).fetchall()
    assert [r["job_id"] for r in rows] == ["1", "2"]
    assert rows[0]["title"] == "Backend Engineer"
    assert rows[0]["description_hash"] == "abc123"
    assert rows[1]["team"] is None


def test_save_evidence_assigns_run_local_ids_and_persists(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    _insert_run(conn, run_id="r1")

    claims = [
        ProbeClaim(
            claim_type="first_published",
            value="2026-08-20T10:00:00Z",
            source_url="https://boards.greenhouse.io/acme/jobs/1",
            source_quality="ats_native",
            source_event_at=NOW,
            available_at=NOW,
            fetched_at=NOW,
        ),
        ProbeClaim(
            claim_type="updated_at",
            value="2026-08-25T00:00:00Z",
            source_url="https://boards.greenhouse.io/acme/jobs/1",
            source_quality="ats_native",
            available_at=NOW,
            fetched_at=NOW,
        ),
    ]

    items = save_evidence(conn, run_id="r1", probe="resolve_posting", claims=claims)

    assert [item.id for item in items] == ["e1", "e2"]
    assert all(item.run_id == "r1" for item in items)
    assert all(item.probe == "resolve_posting" for item in items)

    rows = conn.execute(
        "SELECT id, claim_type, source_event_at FROM evidence WHERE run_id = 'r1' ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in rows] == ["e1", "e2"]
    assert rows[0]["claim_type"] == "first_published"
    assert rows[0]["source_event_at"] == "2026-09-07T00:00:00.000000Z"
    assert rows[1]["source_event_at"] is None


def test_save_evidence_respects_start_index_for_multi_probe_runs(conn: sqlite3.Connection) -> None:
    _insert_company(conn)
    _insert_run(conn, run_id="r1")

    claim = ProbeClaim(
        claim_type="board_listing",
        value="Backend Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/1",
        source_quality="ats_native",
        available_at=NOW,
        fetched_at=NOW,
    )

    first_batch = save_evidence(conn, run_id="r1", probe="resolve_posting", claims=[claim])
    second_batch = save_evidence(
        conn, run_id="r1", probe="board_snapshot", claims=[claim], start_index=len(first_batch) + 1
    )

    assert [item.id for item in first_batch] == ["e1"]
    assert [item.id for item in second_batch] == ["e2"]

    count = conn.execute("SELECT COUNT(*) AS n FROM evidence WHERE run_id = 'r1'").fetchone()["n"]
    assert count == 2
