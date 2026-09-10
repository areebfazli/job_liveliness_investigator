"""`rli.archive.backfill.run_backfill` — end-to-end backfill against mocked Wayback."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import respx

from rli.archive.backfill import (
    CompanySummary,
    _parse_html_board,
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
def test_backfill_never_writes_postings_or_posting_snapshots(cfg, conn, net_client_factory) -> None:
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


# ---------------------------------------------------------------------------
# _parse_html_board — per-ATS extraction, VERIFIED markup shapes
#
# Every fixture below is the smallest representative excerpt of a real
# Wayback capture (jobs.lever.co/gohighlevel 20260713,
# job-boards.greenhouse.io/twilio 20260824, jobs.ashbyhq.com/linear
# 20260810), trimmed to the structure the extractor depends on.
# ---------------------------------------------------------------------------

# Note the document order inside `div.posting`: the `.posting-apply` anchor
# with text "Apply" comes BEFORE `a.posting-title`, and both carry the SAME
# job UUID. Keeping the first anchor seen is what produced 8k "Apply" rows.
LEVER_HTML = """
<html><body><div class="content">
  <div class="postings-group">
    <div class="large-category-header">Customer Success</div>
    <div class="posting-category-title large-category-label">Implementation Services</div>
    <div class="posting" data-qa-posting-id="ff2ad979-17a1-42d7-b206-96b9025b753d">
      <div class="posting-apply" data-qa="btn-apply"><a
        href="https://jobs.lever.co/gohighlevel/ff2ad979-17a1-42d7-b206-96b9025b753d"
        class="posting-btn-submit template-btn-submit">Apply</a></div>
      <a class="posting-title"
         href="https://jobs.lever.co/gohighlevel/ff2ad979-17a1-42d7-b206-96b9025b753d">
        <h5 data-qa="posting-name">Implementation Advisor</h5>
        <div class="posting-categories">
          <span class="small-category-label workplaceTypes">Remote &mdash; </span>
          <span class="sort-by-commitment posting-category commitment">EOR Mexico</span>
          <span class="sort-by-location posting-category location">Mexico</span>
        </div>
      </a>
    </div>
  </div>
  <div class="postings-group">
    <div class="large-category-header">Engineering</div>
    <div class="posting-category-title large-category-label">AI &amp; Marketplace</div>
    <div class="posting" data-qa-posting-id="ce928065-f2cf-4412-a5e7-32677e6f4e4b">
      <div class="posting-apply"><a
        href="https://jobs.lever.co/gohighlevel/ce928065-f2cf-4412-a5e7-32677e6f4e4b">Apply</a></div>
      <a class="posting-title"
         href="https://jobs.lever.co/gohighlevel/ce928065-f2cf-4412-a5e7-32677e6f4e4b">
        <h5 data-qa="posting-name">Full Stack Builder (Team of One)</h5>
        <div class="posting-categories">
          <span class="sort-by-location posting-category location">India</span>
        </div>
      </a>
    </div>
  </div>
  <div class="postings-group">
    <div class="posting" data-qa-posting-id="dd173c17-3759-437c-ab96-f37e135ee804">
      <div class="posting-apply"><a
        href="https://jobs.lever.co/gohighlevel/dd173c17-3759-437c-ab96-f37e135ee804">Apply</a></div>
      <a class="posting-title"
         href="https://jobs.lever.co/gohighlevel/dd173c17-3759-437c-ab96-f37e135ee804">
        <h5 data-qa="posting-name">Engineering Manager - Conversations</h5>
      </a>
    </div>
  </div>
</div></body></html>
"""

# The single job anchor wraps two sibling <p>s with NO separator, so
# `a.get_text()` yields "IT Internal AuditorRemote - India". The second row
# additionally carries a `.tag-container` "New" badge inside the title <p>.
GREENHOUSE_HTML = """
<html><body><div class="job-posts">
  <div class="job-posts--table--department">
    <div class="job-posts--department-path"><p class="body body--metadata">G&amp;A</p></div>
    <h3 class="section-header font-primary">Accounting</h3>
    <div class="job-posts--table"><table><tbody>
      <tr class="job-post"><td class="cell">
        <a href="https://job-boards.greenhouse.io/twilio/jobs/7982861" target="_top">
          <p class="body body--medium">IT Internal Auditor</p>
          <p class="body body__secondary body--metadata">Remote - India</p>
        </a>
      </td></tr>
      <tr class="job-post"><td class="cell">
        <a href="https://job-boards.greenhouse.io/twilio/jobs/8141746" target="_top">
          <p class="body body--medium">Senior Tax and Compliance Analyst (Payroll)<span
            class="tag-container"><span class="ellipse"><span
            class="tag-text">New</span></span></span></p>
          <p class="body body__secondary body--metadata">Remote - US</p>
        </a>
      </td></tr>
    </tbody></table></div>
  </div>
</div></body></html>
"""

# Same template, but the capture holds page 1 of 3: the rendered rows are a
# strict subset of the `"total"` the page itself declares.
GREENHOUSE_PAGINATED_HTML = """
<html><body>
  <h2 class="section-header" data-testid="job-count-header">146 jobs</h2>
  <div class="job-posts"><div class="job-posts--table--department">
    <h3 class="section-header font-primary">Accounting</h3>
    <tr class="job-post"><td class="cell">
      <a href="https://job-boards.greenhouse.io/twilio/jobs/7982861">
        <p class="body body--medium">IT Internal Auditor</p>
        <p class="body body__secondary body--metadata">Remote - India</p>
      </a>
    </td></tr>
  </div></div>
  <script>window.__remixContext = {"jobPosts":{"count":50,"page":1,"total":146,
  "total_pages":3,"data":[{"id":7982861,"title":"IT Internal Auditor"}]}};</script>
</body></html>
"""

# Ashby serves a pure SPA shell: ZERO <a> tags, the whole board inlined as a
# `window.__appData` assignment.
ASHBY_HTML = """
<html><body><div id="root"><div class="spinner"></div></div>
<script nonce="ChWWoR3">
  window.__appData = {"environment":"production","organization":{"name":"Linear"},
  "jobBoard":{"teams":[{"id":"7ab28977","name":"GTM","externalName":null,"parentTeamId":null},
  {"id":"5051511e","name":"Sales","externalName":null,"parentTeamId":"7ab28977"}],
  "jobPostings":[{"id":"1bfdcabe-aa5f-4999-9a6d-b8a824dd779b",
  "title":"Account Executive, Enterprise","teamId":"5051511e",
  "locationName":"North America","workplaceType":"Remote","secondaryLocations":[]},
  {"id":"453f1ba0-a35e-4ed2-8215-1514e0a30b92","title":"Account Executive, Growth",
  "teamId":"5051511e","locationName":"North America","secondaryLocations":[]}]},
  "routerPrefix":""};
  fetch("https://cdn.ashbyprd.com/manifest.json");
</script></body></html>
"""

ASHBY_HTML_NO_APP_DATA = """
<html><body><div id="root"><div class="spinner"></div></div></body></html>
"""


def _titles(jobs: list) -> list[str | None]:
    return [job.title for job in jobs]


def test_parse_html_board_lever_prefers_posting_title_over_apply_anchor(cfg) -> None:
    """The Apply anchor shares the job UUID and comes first; the h5 must win."""
    jobs, reason = _parse_html_board(cfg, "lever", "gohighlevel", LEVER_HTML)

    assert reason is None
    assert _titles(jobs) == [
        "Implementation Advisor",
        "Full Stack Builder (Team of One)",
        "Engineering Manager - Conversations",
    ]
    # One entry per posting, not one per anchor: the Apply anchor merged in.
    assert [job.job_id for job in jobs] == [
        "ff2ad979-17a1-42d7-b206-96b9025b753d",
        "ce928065-f2cf-4412-a5e7-32677e6f4e4b",
        "dd173c17-3759-437c-ab96-f37e135ee804",
    ]


def test_parse_html_board_lever_recovers_team_location_and_url(cfg) -> None:
    jobs, _ = _parse_html_board(cfg, "lever", "gohighlevel", LEVER_HTML)

    first = jobs[0]
    # `.posting-category-title` is Lever's TEAM (the group header is the
    # department) — the same field the API path stores as `categories.team`.
    assert first.team == "Implementation Services"
    # Only `.sort-by-location`, never the workplaceTypes/commitment chips.
    assert first.location == "Mexico"
    assert first.url == ("https://jobs.lever.co/gohighlevel/ff2ad979-17a1-42d7-b206-96b9025b753d")
    # A listing page carries no description.
    assert first.description_hash is None

    assert jobs[1].team == "AI & Marketplace"
    assert jobs[1].location == "India"
    # Third group has no labels of its own: the department carries forward.
    assert jobs[2].team == "Engineering"
    assert jobs[2].location is None


def test_parse_html_board_greenhouse_does_not_concatenate_title_and_location(cfg) -> None:
    """`a.get_text()` would yield "IT Internal AuditorRemote - India"."""
    jobs, reason = _parse_html_board(cfg, "greenhouse", "twilio", GREENHOUSE_HTML)

    assert reason is None
    assert _titles(jobs) == [
        "IT Internal Auditor",
        "Senior Tax and Compliance Analyst (Payroll)",
    ]
    assert [job.location for job in jobs] == ["Remote - India", "Remote - US"]
    assert {job.team for job in jobs} == {"Accounting"}
    assert jobs[0].url == "https://job-boards.greenhouse.io/twilio/jobs/7982861"


def test_parse_html_board_greenhouse_strips_new_badge_from_title(cfg) -> None:
    jobs, _ = _parse_html_board(cfg, "greenhouse", "twilio", GREENHOUSE_HTML)

    assert "New" not in (jobs[1].title or "")


def test_parse_html_board_greenhouse_paginated_capture_is_partial(cfg) -> None:
    """Page 1 of 3 must never be stored as a complete board (spec.md §4).

    Otherwise `rli.history.closures` would read the 145 jobs that live on
    pages 2-3 as having closed on the capture date.
    """
    jobs, reason = _parse_html_board(cfg, "greenhouse", "twilio", GREENHOUSE_PAGINATED_HTML)

    # The job that WAS rendered is real and is kept — only coverage is partial.
    assert _titles(jobs) == ["IT Internal Auditor"]
    assert reason is not None
    assert "146" in reason and "paginated" in reason


def test_parse_html_board_ashby_reads_window_app_data(cfg) -> None:
    jobs, reason = _parse_html_board(cfg, "ashby", "linear", ASHBY_HTML)

    assert reason is None
    assert _titles(jobs) == ["Account Executive, Enterprise", "Account Executive, Growth"]
    assert [job.team for job in jobs] == ["Sales", "Sales"]
    assert [job.location for job in jobs] == ["North America", "North America"]
    assert jobs[0].url == ("https://jobs.ashbyhq.com/linear/1bfdcabe-aa5f-4999-9a6d-b8a824dd779b")


def test_parse_html_board_ashby_without_app_data_is_zero_jobs_and_partial(cfg) -> None:
    """No blob and no <a> tags: the honest answer is zero jobs + a gap."""
    jobs, reason = _parse_html_board(cfg, "ashby", "linear", ASHBY_HTML_NO_APP_DATA)

    assert jobs == []
    assert reason is not None
    assert "no job entries" in reason


def test_parse_html_board_ashby_with_unparseable_app_data_is_zero_jobs(cfg) -> None:
    broken = '<html><body><script>window.__appData = {"jobBoard": {"jobPos</script></body></html>'
    jobs, reason = _parse_html_board(cfg, "ashby", "linear", broken)

    assert jobs == []
    assert reason is not None


def test_parse_html_board_drops_junk_titles_and_never_persists_them(cfg) -> None:
    """Junk link text is dropped, not stored — and not stored as NULL either."""
    html = """
    <html><body>
      <a href="https://boards.greenhouse.io/acme/jobs/101">Apply</a>
      <a href="https://boards.greenhouse.io/acme/jobs/102">Apply now</a>
      <a href="https://boards.greenhouse.io/acme/jobs/103">View</a>
      <a href="https://boards.greenhouse.io/acme/jobs/104"></a>
    </body></html>
    """
    jobs, reason = _parse_html_board(cfg, "greenhouse", "acme", html)

    assert jobs == []
    assert reason is not None
    assert "4 of 4" in reason


def test_parse_html_board_keeps_real_titles_alongside_junk_ones(cfg) -> None:
    """A partial parse keeps what survived and still reports the loss."""
    html = """
    <html><body>
      <a href="https://boards.greenhouse.io/acme/jobs/101">Staff Engineer</a>
      <a href="https://boards.greenhouse.io/acme/jobs/102">Product Manager</a>
      <a href="https://boards.greenhouse.io/acme/jobs/103">Apply</a>
    </body></html>
    """
    jobs, reason = _parse_html_board(cfg, "greenhouse", "acme", html)

    assert _titles(jobs) == ["Staff Engineer", "Product Manager"]
    assert reason is not None
    assert "1 of 3" in reason


def test_parse_html_board_shared_title_page_guard_drops_every_job(cfg) -> None:
    """Nine syntactically fine but identical titles are an extraction failure."""
    html = (
        "<html><body>"
        + "".join(
            f'<a href="https://boards.greenhouse.io/acme/jobs/{i}">Software Engineer</a>'
            for i in range(9)
        )
        + "</body></html>"
    )

    jobs, reason = _parse_html_board(cfg, "greenhouse", "acme", html)

    assert jobs == []
    assert reason is not None
    assert "100% of 9 jobs" in reason


def test_parse_html_board_page_guard_respects_config_knobs(cfg) -> None:
    """The ceiling and the floor come from `[matching]`, not from constants."""
    html = (
        "<html><body>"
        + "".join(
            f'<a href="https://boards.greenhouse.io/acme/jobs/{i}">Software Engineer</a>'
            for i in range(9)
        )
        + "</body></html>"
    )
    loosened = cfg.model_copy(
        update={"matching": cfg.matching.model_copy(update={"page_shared_title_max_fraction": 1.0})}
    )

    jobs, reason = _parse_html_board(loosened, "greenhouse", "acme", html)

    assert len(jobs) == 9
    assert reason is None


def test_parse_html_board_uses_heading_and_aria_label_fallbacks(cfg) -> None:
    """An unknown template degrades through the generic candidate ladder."""
    html = """
    <html><body>
      <li class="card">
        <h4>Data Platform Engineer</h4>
        <a href="https://boards.greenhouse.io/acme/jobs/201">Apply now</a>
      </li>
      <li class="card">
        <a href="https://boards.greenhouse.io/acme/jobs/202" aria-label="Security Analyst">
          Apply
        </a>
        <span class="location">Berlin</span>
      </li>
    </body></html>
    """
    jobs, reason = _parse_html_board(cfg, "greenhouse", "acme", html)

    assert reason is None
    assert _titles(jobs) == ["Data Platform Engineer", "Security Analyst"]
    assert jobs[1].location == "Berlin"


# ---------------------------------------------------------------------------
# End-to-end: a junk-title HTML capture is a documented coverage gap
# ---------------------------------------------------------------------------


@respx.mock
def test_backfill_html_capture_with_junk_titles_is_partial_with_reason(
    cfg, conn, net_client_factory
) -> None:
    """`board_snapshots` has no reason column, so the reason rides on the
    successful `capture_attempts` row (ok=1) for that capture's URL."""
    net = net_client_factory("archive_backfill")
    page_url = "https://boards.greenhouse.io/acme"

    def cdx_responder(request: httpx.Request) -> httpx.Response:
        pattern = dict(request.url.params)["url"]
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
      <a href="https://boards.greenhouse.io/acme/jobs/101">Apply</a>
      <a href="https://boards.greenhouse.io/acme/jobs/102">Apply</a>
    </body></html>
    """
    respx.get(capture_url("20250601000000", page_url)).mock(
        return_value=httpx.Response(200, text=html)
    )

    summaries = run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    # The capture was fetched and parsed — it is a snapshot, just a partial one.
    assert summaries[0].pattern_used == "html"
    assert summaries[0].captures_parsed == 1
    assert summaries[0].captures_failed == 0

    snapshot = conn.execute("SELECT coverage_status FROM board_snapshots").fetchone()
    assert snapshot["coverage_status"] == "partial"
    assert conn.execute("SELECT COUNT(*) AS n FROM board_snapshot_jobs").fetchone()["n"] == 0

    attempt = conn.execute(
        "SELECT ok, error, retryable FROM capture_attempts WHERE target = ?",
        (capture_url("20250601000000", page_url),),
    ).fetchone()
    assert attempt["ok"] == 1
    assert attempt["retryable"] is None
    assert attempt["error"]
    assert attempt["error"].startswith("partial coverage:")
    assert "no plausible title" in attempt["error"]


@respx.mock
def test_backfill_html_capture_with_real_titles_is_complete_with_no_reason(
    cfg, conn, net_client_factory
) -> None:
    """The success path must not start writing a reason for clean captures."""
    net = net_client_factory("archive_backfill")
    page_url = "https://boards.greenhouse.io/acme"

    def cdx_responder(request: httpx.Request) -> httpx.Response:
        pattern = dict(request.url.params)["url"]
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
    </body></html>
    """
    respx.get(capture_url("20250601000000", page_url)).mock(
        return_value=httpx.Response(200, text=html)
    )

    run_backfill(cfg, conn, [_gh_target()], months=6, net=net, now=_fixed_now)

    snapshot = conn.execute("SELECT coverage_status FROM board_snapshots").fetchone()
    assert snapshot["coverage_status"] == "complete"
    attempt = conn.execute(
        "SELECT ok, error FROM capture_attempts WHERE target = ?",
        (capture_url("20250601000000", page_url),),
    ).fetchone()
    assert attempt["ok"] == 1
    assert attempt["error"] is None


def test_parse_html_board_lever_card_without_a_job_id_is_reported_not_hidden(cfg) -> None:
    """A `.posting` card the extractor cannot identify is a coverage gap.

    Silently skipping it would make the capture look like a complete board
    that is one job shorter than it really was.
    """
    html = LEVER_HTML.replace(
        '<div class="posting" data-qa-posting-id="dd173c17-3759-437c-ab96-f37e135ee804">',
        '<div class="posting">',
    ).replace("https://jobs.lever.co/gohighlevel/dd173c17-3759-437c-ab96-f37e135ee804", "/apply")

    jobs, reason = _parse_html_board(cfg, "lever", "gohighlevel", html)

    assert len(jobs) == 2
    assert reason is not None
    assert "3 jobs but only 2" in reason


def test_parse_html_board_greenhouse_ignores_another_tenants_job_link(cfg) -> None:
    """A cross-tenant link must not inject a phantom job into this board."""
    html = """
    <html><body>
      <a href="https://boards.greenhouse.io/other-co/jobs/9">Phantom Role</a>
      <a href="https://boards.greenhouse.io/acme/jobs/1">Staff Engineer</a>
    </body></html>
    """
    jobs, reason = _parse_html_board(cfg, "greenhouse", "acme", html)

    assert reason is None
    assert _titles(jobs) == ["Staff Engineer"]


def test_parse_html_board_keeps_short_real_department_names(cfg) -> None:
    """The junk-TITLE policy must not be applied to `team`.

    Twilio's board really does have an "IT" department; running the title
    rules over it (min 3 chars) would silently drop the corroborating
    component `rli.history.matching` relies on.
    """
    html = GREENHOUSE_HTML.replace(
        '<h3 class="section-header font-primary">Accounting</h3>',
        '<h3 class="section-header font-primary">IT</h3>',
    )
    jobs, _ = _parse_html_board(cfg, "greenhouse", "twilio", html)

    assert {job.team for job in jobs} == {"IT"}


def test_parse_html_board_keeps_remote_as_a_location(cfg) -> None:
    """ "Remote" is a non-TITLE but a perfectly good location."""
    html = LEVER_HTML.replace(
        '<span class="sort-by-location posting-category location">India</span>',
        '<span class="sort-by-location posting-category location">Remote</span>',
    )
    jobs, _ = _parse_html_board(cfg, "lever", "gohighlevel", html)

    assert jobs[1].location == "Remote"
