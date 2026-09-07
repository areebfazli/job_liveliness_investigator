"""Archive backfill: historical board snapshots from Wayback captures.

Standalone data-collection module (like a cron job), NOT a `rli.probes.base.
Probe` subclass — it takes a `Config` and a `sqlite3.Connection`, builds its
own `NetClient` via `NetClient.from_config(cfg, probe="archive_backfill",
conn=conn)`, and persists directly through `rli.probes.persist` (spec.md §7:
`board_snapshots` / `board_snapshot_jobs` / `capture_attempts` / `companies`
only — `postings` and `posting_snapshots` are out of scope for this module).

For each target (an ATS tenant with a known `{ats, tenant, website_domain,
company_name}`), the priority order is:

1. Try the ATS's documented board API URL first (the richest source — full
   job objects, same shape `rli.resolvers.*` already parses for live
   fetches).
2. If Wayback has zero usable (HTTP 200) captures of the API URL in the
   requested window, fall back to the public, human-facing board page
   URL(s) for that ATS, tried in order, and tolerantly scrape job links out
   of the archived HTML with BeautifulSoup.
3. Whichever pattern is the first to have >=1 usable capture is the one
   used for that company; captures of the other pattern(s) are not queried.

Every CDX list call and every capture-body fetch is logged to
`capture_attempts` (spec.md §4 "coverage gap, never absence") whether it
succeeds or fails. A `board_snapshots` row is written ONLY for a capture
that was both fetched and successfully parsed — a failed fetch or an
unparseable body produces a `capture_attempts` row with `ok=False` and
nothing else.

GUESSED / judgment calls made in this module (see also `rli.archive.cdx` and
`rli.archive.fetch` docstrings for the underlying Wayback API GUESSES):

* CDX prefix matching (`url_pattern + "*"`) is applied only to the API URL
  pattern, to tolerate query-string variance in what Wayback's crawler
  happened to fetch (e.g. a captured Greenhouse URL with or without
  `?content=true`). Page-page URL patterns are queried as exact matches,
  because CDX prefix matching has no path-boundary awareness — `"…/acme*"`
  would also match a *different* tenant like `"…/acme-corp"` — and an exact
  tenant match is safer here at the cost of possibly missing a captured
  trailing-slash variant.
* `--months N` is approximated as `N * 30` calendar days back from now
  (there is no calendar-month arithmetic dependency in this project); this
  is a documented approximation, not exact month arithmetic.
* HTML board-page extraction (see `_parse_html_board`) is a best-effort,
  tolerant heuristic — it looks for `<a href>`s shaped like each ATS's job
  URL and takes the link text as the title and the last path segment as the
  job id. It is expected to yield zero results for a JS-rendered board (e.g.
  Ashby's page is a client-rendered SPA), which is exactly why a
  zero-job-link HTML capture is recorded as `coverage_status="partial"`
  rather than `"complete"`.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

from rli.archive import cdx as cdx_mod
from rli.archive import fetch as fetch_mod
from rli.config import Config
from rli.models.time import now_utc, to_utc_z
from rli.net import NetClient
from rli.probes import persist
from rli.probes.board_snapshot import BoardJob
from rli.resolvers import ashby as ashby_resolver
from rli.resolvers import greenhouse as greenhouse_resolver
from rli.resolvers import lever as lever_resolver

__all__ = [
    "API_URL_TEMPLATES",
    "PAGE_URL_TEMPLATES",
    "CompanySummary",
    "candidate_urls",
    "filter_targets",
    "parse_wayback_timestamp",
    "read_targets_csv",
    "run_backfill",
]

# Documented ATS board-API URL templates (task contract #6). Query params
# (e.g. Greenhouse's `?content=true`) are added at live-fetch time by
# `rli.resolvers.*`; here they are irrelevant to the CDX *pattern* since the
# API pattern is queried with prefix matching (see module docstring).
API_URL_TEMPLATES: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{tenant}",
    "lever": "https://api.lever.co/v0/postings/{tenant}",
}

# Public, human-facing board page URL templates (HTML fallback). Greenhouse
# gets two: tenants migrated between `boards.greenhouse.io` and
# `job-boards.greenhouse.io` over time, so both are tried in order.
PAGE_URL_TEMPLATES: dict[str, list[str]] = {
    "greenhouse": [
        "https://boards.greenhouse.io/{tenant}",
        "https://job-boards.greenhouse.io/{tenant}",
    ],
    "ashby": ["https://jobs.ashbyhq.com/{tenant}"],
    "lever": ["https://jobs.lever.co/{tenant}"],
}

# Best-effort HTML job-link heuristics (task contract #6). GUESSED —
# best-effort/tolerant, not verified against live board-page markup.
_GREENHOUSE_JOB_HREF = re.compile(r"/jobs/(\d+)/?(?:[?#].*)?$")
_UUID_TAIL = re.compile(
    r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"/?(?:[?#].*)?$"
)


def candidate_urls(ats: str, tenant: str) -> list[tuple[str, str]]:
    """Ordered `(kind, url)` candidates for one `{ats, tenant}` — API first, then HTML pages."""
    api_url = API_URL_TEMPLATES[ats].format(tenant=tenant)
    page_urls = [t.format(tenant=tenant) for t in PAGE_URL_TEMPLATES[ats]]
    return [("api", api_url)] + [("html", u) for u in page_urls]


def _cdx_pattern(kind: str, url: str) -> str:
    """CDX query pattern for one candidate URL (see module docstring GUESS)."""
    return f"{url}*" if kind == "api" else url


def parse_wayback_timestamp(timestamp: str) -> datetime:
    """Parse a 14-digit Wayback capture timestamp (`YYYYMMDDHHMMSS`, UTC)."""
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def read_targets_csv(path: str | Path) -> list[dict[str, str]]:
    """Read `scripts/targets.csv`-shaped rows (`company_name, website_domain, ats, tenant, ...`)."""
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def filter_targets(targets: list[dict[str, str]], only: str | None) -> list[dict[str, str]]:
    """Keep targets whose tenant/company_name/website_domain contains `only` (case-insensitive)."""
    if not only:
        return targets
    needle = only.strip().lower()
    return [
        t
        for t in targets
        if needle in (t.get("tenant") or "").lower()
        or needle in (t.get("company_name") or "").lower()
        or needle in (t.get("website_domain") or "").lower()
    ]


def _upsert_company(conn, *, company_id: str, name: str | None, created_at: datetime) -> None:
    """Idempotently ensure a `companies` row exists before any FK-dependent insert."""
    conn.execute(
        """
        INSERT OR IGNORE INTO companies (company_id, name, website_domain, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (company_id, name, company_id, to_utc_z(created_at)),
    )
    conn.commit()


def _to_board_job(ats: str, job: object) -> BoardJob:
    """Mirror `rli.probes.board_snapshot._from_greenhouse/_from_ashby/_from_lever`."""
    if ats == "greenhouse":
        gh = job  # type: greenhouse_resolver.GreenhouseJob
        return BoardJob(
            job_id=gh.id,
            title=gh.title,
            team=gh.departments[0] if gh.departments else None,
            location=gh.location,
            url=gh.absolute_url,
            description_hash=gh.content_hash,
        )
    if ats == "ashby":
        ab = job  # type: ashby_resolver.AshbyJob
        return BoardJob(
            job_id=ab.id,
            title=ab.title,
            team=ab.team or ab.department,
            location=ab.location,
            url=ab.job_url,
            description_hash=ab.content_hash,
        )
    lv = job  # type: lever_resolver.LeverJob
    return BoardJob(
        job_id=lv.id,
        title=lv.text,
        team=lv.team,
        location=lv.location,
        url=lv.hosted_url,
        description_hash=lv.content_hash,
    )


def _parse_api_body(ats: str, body: str | None) -> tuple[list[BoardJob], bool]:
    """Parse an archived ATS API body via the live resolver's `_parse_job`.

    Returns `(jobs, parse_ok)`. `parse_ok=False` means the body was not the
    expected shape at all (invalid JSON, or not an object/list of the
    expected top-level shape) — the caller must not write a snapshot for
    that. `parse_ok=True` with an empty `jobs` list is a real "board had
    zero open jobs" observation, not a failure.
    """
    if body is None:
        return [], False
    try:
        raw = json.loads(body)
    except ValueError:
        return [], False

    if ats == "greenhouse":
        jobs_raw = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(jobs_raw, list):
            return [], False
        parsed = [greenhouse_resolver._parse_job(item) for item in jobs_raw]
    elif ats == "ashby":
        jobs_raw = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(jobs_raw, list):
            return [], False
        parsed = [ashby_resolver._parse_job(item) for item in jobs_raw]
    else:  # lever
        if not isinstance(raw, list):
            return [], False
        parsed = [lever_resolver._parse_job(item) for item in raw]

    jobs = [_to_board_job(ats, job) for job in parsed if job is not None]
    return jobs, True


def _parse_html_board(ats: str, tenant: str, html: str) -> list[BoardJob]:
    """Tolerant, best-effort job-link extraction from an archived board page.

    GUESSED heuristic (task contract #6): scans `<a href>` tags for the
    ATS's job-URL shape, taking the link text as `title` and the trailing
    path segment as `job_id`. Only `title`/`url`/`job_id` are populated —
    `team`/`location`/`description_hash` are not recoverable from a listing
    page. Zero results is an expected outcome for a JS-rendered board (e.g.
    Ashby's board page is a client-side SPA); the caller treats that as
    `coverage_status="partial"`, not a failure.
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[BoardJob] = []
    seen_ids: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(strip=True)
        if not title:
            continue

        job_id: str | None = None
        if ats == "greenhouse":
            match = _GREENHOUSE_JOB_HREF.search(href)
            if match:
                job_id = match.group(1)
        else:  # ashby, lever: both use `<tenant>/<uuid>` job URLs
            match = _UUID_TAIL.search(href)
            if match and tenant.lower() in href.lower():
                job_id = match.group(1)

        if not job_id or job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        jobs.append(BoardJob(job_id=job_id, title=title, url=href))

    return jobs


@dataclass(frozen=True, slots=True)
class CompanySummary:
    """Per-company backfill result, for the CLI to print and totals to roll up."""

    company_id: str
    ats: str
    tenant: str
    pattern_used: str | None  # "api" | "html" | None (no usable captures found)
    captures_found: int  # total captures returned by CDX for the winning pattern
    captures_parsed: int  # captures fetched + parsed into a board_snapshots row
    captures_failed: int  # captures whose fetch or parse failed


def _backfill_one_company(
    cfg: Config,
    conn,
    net: NetClient,
    target: dict[str, str],
    *,
    months: int,
    limit_captures: int | None,
    now: Callable[[], datetime],
) -> CompanySummary:
    company_id = (target.get("website_domain") or "").strip().lower()
    name = target.get("company_name")
    ats = (target.get("ats") or "").strip().lower()
    tenant = (target.get("tenant") or "").strip()

    moment = now()
    _upsert_company(conn, company_id=company_id, name=name, created_at=moment)

    # `--months N` back from now, approximated as N*30 calendar days (see
    # module docstring GUESS/judgment call).
    from_date = (moment - timedelta(days=30 * months)).date()
    to_date = moment.date()

    chosen: tuple[str, list[cdx_mod.CdxCapture]] | None = None
    for kind, url in candidate_urls(ats, tenant):
        pattern = _cdx_pattern(kind, url)
        cdx_result = cdx_mod.list_captures(net, pattern, from_date, to_date)
        persist.record_capture_attempt(
            conn,
            company_id=company_id,
            target=pattern,
            attempted_at=now(),
            source="archive",
            ok=cdx_result.ok,
            error=cdx_result.error,
            retryable=None if cdx_result.ok else cdx_result.retryable,
        )
        if cdx_result.ok and cdx_result.captures:
            chosen = (kind, cdx_result.captures)
            break

    if chosen is None:
        return CompanySummary(
            company_id=company_id,
            ats=ats,
            tenant=tenant,
            pattern_used=None,
            captures_found=0,
            captures_parsed=0,
            captures_failed=0,
        )

    kind, captures = chosen
    # Most-recent-first: a 14-digit timestamp string sorts chronologically.
    captures_sorted = sorted(captures, key=lambda c: c.timestamp, reverse=True)
    to_process = captures_sorted if limit_captures is None else captures_sorted[:limit_captures]

    captures_parsed = 0
    captures_failed = 0

    for capture in to_process:
        target_url = fetch_mod.capture_url(capture.timestamp, capture.original)
        fetch_result = fetch_mod.fetch_capture(net, capture.timestamp, capture.original)
        attempted_at = now()

        if not fetch_result.ok:
            persist.record_capture_attempt(
                conn,
                company_id=company_id,
                target=target_url,
                attempted_at=attempted_at,
                source="archive",
                ok=False,
                error=fetch_result.error,
                retryable=fetch_result.retryable,
            )
            captures_failed += 1
            continue

        if kind == "api":
            jobs, parse_ok = _parse_api_body(ats, fetch_result.body)
            coverage_status = "complete"
        else:
            jobs = _parse_html_board(ats, tenant, fetch_result.body or "")
            parse_ok = True
            coverage_status = "complete" if jobs else "partial"

        if not parse_ok:
            persist.record_capture_attempt(
                conn,
                company_id=company_id,
                target=target_url,
                attempted_at=attempted_at,
                source="archive",
                ok=False,
                error="archived response body could not be parsed into the expected shape",
                retryable=False,
            )
            captures_failed += 1
            continue

        captured_at = parse_wayback_timestamp(capture.timestamp)
        persist.save_board_snapshot(
            conn,
            company_id=company_id,
            captured_at=captured_at,
            coverage_status=coverage_status,
            jobs=jobs,
            source="archive",
        )
        persist.record_capture_attempt(
            conn,
            company_id=company_id,
            target=target_url,
            attempted_at=attempted_at,
            source="archive",
            ok=True,
            error=None,
            retryable=None,
        )
        captures_parsed += 1

    return CompanySummary(
        company_id=company_id,
        ats=ats,
        tenant=tenant,
        pattern_used=kind,
        captures_found=len(captures),
        captures_parsed=captures_parsed,
        captures_failed=captures_failed,
    )


def run_backfill(
    cfg: Config,
    conn,
    targets: list[dict[str, str]],
    *,
    months: int = 12,
    limit_captures: int | None = None,
    net: NetClient | None = None,
    now: Callable[[], datetime] = now_utc,
) -> list[CompanySummary]:
    """Backfill historical board snapshots for every row in `targets`.

    `targets` are dicts shaped like `scripts/targets.csv` rows (must have
    `company_name`, `website_domain`, `ats`, `tenant`) — read them with
    `read_targets_csv` or pass an injected list directly (e.g. from tests).

    `net`, when given, is used as-is and left open for the caller to manage
    (a test's fixture-owned `NetClient`, already bound to the
    `archive_backfill` allowlist). When omitted, a `NetClient` is built via
    `NetClient.from_config(cfg, probe="archive_backfill", conn=conn)` and
    closed before returning.
    """
    owns_net = net is None
    client = net if net is not None else NetClient.from_config(
        cfg, probe="archive_backfill", conn=conn
    )
    try:
        return [
            _backfill_one_company(
                cfg, conn, client, target, months=months, limit_captures=limit_captures, now=now
            )
            for target in targets
        ]
    finally:
        if owns_net:
            client.close()
