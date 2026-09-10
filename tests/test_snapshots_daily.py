"""`rli.snapshots.daily.run_daily_snapshot` — daily board-snapshot cron (spec.md §4/§5)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
import respx

from rli.config import load_config
from rli.db import connect, init_db
from rli.snapshots.daily import run_daily_snapshot
from rli.snapshots.targets import Target, upsert_companies

GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"

TARGET = Target(company_name="Acme", website_domain="acme.com", ats="greenhouse", tenant="acme")

DAY1 = datetime(2026, 1, 1, tzinfo=UTC)
DAY2 = datetime(2026, 1, 2, tzinfo=UTC)
DAY3 = datetime(2026, 1, 3, tzinfo=UTC)


def _gh_job(job_id: str, title: str = "Engineer") -> dict:
    return {
        "id": int(job_id),
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
        "content": f"desc-{job_id}",
    }


@pytest.fixture
def db_conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "rli.sqlite3"
    init_db(db_path)
    conn = connect(db_path)
    upsert_companies(conn, [TARGET], now=DAY1)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def cfg():
    return load_config()


def _no_sleep(_seconds: float) -> None:
    return None


def test_three_day_lifecycle_new_absent_reappeared(db_conn, cfg) -> None:
    conn = db_conn

    # --- Day 1: jobs "1" and "2" open ---
    with respx.mock:
        respx.get(GH_URL).mock(
            return_value=httpx.Response(200, json={"jobs": [_gh_job("1"), _gh_job("2")]})
        )
        summary1 = run_daily_snapshot(conn, cfg, [TARGET], DAY1, sleep=_no_sleep)

    assert summary1.companies_ok == 1
    assert summary1.postings_new == 2
    assert summary1.postings_absent == 0
    assert summary1.postings_reappeared == 0

    header_count = conn.execute(
        "SELECT COUNT(*) AS n FROM board_snapshots WHERE company_id = 'acme.com'"
    ).fetchone()["n"]
    assert header_count == 1

    posting1 = conn.execute(
        "SELECT * FROM postings WHERE posting_id = 'greenhouse:acme:1'"
    ).fetchone()
    posting2 = conn.execute(
        "SELECT * FROM postings WHERE posting_id = 'greenhouse:acme:2'"
    ).fetchone()
    assert posting1["first_observed"] == posting1["last_seen_open"] == "2026-01-01T00:00:00.000000Z"
    assert posting2["first_observed"] == posting2["last_seen_open"] == "2026-01-01T00:00:00.000000Z"
    assert posting1["first_seen_absent"] is None
    assert posting2["first_seen_absent"] is None

    snap_statuses = conn.execute(
        "SELECT posting_id, status FROM posting_snapshots ORDER BY posting_id"
    ).fetchall()
    assert [(r["posting_id"], r["status"]) for r in snap_statuses] == [
        ("greenhouse:acme:1", "open"),
        ("greenhouse:acme:2", "open"),
    ]

    # --- Day 2: only job "1" remains ---
    with respx.mock:
        respx.get(GH_URL).mock(return_value=httpx.Response(200, json={"jobs": [_gh_job("1")]}))
        summary2 = run_daily_snapshot(conn, cfg, [TARGET], DAY2, sleep=_no_sleep)

    assert summary2.companies_ok == 1
    assert summary2.postings_absent == 1
    assert summary2.postings_new == 0
    assert summary2.postings_reappeared == 0

    posting1 = conn.execute(
        "SELECT * FROM postings WHERE posting_id = 'greenhouse:acme:1'"
    ).fetchone()
    posting2 = conn.execute(
        "SELECT * FROM postings WHERE posting_id = 'greenhouse:acme:2'"
    ).fetchone()
    assert posting1["last_seen_open"] == "2026-01-02T00:00:00.000000Z"
    assert posting1["first_seen_absent"] is None
    assert posting2["first_seen_absent"] == "2026-01-02T00:00:00.000000Z"
    assert posting2["reappeared_at"] is None
    # posting 1 unaffected: still only "open" snapshots
    posting1_statuses = [
        r["status"]
        for r in conn.execute(
            "SELECT status FROM posting_snapshots WHERE posting_id = 'greenhouse:acme:1' "
            "ORDER BY id"
        ).fetchall()
    ]
    assert posting1_statuses == ["open", "open"]
    posting2_new_snap = conn.execute(
        "SELECT status FROM posting_snapshots WHERE posting_id = 'greenhouse:acme:2' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert posting2_new_snap["status"] == "absent"

    # --- Day 3: both "1" and "2" open again ---
    with respx.mock:
        respx.get(GH_URL).mock(
            return_value=httpx.Response(200, json={"jobs": [_gh_job("1"), _gh_job("2")]})
        )
        summary3 = run_daily_snapshot(conn, cfg, [TARGET], DAY3, sleep=_no_sleep)

    assert summary3.postings_reappeared == 1
    posting2 = conn.execute(
        "SELECT * FROM postings WHERE posting_id = 'greenhouse:acme:2'"
    ).fetchone()
    assert posting2["reappeared_at"] == "2026-01-03T00:00:00.000000Z"
    # first_seen_absent stays at day 2's value: not cleared, not overwritten.
    assert posting2["first_seen_absent"] == "2026-01-02T00:00:00.000000Z"


def test_failed_capture_is_coverage_gap_not_absence(db_conn, cfg) -> None:
    conn = db_conn

    with respx.mock:
        respx.get(GH_URL).mock(side_effect=httpx.ConnectError("boom"))
        summary = run_daily_snapshot(conn, cfg, [TARGET], DAY1, sleep=_no_sleep)

    assert summary.companies_failed == 1
    assert summary.companies_ok == 0

    capture_attempt = conn.execute(
        "SELECT * FROM capture_attempts WHERE company_id = 'acme.com'"
    ).fetchone()
    assert capture_attempt is not None
    assert capture_attempt["ok"] == 0

    assert conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM posting_snapshots").fetchone()["n"] == 0


def test_same_day_rerun_is_idempotent_by_skip(db_conn, cfg) -> None:
    conn = db_conn

    with respx.mock:
        respx.get(GH_URL).mock(
            return_value=httpx.Response(200, json={"jobs": [_gh_job("1"), _gh_job("2")]})
        )
        first = run_daily_snapshot(conn, cfg, [TARGET], DAY1, sleep=_no_sleep)

    assert first.companies_ok == 1
    board_count_after_first = conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()[
        "n"
    ]
    postings_count_after_first = conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]
    snaps_count_after_first = conn.execute(
        "SELECT COUNT(*) AS n FROM posting_snapshots"
    ).fetchone()["n"]

    # No mocked route registered this time: a rerun must make zero network calls.
    with respx.mock:
        second = run_daily_snapshot(conn, cfg, [TARGET], DAY1, sleep=_no_sleep)

    assert second.companies_skipped == 1
    assert second.companies_ok == 0
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()["n"]
        == board_count_after_first
        == 1
    )
    postings_count_after_second = conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]
    assert postings_count_after_second == postings_count_after_first
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM posting_snapshots").fetchone()["n"]
        == snaps_count_after_first
    )
