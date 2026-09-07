"""init_db creates the full schema, is idempotent, and enforces invariants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rli.db import BUSY_TIMEOUT_MS, SCHEMA_VERSION, connect, init_db, schema_version

EXPECTED_TABLES = {
    "companies",
    "postings",
    "posting_snapshots",
    "board_snapshots",
    "board_snapshot_jobs",
    "capture_attempts",
    "company_events",
    "evidence",
    "runs",
    "run_steps",
    "outcomes",
    "llm_cache",
    "tool_cache",
    # schema version 2 (rli.history.matching audit trail)
    "repost_links",
    # schema version 3 (rli.replay point-in-time dataset)
    "replay_datasets",
    "replay_cases",
    "replay_probe_results",
}

# (table, required columns, forbidden columns)
KEY_COLUMNS = [
    (
        "postings",
        {"first_observed", "last_seen_open", "first_seen_absent", "reappeared_at",
         "replacement_job_id"},
        set(),
    ),
    (
        "posting_snapshots",
        {"source", "status", "content_hash", "capture_url"},
        {"first_observed"},
    ),
    (
        "board_snapshots",
        {"source", "coverage_status"},
        {"open_job_ids"},
    ),
    (
        "board_snapshot_jobs",
        {"board_snapshot_id", "job_id", "title", "team", "location",
         "description_hash", "url"},
        set(),
    ),
    (
        "capture_attempts",
        {"company_id", "target", "attempted_at", "source", "ok", "error", "retryable"},
        set(),
    ),
    (
        "evidence",
        {"run_id"},
        set(),
    ),
    (
        "runs",
        {"input_url", "system", "mode", "replay_at", "policy_version", "config_hash",
         "total_cost_usd", "total_latency_ms"},
        set(),
    ),
    (
        "run_steps",
        {"probe_name", "args_hash", "step_index"},
        set(),
    ),
    (
        "tool_cache",
        {"fetched_at"},
        {"created_at"},
    ),
]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row["name"] for row in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_company(conn: sqlite3.Connection, company_id: str = "acme.com") -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) "
        "VALUES (?, ?, ?, ?)",
        (company_id, "Acme", company_id, "2026-01-01T00:00:00Z"),
    )


def _insert_run(
    conn: sqlite3.Connection, run_id: str = "r1", posting_id: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO runs (id, posting_id, input_url, system, mode, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, posting_id, "https://example.com/job/1", "A", "live",
         "2026-01-01T00:00:00Z", "running"),
    )


def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        assert _table_names(conn) == EXPECTED_TABLES
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    init_db(db_path)  # must not raise
    conn = connect(db_path)
    try:
        assert _table_names(conn) == EXPECTED_TABLES
        assert schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_init_db_creates_parent_dirs(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "rli.db"
    init_db(db_path)
    assert db_path.exists()


@pytest.mark.parametrize("table,required,forbidden", KEY_COLUMNS)
def test_key_columns(
    tmp_path: Path, table: str, required: set[str], forbidden: set[str]
) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        columns = _column_names(conn, table)
        missing = required - columns
        assert not missing, f"{table} missing columns: {missing}"
        present_forbidden = forbidden & columns
        assert not present_forbidden, f"{table} has forbidden columns: {present_forbidden}"
    finally:
        conn.close()


def test_evidence_pk_is_run_id_and_id(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _insert_company(conn)
        _insert_run(conn, run_id="r1")
        _insert_run(conn, run_id="r2")

        def _insert_evidence(run_id: str) -> None:
            conn.execute(
                "INSERT INTO evidence "
                "(id, run_id, posting_id, probe, claim_type, value, source_url, "
                "source_quality, available_at, fetched_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "e1",
                    run_id,
                    "resolve_posting",
                    "first_published",
                    "2026-08-20T10:00:00Z",
                    "https://boards.greenhouse.io/acme/jobs/123",
                    "ats_native",
                    "2026-08-20T10:00:00Z",
                    "2026-08-20T10:00:00Z",
                ),
            )

        # Same run-local id "e1" under two different run_ids is fine.
        _insert_evidence("r1")
        _insert_evidence("r2")
        conn.commit()

        rows = conn.execute("SELECT run_id, id FROM evidence ORDER BY run_id").fetchall()
        assert [(row["run_id"], row["id"]) for row in rows] == [("r1", "e1"), ("r2", "e1")]

        # Duplicate (run_id, id) is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence("r1")
    finally:
        conn.close()


def test_evidence_posting_id_and_source_event_at_nullable(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _insert_company(conn)
        _insert_run(conn, run_id="r1")
        conn.execute(
            "INSERT INTO evidence "
            "(id, run_id, posting_id, probe, claim_type, value, source_url, "
            "source_quality, source_event_at, available_at, fetched_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                "e1",
                "r1",
                "company_events",
                "layoff",
                "true",
                "https://news.example.com/article",
                "news",
                "2026-08-20T10:00:00Z",
                "2026-08-20T10:00:00Z",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT posting_id, source_event_at FROM evidence WHERE id = 'e1'"
        ).fetchone()
        assert row["posting_id"] is None
        assert row["source_event_at"] is None
    finally:
        conn.close()


def test_runs_posting_id_nullable(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _insert_run(conn, run_id="r1", posting_id=None)
        conn.commit()
        row = conn.execute("SELECT posting_id FROM runs WHERE id = 'r1'").fetchone()
        assert row["posting_id"] is None
    finally:
        conn.close()


def test_capture_attempts_failed_row_insertable(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _insert_company(conn)
        conn.execute(
            "INSERT INTO capture_attempts "
            "(company_id, target, attempted_at, source, ok, error, retryable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("acme.com", "https://boards.greenhouse.io/acme", "2026-08-20T10:00:00Z",
             "own", 0, "HTTP 429", 1),
        )
        conn.commit()
        row = conn.execute(
            "SELECT ok, error, retryable FROM capture_attempts WHERE target = ?",
            ("https://boards.greenhouse.io/acme",),
        ).fetchone()
        assert row["ok"] == 0
        assert row["error"] == "HTTP 429"
        assert row["retryable"] == 1
    finally:
        conn.close()


def test_connect_applies_pragmas(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal"

        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert foreign_keys == 1

        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy_timeout == BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_foreign_keys_enforced_on_postings(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO postings "
                "(posting_id, company_id, ats, canonical_url, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("p1", "unknown-company.com", "greenhouse",
                 "https://boards.greenhouse.io/acme/jobs/1", "2026-08-20T10:00:00Z"),
            )
            conn.commit()
    finally:
        conn.close()


def test_init_db_raises_on_future_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError):
        init_db(db_path)
