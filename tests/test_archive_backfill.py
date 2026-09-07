"""`rli.archive.backfill.run_backfill` — end-to-end backfill against mocked Wayback."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import respx

from rli.archive.backfill import (
    CompanySummary,
    candidate_urls,
    filter_targets,
    parse_wayback_timestamp,
    run_backfill,
)
from rli.archive.cdx import CDX_URL
from rli.archive.fetch import capture_url

CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


def _cdx_page(rows: list[list[str]]) -> str:
    return json.dumps([CDX_HEADER, *rows])


def _fixed_now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _gh_target() -> dict[str, str]:
    return {
        "company_name": "Acme Corp",
        "website_domain": "acme.com",
        "ats": "greenhouse",
        "tenant": "acme",
        "open_job_count": "1",
        "checked_at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# candidate_urls / filter_targets — pure helpers
# ---------------------------------------------------------------------------


def test_candidate_urls_greenhouse_order() -> None:
    candidates = candidate_urls("greenhouse", "acme")
    assert candidates[0] == ("api", "https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    assert candidates[1] == ("html", "https://boards.greenhouse.io/acme")
    assert candidates[2] == ("html", "https://job-boards.greenhouse.io/acme")


def test_filter_targets_matches_tenant_company_or_domain() -> None:
    targets = [
        {"tenant": "acme", "company_name": "Acme Corp", "website_domain": "acme.com"},
        {"tenant": "beta", "company_name": "Beta Inc", "website_domain": "beta.io"},
    ]
    assert filter_targets(targets, "ACME") == [targets[0]]
    assert filter_targets(targets, "beta.io") == [targets[1]]
    assert filter_targets(targets, None) == targets


def test_parse_wayback_timestamp() -> None:
    dt = parse_wayback_timestamp("20250615123045")
    assert dt == datetime(2025, 6, 15, 12, 30, 45, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Archived Greenhouse JSON capture -> board_snapshot_jobs
# ---------------------------------------------------------------------------


@respx.mock
def test_backfill_greenhouse_api_capture_produces_snapshot_and_jobs(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"

    respx.get(CDX_URL).mock(
        return_value=httpx.Response(
            200,
            text=_cdx_page(
                [["k", "20250601000000", api_url, "application/json", "200", "AAA", "10"]]
            ),
        )
    )
    gh_body = json.dumps(
        {
            "jobs": [
                {
                    "id": 42,
                    "title": "Backend Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                    "departments": [{"name": "Engineering"}],
                    "offices": [{"name": "Remote"}],
                    "content": "job description",
                }
            ]
        }
    )
    respx.get(capture_url("20250601000000", api_url)).mock(
        return_value=httpx.Response(200, text=gh_body)
    )

    summaries = run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    assert len(summaries) == 1
    summary = summaries[0]
    assert isinstance(summary, CompanySummary)
    assert summary.pattern_used == "api"
    assert summary.captures_found == 1
    assert summary.captures_parsed == 1
    assert summary.captures_failed == 0

    snapshot_rows = conn.execute(
        "SELECT id, company_id, source, coverage_status FROM board_snapshots"
    ).fetchall()
    assert len(snapshot_rows) == 1
    assert snapshot_rows[0]["company_id"] == "acme.com"
    assert snapshot_rows[0]["source"] == "archive"
    assert snapshot_rows[0]["coverage_status"] == "complete"

    job_rows = conn.execute(
        "SELECT job_id, title, team, location, url FROM board_snapshot_jobs"
    ).fetchall()
    assert len(job_rows) == 1
    assert job_rows[0]["job_id"] == "42"
    assert job_rows[0]["title"] == "Backend Engineer"
    assert job_rows[0]["team"] == "Engineering"
    assert job_rows[0]["location"] == "Remote"
    assert job_rows[0]["url"] == "https://boards.greenhouse.io/acme/jobs/42"

    company_rows = conn.execute("SELECT company_id, name FROM companies").fetchall()
    assert len(company_rows) == 1
    assert company_rows[0]["company_id"] == "acme.com"
    assert company_rows[0]["name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# Archived HTML board page -> tolerant title+url+job_id parse
# ---------------------------------------------------------------------------


@respx.mock
def test_backfill_falls_back_to_html_page_when_api_has_no_captures(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    page_url = "https://boards.greenhouse.io/acme"

    def cdx_responder(request: httpx.Request) -> httpx.Response:
        pattern = dict(request.url.params)["url"]
        if pattern.startswith(api_url):
            return httpx.Response(200, text=_cdx_page([]))
        if pattern == page_url:
            return httpx.Response(
                200,
                text=_cdx_page(
                    [["k", "20250601000000", page_url, "text/html", "200", "BBB", "10"]]
                ),
            )
        return httpx.Response(200, text=_cdx_page([]))

    respx.get(CDX_URL).mock(side_effect=cdx_responder)

    html = """
    <html><body>
      <a href="https://boards.greenhouse.io/acme/jobs/101">Staff Engineer</a>
      <a href="https://boards.greenhouse.io/acme/jobs/102">Product Manager</a>
      <a href="/about">About us</a>
    </body></html>
    """
    respx.get(capture_url("20250601000000", page_url)).mock(
        return_value=httpx.Response(200, text=html)
    )

    summaries = run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    summary = summaries[0]
    assert summary.pattern_used == "html"
    assert summary.captures_parsed == 1

    job_rows = conn.execute(
        "SELECT job_id, title, url FROM board_snapshot_jobs ORDER BY job_id"
    ).fetchall()
    assert [dict(r) for r in job_rows] == [
        {
            "job_id": "101",
            "title": "Staff Engineer",
            "url": "https://boards.greenhouse.io/acme/jobs/101",
        },
        {
            "job_id": "102",
            "title": "Product Manager",
            "url": "https://boards.greenhouse.io/acme/jobs/102",
        },
    ]

    snapshot = conn.execute("SELECT coverage_status FROM board_snapshots").fetchone()
    assert snapshot["coverage_status"] == "complete"


@respx.mock
def test_backfill_html_page_with_zero_job_links_is_partial_coverage(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    page_url = "https://boards.greenhouse.io/acme"

    def cdx_responder(request: httpx.Request) -> httpx.Response:
        pattern = dict(request.url.params)["url"]
        if pattern.startswith(api_url):
            return httpx.Response(200, text=_cdx_page([]))
        if pattern == page_url:
            return httpx.Response(
                200,
                text=_cdx_page(
                    [["k", "20250601000000", page_url, "text/html", "200", "BBB", "10"]]
                ),
            )
        return httpx.Response(200, text=_cdx_page([]))

    respx.get(CDX_URL).mock(side_effect=cdx_responder)
    # JS-rendered SPA shell: no job links present in the raw HTML.
    respx.get(capture_url("20250601000000", page_url)).mock(
        return_value=httpx.Response(200, text="<html><body><div id='app'></div></body></html>")
    )

    summaries = run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    assert summaries[0].captures_parsed == 1
    snapshot = conn.execute("SELECT coverage_status FROM board_snapshots").fetchone()
    assert snapshot["coverage_status"] == "partial"
    job_rows = conn.execute("SELECT * FROM board_snapshot_jobs").fetchall()
    assert job_rows == []


# ---------------------------------------------------------------------------
# Retry exhaustion -> structured failure, capture_attempts row, no snapshot
# ---------------------------------------------------------------------------


@respx.mock
def test_backfill_cdx_failure_records_capture_attempt_and_no_snapshot(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    respx.get(CDX_URL).mock(return_value=httpx.Response(503, text="down"))

    summaries = run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    summary = summaries[0]
    assert summary.pattern_used is None
    assert summary.captures_found == 0
    assert summary.captures_parsed == 0
    assert summary.captures_failed == 0

    assert conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()["n"] == 0

    attempts = conn.execute(
        "SELECT target, ok, retryable FROM capture_attempts WHERE source = 'archive'"
    ).fetchall()
    # One failed CDX-list attempt per candidate pattern (api + 2 html pages).
    assert len(attempts) == 3
    assert all(row["ok"] == 0 for row in attempts)
    assert all(row["retryable"] == 1 for row in attempts)


@respx.mock
def test_backfill_capture_fetch_failure_writes_only_capture_attempt(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"

    respx.get(CDX_URL).mock(
        return_value=httpx.Response(
            200,
            text=_cdx_page(
                [["k", "20250601000000", api_url, "application/json", "200", "AAA", "10"]]
            ),
        )
    )
    respx.get(capture_url("20250601000000", api_url)).mock(
        return_value=httpx.Response(503, text="down")
    )

    summaries = run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    summary = summaries[0]
    assert summary.captures_found == 1
    assert summary.captures_parsed == 0
    assert summary.captures_failed == 1

    assert conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()["n"] == 0

    fetch_attempt = conn.execute(
        "SELECT ok, retryable, error FROM capture_attempts WHERE target = ?",
        (capture_url("20250601000000", api_url),),
    ).fetchone()
    assert fetch_attempt["ok"] == 0
    assert fetch_attempt["retryable"] == 1
    assert fetch_attempt["error"] is not None


@respx.mock
def test_backfill_unparseable_body_writes_only_capture_attempt_not_a_snapshot(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"

    respx.get(CDX_URL).mock(
        return_value=httpx.Response(
            200,
            text=_cdx_page(
                [["k", "20250601000000", api_url, "application/json", "200", "AAA", "10"]]
            ),
        )
    )
    respx.get(capture_url("20250601000000", api_url)).mock(
        return_value=httpx.Response(200, text="not json at all")
    )

    summaries = run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    summary = summaries[0]
    assert summary.captures_parsed == 0
    assert summary.captures_failed == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()["n"] == 0

    fetch_attempt = conn.execute(
        "SELECT ok FROM capture_attempts WHERE target = ?",
        (capture_url("20250601000000", api_url),),
    ).fetchone()
    assert fetch_attempt["ok"] == 0


# ---------------------------------------------------------------------------
# --limit-captures caps processed (not found) captures, most-recent-first
# ---------------------------------------------------------------------------


@respx.mock
def test_backfill_limit_captures_caps_processing_most_recent_first(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    timestamps = ["20250101000000", "20250201000000", "20250301000000"]

    respx.get(CDX_URL).mock(
        return_value=httpx.Response(
            200,
            text=_cdx_page(
                [
                    ["k", ts, api_url, "application/json", "200", f"D{i}", "10"]
                    for i, ts in enumerate(timestamps)
                ]
            ),
        )
    )
    for ts in timestamps:
        respx.get(capture_url(ts, api_url)).mock(
            return_value=httpx.Response(200, text=json.dumps({"jobs": []}))
        )

    summaries = run_backfill(
        cfg, conn, [_gh_target()], months=6, limit_captures=1, net=net, now=_fixed_now
    )

    summary = summaries[0]
    assert summary.captures_found == 3
    assert summary.captures_parsed == 1

    snapshot = conn.execute("SELECT captured_at FROM board_snapshots").fetchone()
    assert snapshot["captured_at"].startswith("2025-03-01")


# ---------------------------------------------------------------------------
# postings / posting_snapshots are never touched
# ---------------------------------------------------------------------------


@respx.mock
def test_backfill_never_writes_postings_or_posting_snapshots(
    cfg, conn, net_client_factory
) -> None:
    net = net_client_factory("archive_backfill")
    api_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"

    respx.get(CDX_URL).mock(
        return_value=httpx.Response(
            200,
            text=_cdx_page(
                [["k", "20250601000000", api_url, "application/json", "200", "AAA", "10"]]
            ),
        )
    )
    respx.get(capture_url("20250601000000", api_url)).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Engineer",
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                            "departments": [],
                            "content": "desc",
                        }
                    ]
                }
            ),
        )
    )

    run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    assert conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM posting_snapshots").fetchone()["n"] == 0
    # Sanity: the run did actually produce data elsewhere.
    assert conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()["n"] == 1
