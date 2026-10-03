"""Publish dates kept by the daily collector and replayed point-in-time.

Covers, end to end:

* `BoardJob` carries Greenhouse `first_published` / `updated_at` and Ashby
  `publishedAt` from the BOARD listing; `save_board_snapshot` persists them
  normalized to UTC-Z.
* Schema version 4: a version-3 database (the real one's shape) upgrades
  idempotently, keeps its data, and the `posting_page_dates` DDL is identical
  in `schema.sql` and `rli.db`.
* `rli.replay.build.capture_date_claims` emits a date only when an own capture
  (or a Lever page fetch) at or before `T` carried it, with `available_at` =
  that capture's (fetch's) time — and the claim makes Q2 strong and passes the
  leakage checker.
* The Lever page-date pass in `rli snapshot`: success, failure-is-a-gap,
  per-run cap (newest first), no refetch once obtained, same-day rerun makes no
  fetch, and the consecutive-failure breaker.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import httpx
import pytest
import respx
from test_eval_helpers import add_capture, add_posting
from test_replay_helpers import (
    NOW,
    OPEN_JOB,
    TENANT,
    dev_everything_cutoff,
    mock_ats,
    seed_corpus,
)

from rli.config import Config, RateLimit
from rli.db import POSTING_PAGE_DATES_DDL, SCHEMA_VERSION, connect, init_db, schema_version
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import PolicyInputs
from rli.models.time import parse_utc, to_utc_z
from rli.policy.quality import evidence_quality_detail
from rli.probes.board_snapshot import BoardJob, board_snapshot
from rli.probes.persist import save_board_snapshot
from rli.replay.build import build_dataset, capture_date_claims, case_state_at
from rli.replay.leakage import check_dataset
from rli.replay.mode import ARCHIVE_BOARD_STATE_PROBE
from rli.replay.run import run_replay
from rli.snapshots.daily import run_daily_snapshot
from rli.snapshots.targets import Target, upsert_companies

GH_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
ASHBY_BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/acme"
LEVER_BOARD_URL = "https://api.lever.co/v0/postings/lev"

GH_TARGET = Target(company_name="Acme", website_domain="acme.com", ats="greenhouse", tenant="acme")
ASHBY_TARGET = Target(company_name="Ash", website_domain="ash.com", ats="ashby", tenant="acme")
LEVER_TARGET = Target(company_name="Lev", website_domain="lev.com", ats="lever", tenant="lev")

DAY1 = datetime(2026, 9, 1, tzinfo=UTC)
DAY2 = datetime(2026, 9, 2, tzinfo=UTC)

OPEN_POSTING = f"greenhouse:{TENANT}:{OPEN_JOB}"


def _no_sleep(_seconds: float) -> None:
    return None


def _fast(cfg: Config, **page_dates: object) -> Config:
    """`cfg` with unthrottled hosts (tests must not spin on real-time buckets)."""
    fast = {host: RateLimit(requests_per_second=1000.0, burst=1000) for host in cfg.rate_limits}
    return cfg.model_copy(
        update={
            "rate_limits": fast,
            "page_dates": cfg.page_dates.model_copy(update=page_dates),
        }
    )


# ---------------------------------------------------------------------------
# BoardJob dates (Greenhouse, Ashby) and their persistence
# ---------------------------------------------------------------------------


@respx.mock
def test_greenhouse_board_listing_dates_reach_boardjob(ctx_factory) -> None:
    respx.get(GH_BOARD_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 11,
                        "title": "Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
                        "first_published": "2026-08-20T10:00:00-04:00",
                        "updated_at": "2026-08-28T09:30:00-04:00",
                        "content": "x",
                    }
                ]
            },
        )
    )
    result = board_snapshot("greenhouse", "acme", ctx_factory())
    (only,) = result.data["jobs"]
    assert only.first_published == "2026-08-20T10:00:00-04:00"
    assert only.updated_at == "2026-08-28T09:30:00-04:00"


@respx.mock
def test_ashby_published_at_maps_to_first_published(ctx_factory) -> None:
    respx.get(ASHBY_BOARD_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "ab-1",
                        "title": "Designer",
                        "publishedAt": "2026-08-01T12:00:00.000Z",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/ab-1",
                    }
                ]
            },
        )
    )
    result = board_snapshot("ashby", "acme", ctx_factory())
    (only,) = result.data["jobs"]
    assert only.first_published == "2026-08-01T12:00:00.000Z"
    assert only.updated_at is None  # Ashby documents no updatedAt


@respx.mock
def test_lever_board_listing_never_yields_a_date(ctx_factory) -> None:
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(
            200, json=[{"id": "l1", "text": "Rep", "createdAt": 1755000000000}]
        )
    )
    (only,) = board_snapshot("lever", "acme", ctx_factory()).data["jobs"]
    assert only.first_published is None and only.updated_at is None


def test_save_board_snapshot_normalizes_dates_and_drops_garbage(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) "
        "VALUES ('acme.com', 'Acme', 'acme.com', '2026-01-01T00:00:00.000000Z')"
    )
    snapshot_id = save_board_snapshot(
        conn,
        company_id="acme.com",
        captured_at=DAY1,
        coverage_status="complete",
        jobs=[
            BoardJob(
                job_id="1",
                first_published="2026-08-20T10:00:00-04:00",
                updated_at="2026-08-28T13:30:00Z",
            ),
            BoardJob(job_id="2", first_published="not a date"),
            BoardJob(job_id="3"),
        ],
    )
    rows = {
        row["job_id"]: row
        for row in conn.execute(
            "SELECT job_id, first_published, updated_at FROM board_snapshot_jobs "
            "WHERE board_snapshot_id = ?",
            (snapshot_id,),
        )
    }
    assert rows["1"]["first_published"] == "2026-08-20T14:00:00.000000Z"
    assert rows["1"]["updated_at"] == "2026-08-28T13:30:00.000000Z"
    assert rows["2"]["first_published"] is None
    assert rows["3"]["first_published"] is None and rows["3"]["updated_at"] is None


@respx.mock
def test_daily_snapshot_persists_greenhouse_and_ashby_dates(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    upsert_companies(conn, [GH_TARGET, ASHBY_TARGET], now=DAY1)
    respx.get(GH_BOARD_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 11,
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
                        "first_published": "2026-08-20T10:00:00Z",
                        "updated_at": "2026-08-30T10:00:00Z",
                    }
                ]
            },
        )
    )
    respx.get(ASHBY_BOARD_URL).mock(
        return_value=httpx.Response(
            200, json={"jobs": [{"id": "ab-1", "publishedAt": "2026-08-01T00:00:00Z"}]}
        )
    )
    summary = run_daily_snapshot(conn, _fast(cfg), [GH_TARGET, ASHBY_TARGET], DAY1, sleep=_no_sleep)
    assert summary.companies_ok == 2

    stored = {
        row["job_id"]: (row["first_published"], row["updated_at"])
        for row in conn.execute("SELECT * FROM board_snapshot_jobs")
    }
    assert stored == {
        "11": ("2026-08-20T10:00:00.000000Z", "2026-08-30T10:00:00.000000Z"),
        "ab-1": ("2026-08-01T00:00:00.000000Z", None),
    }
    # No Lever target -> no page fetch was even considered.
    assert summary.page_dates.attempted == 0
    assert "companies: ok=2 failed=0 skipped=0 | postings: new=2" in summary.describe()


# ---------------------------------------------------------------------------
# Schema version 4 migration
# ---------------------------------------------------------------------------


def _schema_sql() -> str:
    return resources.files("rli.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _page_dates_section(text: str) -> str:
    start = text.index("CREATE TABLE IF NOT EXISTS posting_page_dates")
    end = text.index(");", start) + 2
    body = "\n".join(line.split("--")[0] for line in text[start:end].splitlines())
    return " ".join(body.split())


def _build_v3_database(path: Path) -> None:
    """Today's schema minus everything version 4 added: the real DB's shape."""
    sql = _schema_sql()
    columns = (
        "    url                 TEXT,\n"
        "    first_published     TEXT,\n"
        "    updated_at          TEXT\n"
    )
    assert columns in sql
    sql = sql.replace(columns, "    url                 TEXT\n")
    start = sql.index("CREATE TABLE IF NOT EXISTS posting_page_dates")
    sql = sql[:start] + sql[sql.index(");", start) + 2 :]
    assert "CREATE TABLE IF NOT EXISTS posting_page_dates" not in sql

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(sql)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT INTO companies (company_id, name, website_domain, created_at) "
            "VALUES ('acme.com', 'Acme', 'acme.com', '2026-01-01T00:00:00.000000Z')"
        )
        conn.execute(
            "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
            "VALUES ('acme.com', '2026-09-01T00:00:00.000000Z', 'own', 'complete')"
        )
        conn.execute(
            "INSERT INTO board_snapshot_jobs (board_snapshot_id, job_id, title) "
            "VALUES (1, '11', 'Engineer')"
        )
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_version_3_database_upgrades_to_4_and_a_second_run_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite3"
    _build_v3_database(path)
    conn = connect(path)
    try:
        assert schema_version(conn) == 3
        assert "first_published" not in _columns(conn, "board_snapshot_jobs")
    finally:
        conn.close()

    init_db(path)
    init_db(path)  # idempotent: second run must neither fail nor change anything

    conn = connect(path)
    try:
        assert SCHEMA_VERSION == 4
        assert schema_version(conn) == 4
        assert {"first_published", "updated_at"} <= _columns(conn, "board_snapshot_jobs")
        assert "attempts" in _columns(conn, "posting_page_dates")
        row = conn.execute("SELECT * FROM board_snapshot_jobs").fetchone()
        assert (row["job_id"], row["title"]) == ("11", "Engineer")
        assert row["first_published"] is None and row["updated_at"] is None
    finally:
        conn.close()


def test_migration_skips_a_column_a_previous_attempt_already_added(tmp_path: Path) -> None:
    """`ALTER TABLE ADD COLUMN` has no IF NOT EXISTS; the migration must cope."""
    path = tmp_path / "half.sqlite3"
    _build_v3_database(path)
    conn = sqlite3.connect(str(path))
    conn.execute("ALTER TABLE board_snapshot_jobs ADD COLUMN first_published TEXT")
    conn.commit()
    conn.close()

    init_db(path)

    conn = connect(path)
    try:
        assert schema_version(conn) == 4
        assert {"first_published", "updated_at"} <= _columns(conn, "board_snapshot_jobs")
    finally:
        conn.close()


def test_the_posting_page_dates_ddl_is_identical_in_both_places() -> None:
    assert _page_dates_section(_schema_sql()) == _page_dates_section(POSTING_PAGE_DATES_DDL)


# ---------------------------------------------------------------------------
# capture_date_claims: point-in-time
# ---------------------------------------------------------------------------


def _dated(job_id: str, first_published: str | None, updated_at: str | None = None) -> BoardJob:
    return BoardJob(
        job_id=job_id,
        title="Backend Engineer",
        url=f"https://boards.greenhouse.io/{TENANT}/jobs/{job_id}",
        first_published=first_published,
        updated_at=updated_at,
    )


def _seed_dated(conn: sqlite3.Connection) -> None:
    """`seed_corpus` plus own captures that carry dates from -20 days on."""
    seed_corpus(conn)
    add_capture(
        conn,
        NOW - timedelta(days=20),
        [_dated(OPEN_JOB, "2026-08-10T09:00:00Z", "2026-08-15T09:00:00Z")],
    )
    add_capture(
        conn,
        NOW - timedelta(days=5),
        [_dated(OPEN_JOB, "2026-08-10T09:00:00Z", "2026-09-01T09:00:00Z")],
    )


def test_no_date_claim_before_any_capture_carried_one(conn: sqlite3.Connection) -> None:
    _seed_dated(conn)
    # Captures at -60/-45/-30 listed the job but carried no date.
    assert capture_date_claims(conn, OPEN_POSTING, NOW - timedelta(days=21)) == []


def test_date_claim_is_available_from_the_earliest_capture_that_carried_it(
    conn: sqlite3.Connection,
) -> None:
    _seed_dated(conn)
    claims = {c.claim_type: c for c in capture_date_claims(conn, OPEN_POSTING, NOW)}

    published = claims["first_published"]
    assert published.source_quality == "ats_native"
    assert published.source_event_at == datetime(2026, 8, 10, 9, tzinfo=UTC)
    # Carried by both the -20 and the -5 capture: the EARLIEST wins.
    assert published.available_at == NOW - timedelta(days=20)
    assert published.source_url == f"https://boards.greenhouse.io/{TENANT}/jobs/{OPEN_JOB}"

    # updated_at changed at -5: the latest value, available from -5.
    updated = claims["updated_at"]
    assert updated.source_event_at == datetime(2026, 9, 1, 9, tzinfo=UTC)
    assert updated.available_at == NOW - timedelta(days=5)


def test_a_capture_after_t_is_never_used(conn: sqlite3.Connection) -> None:
    _seed_dated(conn)
    t = NOW - timedelta(days=10)
    claims = {c.claim_type: c for c in capture_date_claims(conn, OPEN_POSTING, t)}
    assert claims["updated_at"].source_event_at == datetime(2026, 8, 15, 9, tzinfo=UTC)
    assert all(c.available_at <= t for c in claims.values())


def test_archive_captures_never_supply_a_date(conn: sqlite3.Connection) -> None:
    seed_corpus(conn)
    add_capture(
        conn,
        NOW - timedelta(days=20),
        [_dated(OPEN_JOB, "2026-08-10T09:00:00Z")],
        source="archive",
    )
    assert capture_date_claims(conn, OPEN_POSTING, NOW) == []


def _lever_posting(conn: sqlite3.Connection) -> str:
    return add_posting(
        conn,
        job_id="lv-1",
        ats="lever",
        tenant="lev",
        canonical_url="https://jobs.lever.co/lev/lv-1",
        first_observed=NOW - timedelta(days=30),
        last_seen_open=NOW - timedelta(days=1),
    )


def _page_date(conn: sqlite3.Connection, posting_id: str, fetched_at: datetime) -> None:
    conn.execute(
        """
        INSERT INTO posting_page_dates
            (posting_id, page_url, status, date_posted_raw, date_posted, fetched_at,
             attempts, last_attempt_at)
        VALUES (?, 'https://jobs.lever.co/lev/lv-1', 'ok', '2026-08-01',
                '2026-08-01T00:00:00.000000Z', ?, 1, ?)
        """,
        (posting_id, to_utc_z(fetched_at), to_utc_z(fetched_at)),
    )
    conn.commit()


def test_lever_page_date_is_available_only_from_its_fetch_time(conn: sqlite3.Connection) -> None:
    posting = _lever_posting(conn)
    fetched = NOW - timedelta(days=29)
    _page_date(conn, posting, fetched)

    assert capture_date_claims(conn, posting, fetched - timedelta(seconds=1)) == []
    (claim,) = capture_date_claims(conn, posting, fetched)
    assert claim.claim_type == "first_published"
    assert claim.source_quality == "page_structured"
    assert claim.source_event_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert claim.available_at == fetched
    assert claim.value == "2026-08-01"


def _as_evidence(claims, probe: str = ARCHIVE_BOARD_STATE_PROBE) -> list[EvidenceItem]:
    return [
        EvidenceItem(id=f"e{i}", run_id="r", probe=probe, **c.model_dump())
        for i, c in enumerate(claims, start=1)
    ]


def test_a_lever_page_date_alone_satisfies_q2(conn: sqlite3.Connection) -> None:
    posting = _lever_posting(conn)
    _page_date(conn, posting, NOW - timedelta(days=29))
    evidence = _as_evidence(capture_date_claims(conn, posting, NOW))
    verdict = evidence_quality_detail(evidence, PolicyInputs(posting_state="open"))
    assert verdict.rule != "no_primary_publish_evidence"
    assert verdict.quality == "strong"


# ---------------------------------------------------------------------------
# End to end: build, case state at T, replay, leakage
# ---------------------------------------------------------------------------

DATASET = "ds-dates"


@respx.mock
def _build_dated(conn: sqlite3.Connection, cfg: Config):
    _seed_dated(conn)
    mock_ats()
    summary = build_dataset(
        conn,
        cfg,
        dataset_id=DATASET,
        split="dev",
        split_kind="temporal",
        grid_step_days=15,
        now=NOW,
        cutoff=dev_everything_cutoff(),
        use_tool_cache=False,
        collection_status_csv="/nonexistent/collection_status.csv",
    )
    assert summary.postings_failed == 0, summary.failures
    return summary


def test_q2_turns_strong_at_an_archive_era_t_once_a_capture_carried_the_date(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    summary = _build_dated(conn, cfg)
    assert summary.cases_with_capture_publish_date >= 1

    # T = -15: the -20 capture carried the date; the live resolver's own date
    # (available only at the build instant) is NOT visible here.
    t = NOW - timedelta(days=15)
    case = case_state_at(conn, cfg, OPEN_POSTING, t, DATASET)
    published = [e for e in case.evidence if e.claim_type == "first_published"]
    assert [(e.probe, e.available_at) for e in published] == [
        (ARCHIVE_BOARD_STATE_PROBE, NOW - timedelta(days=20))
    ]
    verdict = evidence_quality_detail(case.evidence, case.inputs, case.failures)
    assert verdict.quality == "strong", verdict.detail
    assert case.inputs.publish_recency == "recent"  # 2026-08-10 is 13 days before T

    # T = -30: no capture at or before T carried a date -> still weak by Q2.
    early = case_state_at(conn, cfg, OPEN_POSTING, NOW - timedelta(days=30), DATASET)
    assert not [e for e in early.evidence if e.claim_type == "first_published"]
    early_verdict = evidence_quality_detail(early.evidence, early.inputs, early.failures)
    assert early_verdict.rule == "no_primary_publish_evidence"


def test_replaying_the_dated_dataset_is_leakage_clean(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_dated(conn, cfg)
    for system in ("A", "B"):
        result = run_replay(conn, cfg, dataset_id=DATASET, system=system)
        assert result.errors == 0, result.describe()

    rows = conn.execute(
        """
        SELECT r.replay_at, e.available_at FROM evidence AS e
        JOIN runs AS r ON r.id = e.run_id
        WHERE r.mode = 'replay' AND e.probe = ? AND e.claim_type = 'first_published'
        """,
        (ARCHIVE_BOARD_STATE_PROBE,),
    ).fetchall()
    assert rows, "the capture-derived publish claim never reached a replayed run"
    assert all(parse_utc(r["available_at"]) <= parse_utc(r["replay_at"]) for r in rows)

    report = check_dataset(conn, DATASET)
    assert report.clean, report.describe()


# ---------------------------------------------------------------------------
# Lever page dates in the daily collector
# ---------------------------------------------------------------------------


def _lever_job(job_id: str) -> dict:
    return {
        "id": job_id,
        "text": f"Role {job_id}",
        "categories": {"team": "Sales", "location": "SF"},
        "hostedUrl": f"https://jobs.lever.co/lev/{job_id}",
        "createdAt": 1755000000000,
        "descriptionPlain": "desc",
    }


def _jsonld_page(date_posted: str) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        '{"@context": "https://schema.org", "@type": "JobPosting", '
        f'"title": "Role", "datePosted": "{date_posted}"}}'
        "</script></head><body></body></html>"
    )


@pytest.fixture
def lever_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    upsert_companies(conn, [LEVER_TARGET], now=DAY1)
    return conn


def _page_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["posting_id"]: row for row in conn.execute("SELECT * FROM posting_page_dates")}


@respx.mock
def test_new_lever_posting_gets_its_page_date_once(lever_conn, cfg) -> None:
    respx.get(LEVER_BOARD_URL).mock(return_value=httpx.Response(200, json=[_lever_job("a")]))
    page = respx.get("https://jobs.lever.co/lev/a").mock(
        return_value=httpx.Response(200, text=_jsonld_page("2026-08-25"))
    )
    fast = _fast(cfg)

    summary = run_daily_snapshot(lever_conn, fast, [LEVER_TARGET], DAY1, sleep=_no_sleep)
    assert (summary.page_dates.attempted, summary.page_dates.dated) == (1, 1)
    assert "lever_page_dates: fetched=1 dated=1" in summary.describe()

    row = _page_rows(lever_conn)["lever:lev:a"]
    assert row["status"] == "ok"
    assert row["date_posted"] == "2026-08-25T00:00:00.000000Z"
    assert row["date_posted_raw"] == "2026-08-25"
    assert row["page_url"] == "https://jobs.lever.co/lev/a"
    # Availability is the real fetch time, never the posting's stated date.
    assert parse_utc(row["fetched_at"]) > parse_utc(row["date_posted"])

    # Same-day rerun: every company skipped, zero page fetches.
    rerun = run_daily_snapshot(lever_conn, fast, [LEVER_TARGET], DAY1, sleep=_no_sleep)
    assert rerun.companies_skipped == 1 and rerun.page_dates.attempted == 0

    # Next day: obtained already, so never refetched.
    run_daily_snapshot(lever_conn, fast, [LEVER_TARGET], DAY2, sleep=_no_sleep)
    assert page.call_count == 1
    assert _page_rows(lever_conn)["lever:lev:a"]["attempts"] == 1


@respx.mock
def test_a_failed_page_fetch_is_a_gap_and_is_retried_next_run(lever_conn, cfg) -> None:
    respx.get(LEVER_BOARD_URL).mock(return_value=httpx.Response(200, json=[_lever_job("a")]))
    page = respx.get("https://jobs.lever.co/lev/a").mock(
        side_effect=[
            httpx.Response(404, text="gone?"),
            httpx.Response(200, text=_jsonld_page("2026-08-25")),
        ]
    )
    fast = _fast(cfg)

    day1 = run_daily_snapshot(lever_conn, fast, [LEVER_TARGET], DAY1, sleep=_no_sleep)
    # The snapshot itself succeeded and the posting is open, not absent.
    assert (day1.companies_ok, day1.companies_failed, day1.postings_absent) == (1, 0, 0)
    assert day1.page_dates.failed == 1
    posting = lever_conn.execute(
        "SELECT first_seen_absent, last_seen_open FROM postings WHERE posting_id = 'lever:lev:a'"
    ).fetchone()
    assert posting["first_seen_absent"] is None
    assert posting["last_seen_open"] == to_utc_z(DAY1)
    assert lever_conn.execute("SELECT COUNT(*) FROM capture_attempts").fetchone()[0] == 0
    row = _page_rows(lever_conn)["lever:lev:a"]
    assert (row["status"], row["attempts"], row["fetched_at"]) == ("failed", 1, None)
    assert capture_date_claims(lever_conn, "lever:lev:a", DAY2) == []

    day2 = run_daily_snapshot(lever_conn, fast, [LEVER_TARGET], DAY2, sleep=_no_sleep)
    assert day2.page_dates.dated == 1
    row = _page_rows(lever_conn)["lever:lev:a"]
    assert (row["status"], row["attempts"], row["last_error"]) == ("ok", 2, None)
    assert page.call_count == 2


@respx.mock
def test_attempts_stop_at_the_configured_cap(lever_conn, cfg) -> None:
    respx.get(LEVER_BOARD_URL).mock(return_value=httpx.Response(200, json=[_lever_job("a")]))
    page = respx.get("https://jobs.lever.co/lev/a").mock(return_value=httpx.Response(404))
    fast = _fast(cfg, max_attempts_per_posting=2)

    for day in range(4):
        run_daily_snapshot(
            lever_conn, fast, [LEVER_TARGET], DAY1 + timedelta(days=day), sleep=_no_sleep
        )
    assert page.call_count == 2
    assert _page_rows(lever_conn)["lever:lev:a"]["attempts"] == 2


@respx.mock
def test_page_with_no_date_is_terminal_not_retried(lever_conn, cfg) -> None:
    respx.get(LEVER_BOARD_URL).mock(return_value=httpx.Response(200, json=[_lever_job("a")]))
    page = respx.get("https://jobs.lever.co/lev/a").mock(
        return_value=httpx.Response(200, text="<html>no structured data</html>")
    )
    fast = _fast(cfg)
    run_daily_snapshot(lever_conn, fast, [LEVER_TARGET], DAY1, sleep=_no_sleep)
    run_daily_snapshot(lever_conn, fast, [LEVER_TARGET], DAY2, sleep=_no_sleep)
    assert page.call_count == 1
    assert _page_rows(lever_conn)["lever:lev:a"]["status"] == "no_date"


@respx.mock
def test_per_run_cap_serves_the_newest_postings_first(lever_conn, cfg) -> None:
    board = respx.get(LEVER_BOARD_URL)
    board.side_effect = [
        httpx.Response(200, json=[_lever_job("old")]),
        httpx.Response(200, json=[_lever_job("old"), _lever_job("new")]),
        httpx.Response(200, json=[_lever_job("old"), _lever_job("new")]),
    ]
    old_page = respx.get("https://jobs.lever.co/lev/old").mock(
        return_value=httpx.Response(200, text=_jsonld_page("2026-07-01"))
    )
    new_page = respx.get("https://jobs.lever.co/lev/new").mock(
        return_value=httpx.Response(200, text=_jsonld_page("2026-09-01"))
    )

    # Day 1 with the pass disabled: "old" predates the feature.
    run_daily_snapshot(lever_conn, _fast(cfg, enabled=False), [LEVER_TARGET], DAY1, sleep=_no_sleep)
    assert old_page.call_count == 0

    capped = _fast(cfg, max_fetches_per_run=1)
    day2 = run_daily_snapshot(lever_conn, capped, [LEVER_TARGET], DAY2, sleep=_no_sleep)
    assert day2.page_dates.attempted == 1
    assert (new_page.call_count, old_page.call_count) == (1, 0)

    # The remaining cap on a later run catches up the older still-open one.
    run_daily_snapshot(
        lever_conn, capped, [LEVER_TARGET], DAY2 + timedelta(days=1), sleep=_no_sleep
    )
    assert (new_page.call_count, old_page.call_count) == (1, 1)
    assert {r["status"] for r in _page_rows(lever_conn).values()} == {"ok"}


@respx.mock
def test_consecutive_failures_stop_the_pass(lever_conn, cfg) -> None:
    jobs = [_lever_job(j) for j in ("a", "b", "c", "d")]
    respx.get(LEVER_BOARD_URL).mock(return_value=httpx.Response(200, json=jobs))
    pages = [
        respx.get(f"https://jobs.lever.co/lev/{j}").mock(return_value=httpx.Response(404))
        for j in ("a", "b", "c", "d")
    ]
    summary = run_daily_snapshot(
        lever_conn, _fast(cfg, max_consecutive_failures=2), [LEVER_TARGET], DAY1, sleep=_no_sleep
    )
    assert summary.companies_ok == 1
    assert summary.page_dates.failed == 2 and summary.page_dates.stopped_early
    assert sum(p.call_count for p in pages) == 2


@respx.mock
def test_an_unexpected_page_error_never_fails_the_snapshot(lever_conn, cfg) -> None:
    respx.get(LEVER_BOARD_URL).mock(return_value=httpx.Response(200, json=[_lever_job("a")]))
    respx.get("https://jobs.lever.co/lev/a").mock(side_effect=RuntimeError("parser blew up"))
    summary = run_daily_snapshot(lever_conn, _fast(cfg), [LEVER_TARGET], DAY1, sleep=_no_sleep)
    assert (summary.companies_ok, summary.postings_new) == (1, 1)
    row = _page_rows(lever_conn)["lever:lev:a"]
    assert row["status"] == "failed"
