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
   URL(s) for that ATS, tried in order, and extract the jobs out of the
   archived HTML (`_parse_html_board`) — a strictly poorer source, which is
   why everything it could not parse is reported as a coverage gap.
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
* HTML board-page extraction (see `_parse_html_board`) is per-ATS and
  VERIFIED against real Wayback captures of a Lever, a Greenhouse and an
  Ashby board (see each `_extract_*` docstring for the markup it was read
  from), with markup-agnostic fallbacks so an older or unknown template
  revision degrades instead of failing. It recovers `title`, `team`,
  `location` and `url`; `description_hash` is left `None` because a listing
  page carries no description. Ashby's page is a client-rendered SPA with
  zero `<a>` tags, so its jobs are read from the inline `window.__appData`
  JSON blob instead of from links — an unparseable/absent blob yields zero
  jobs, never fabricated ones.
* A Greenhouse board page is PAGINATED (the verified twilio capture renders
  50 of the 146 jobs it declares). An HTML capture of such a board is a
  coverage gap by construction, so the declared total is compared with what
  was extracted and the capture is downgraded — otherwise the jobs on pages
  2..N would look absent and `rli.history.closures` would invent closures
  for them.
* Extracted jobs pass the shared title-quality policy in
  `rli.history.titles` before they are persisted (a junk title such as
  "Apply" is DROPPED, and a page whose titles are degenerate is dropped
  wholesale). Any drop — including "zero jobs extracted" — downgrades the
  capture to `coverage_status="partial"`. Because `board_snapshots` has no
  column for WHY, the reason is recorded in `capture_attempts.error` for
  that capture's URL with `ok=True` (the fetch itself succeeded); that is
  the only writable place for it given the fixed schema.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from rli.archive import cdx as cdx_mod
from rli.archive import fetch as fetch_mod
from rli.config import Config
from rli.history.titles import (
    dominant_title_fraction,
    is_degenerate_page,
    is_junk_title,
    junk_title_reason,
)
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

# --- HTML board-page extraction -------------------------------------------
# Job-URL shapes used to recognise a job link (and to recover a job id from
# one). VERIFIED against the archived captures listed in `_parse_html_board`.
_GREENHOUSE_JOB_HREF = re.compile(r"/jobs/(\d+)/?(?:[?#].*)?$")
# Tenant segment of an ABSOLUTE Greenhouse job URL, used to reject a link to
# a different company's board (a "related openings" module would otherwise
# inject phantom jobs into this company's snapshot). Relative hrefs do not
# match and are left alone.
_GREENHOUSE_TENANT_HREF = re.compile(r"greenhouse\.io/([^/?#]+)/jobs/\d+")
_UUID_TAIL = re.compile(
    r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"/?(?:[?#].*)?$"
)

# Ashby's board page is a client-rendered SPA whose server HTML carries the
# whole board as one inline `window.__appData = {...};` JSON assignment.
_ASHBY_APP_DATA = re.compile(r"window\s*\.\s*__appData\s*=\s*")

# Greenhouse's `job-boards.greenhouse.io` template PAGINATES: the capture of
# twilio's board renders 50 job rows while its embedded state says
# `"jobPosts":{"count":50,"page":1,"total":146,"total_pages":3,…}`. A capture
# holding page 1 of 3 is a coverage gap by definition, so the declared total
# is read back and compared with what was extracted (see
# `_greenhouse_declared_total`). `[^{]*?` keeps the scan inside the
# `jobPosts` header and out of the nested `data` array.
_GREENHOUSE_DECLARED_TOTAL = re.compile(r'"jobPosts"\s*:\s*\{[^{]*?"total"\s*:\s*(\d+)')
_GREENHOUSE_COUNT_HEADER = re.compile(r"^([\d,]+)\s+jobs?\b", re.IGNORECASE)

_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")

# Attributes that carry a human-readable role name on a card or its anchor,
# in descending trustworthiness. `title` is last because on a board page it
# is very often the tooltip of an Apply button.
_TITLE_ATTRS: tuple[str, ...] = ("aria-label", "data-title", "data-job-title", "title")

# Direct-child subtrees that carry job METADATA (location chips, "New"
# badges, commitment labels) rather than the role name. They are skipped
# when a card's text has to be used as a title candidate — this is what
# stops Greenhouse's `<p>title</p><p>location</p>` anchor from collapsing
# into "IT Internal AuditorRemote - India".
_METADATA_CLASSES: tuple[str, ...] = (
    "posting-categories",
    "posting-category",
    "tag-container",
    "sort-by-location",
    "sort-by-commitment",
    "sort-by-time",
    "workplaceTypes",
    "location",
    "commitment",
    "job-location",
    "body--metadata",
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


@dataclass(frozen=True, slots=True)
class _Extracted:
    """One job entry lifted from board-page markup, BEFORE title quality control.

    `title_candidates` is an ordered, best-first list of strings that might
    be the role name; `_choose_title` takes the first one that survives
    `rli.history.titles.junk_title_reason`. Keeping candidates (rather than
    committing to one at extraction time) is what lets a card whose most
    obvious text is "Apply" still be rescued by a heading or an aria-label
    further down the list — and lets two anchors that share a job id (the
    Lever Apply button and the Lever title link) be merged into one entry
    instead of the first one winning.
    """

    job_id: str
    title_candidates: tuple[str, ...]
    team: str | None = None
    location: str | None = None
    url: str | None = None


def _class_names(node: Tag) -> list[str]:
    """CSS classes of `node` as a list (bs4 gives a str for a single class)."""
    raw = node.get("class")
    if isinstance(raw, str):
        return [raw]
    return list(raw or [])


def _clean(value: str | None) -> str | None:
    """Collapse whitespace; return None for a blank result."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _text_skipping_metadata(node: Tag | None) -> str | None:
    """Text of `node` with direct-child metadata subtrees removed.

    Only DIRECT children are inspected, which is exactly where the observed
    metadata sits (Greenhouse's `.tag-container` "New" badge inside the
    title `<p>`, Lever's `.posting-categories` inside `a.posting-title`).
    Doing it this way avoids mutating the parsed tree.
    """
    if node is None:
        return None
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, Tag):
            if any(name in _METADATA_CLASSES for name in _class_names(child)):
                continue
            parts.append(child.get_text(" ", strip=True))
        else:
            parts.append(str(child))
    return _clean(" ".join(part for part in parts if part))


def _first_heading_text(node: Tag | None) -> str | None:
    """Text of the first non-empty heading inside `node`."""
    if node is None:
        return None
    for heading in node.find_all(_HEADING_TAGS):
        text = _clean(heading.get_text(" ", strip=True))
        if text:
            return text
    return None


def _preceding_heading_text(node: Tag | None) -> str | None:
    """Text of the nearest heading BEFORE `node` in document order.

    GUESSED fallback: on an unknown board template the heading a card sits
    under is usually its department, which is a poor title but a plausible
    one. It is therefore the LAST title candidate (and every job in a
    section would then share it, which the page-level degeneracy guard in
    `_parse_html_board` catches). Bounded to a few hops so this stays cheap
    on a large page.
    """
    if node is None:
        return None
    current: Tag | None = node
    for _ in range(3):
        heading = current.find_previous(_HEADING_TAGS)
        if heading is None:
            return None
        text = _clean(heading.get_text(" ", strip=True))
        if text:
            return text
        current = heading
    return None


def _attr_titles(*nodes: Tag | None) -> list[str]:
    """Title-ish attribute values of `nodes`, in `_TITLE_ATTRS` order."""
    out: list[str] = []
    for node in nodes:
        if node is None:
            continue
        for attr in _TITLE_ATTRS:
            value = node.get(attr)
            cleaned = _clean(value) if isinstance(value, str) else None
            if cleaned:
                out.append(cleaned)
    return out


def _title_candidates(
    card: Tag | None,
    anchor: Tag | None,
    *,
    primary: Iterable[str | None],
    min_chars: int,
) -> tuple[str, ...]:
    """Ordered, best-first title candidates for one job card.

    `primary` holds the ATS-specific sources (verified selectors); the rest
    is markup-agnostic so an unknown/older template degrades instead of
    failing: a heading inside the card, then title-ish attributes, then the
    anchor's own text with metadata subtrees stripped.

    The nearest PRECEDING heading is a last resort in both senses — it is
    the weakest source (usually a section/department name, which is why the
    page-level degeneracy guard exists), and it is the only source that
    costs a backwards walk of the document. It is therefore computed only
    when nothing better survived the junk filter, which keeps extraction
    linear in the common case on a large board page.
    """
    candidates: list[str] = []
    for value in (
        *primary,
        _first_heading_text(card),
        *_attr_titles(anchor, card),
        _text_skipping_metadata(anchor),
    ):
        if value and value not in candidates:
            candidates.append(value)
    if all(is_junk_title(candidate, min_chars=min_chars) for candidate in candidates):
        last_resort = _preceding_heading_text(anchor or card)
        if last_resort and last_resort not in candidates:
            candidates.append(last_resort)
    return tuple(candidates)


def _select_text(node: Tag | None, selector: str) -> str | None:
    """Text of the first `selector` match inside `node`."""
    if node is None:
        return None
    found = node.select_one(selector)
    return _clean(found.get_text(" ", strip=True)) if found is not None else None


def _extract_lever(soup: BeautifulSoup, *, min_chars: int) -> list[_Extracted]:
    """Lever `jobs.lever.co/{tenant}` list page (VERIFIED, capture 20260713).

        <div class="postings-group">
          <div class="large-category-header">Customer Success</div>
          <div class="posting-category-title">Implementation Services</div>
          <div class="posting" data-qa-posting-id="ff2ad979-…">
            <div class="posting-apply"><a href="…/ff2ad979-…">Apply</a></div>
            <a class="posting-title" href="…/ff2ad979-…">
              <h5 data-qa="posting-name">Implementation Advisor</h5>
              <div class="posting-categories">
                <span class="…workplaceTypes">Remote — </span>
                <span class="sort-by-commitment …">EOR Mexico</span>
                <span class="sort-by-location …">Mexico</span>
              </div>
            </a>
          </div>
        </div>

    The `.posting-apply` anchor precedes `a.posting-title` and carries the
    SAME job UUID — that document order is the root cause of the archived
    "Apply" titles, so it is skipped here and, if a future template moves
    it, still loses to the real title during candidate merging.

    JUDGMENT CALL on `team`: `.posting-category-title` is preferred over
    `.large-category-header`, because Lever renders the group header as the
    DEPARTMENT (emitted once, then omitted for following groups of the same
    department — see the fixture's headerless "CRM & Automation" group)
    and the category title as the TEAM. `BoardJob.team` from the API path
    is `categories.team`, so this keeps HTML-derived and API-derived rows
    comparable for `rli.history.matching`. The department is carried
    forward across headerless groups and used only as a fallback.
    """
    postings = soup.select("div.posting")
    if not postings:
        return []

    out: list[_Extracted] = []
    current_group: Tag | None = None
    department: str | None = None

    for posting in postings:
        group = posting.find_parent(class_="postings-group")
        if group is not current_group:
            current_group = group
            header = _select_text(group, ".large-category-header")
            if header:
                department = header
        team = _select_text(group, ".posting-category-title") or department

        anchor = posting.select_one("a.posting-title")
        if anchor is None:
            for candidate in posting.find_all("a", href=True):
                if candidate.find_parent(class_="posting-apply") is None:
                    anchor = candidate
                    break

        href = anchor.get("href") if anchor is not None else None
        job_id = _clean(posting.get("data-qa-posting-id"))
        if not job_id and isinstance(href, str):
            match = _UUID_TAIL.search(href)
            job_id = match.group(1) if match else None
        if not job_id:
            continue

        out.append(
            _Extracted(
                job_id=job_id,
                title_candidates=_title_candidates(
                    posting,
                    anchor,
                    primary=(
                        _select_text(posting, 'h5[data-qa="posting-name"]'),
                        _select_text(posting, ".posting-title h5"),
                    ),
                    min_chars=min_chars,
                ),
                team=team,
                location=(
                    _select_text(posting, ".posting-categories .sort-by-location")
                    or _select_text(posting, ".sort-by-location")
                ),
                url=href if isinstance(href, str) else None,
            )
        )
    return out


def _href_is_tenant(href: str, tenant: str) -> bool:
    """False when `href` is another Greenhouse tenant's job URL."""
    match = _GREENHOUSE_TENANT_HREF.search(href)
    return match is None or match.group(1).lower() == tenant.lower()


def _extract_greenhouse(soup: BeautifulSoup, tenant: str, *, min_chars: int) -> list[_Extracted]:
    """Greenhouse `job-boards.greenhouse.io/{tenant}` (VERIFIED, capture 20260824).

        <div class="job-posts--table--department">
          <h3 class="section-header font-primary">Accounting</h3>
          …<tr class="job-post"><td class="cell">
            <a href="https://job-boards.greenhouse.io/twilio/jobs/7982861">
              <p class="body body--medium">IT Internal Auditor</p>
              <p class="body__secondary body--metadata">Remote - India</p>
            </a>
          </td></tr>
        </div>

    One anchor wraps two sibling `<p>`s with no separator, which is why
    `a.get_text()` produced "IT Internal AuditorRemote - India". The FIRST
    direct-child `<p>` is the title (minus a possible `.tag-container`
    "New" badge), the SECOND is the location. The older
    `boards.greenhouse.io` template (`div.opening > a` + a sibling
    `span.location`) has no such `<p>`s and falls through to the generic
    candidates, which is also what keeps a bare `<a>Title</a>` working.
    """
    out: list[_Extracted] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        match = _GREENHOUSE_JOB_HREF.search(href)
        if match is None or not _href_is_tenant(href, tenant):
            continue

        paragraphs = [c for c in anchor.children if isinstance(c, Tag) and c.name == "p"]
        title_text = _text_skipping_metadata(paragraphs[0]) if paragraphs else None
        location = (
            _clean(paragraphs[1].get_text(" ", strip=True)) if len(paragraphs) > 1 else None
        )

        card = anchor.find_parent("tr") or anchor.parent or anchor
        department = anchor.find_parent(class_="job-posts--table--department")
        team = _select_text(department, "h3.section-header") or _select_text(department, "h3")
        if team is None:
            # Unknown/older template: the nearest heading above the card is
            # its department on `boards.greenhouse.io`, but on anything else
            # it is as likely to be page chrome, so it is filtered. The
            # length rule is relaxed to 1 char on purpose — real departments
            # are called "IT", "HR", "QA", and the title-oriented default of
            # `matching.junk_title_min_chars` (3) would discard them.
            heading = _preceding_heading_text(anchor)
            team = heading if heading and not is_junk_title(heading, min_chars=1) else None

        out.append(
            _Extracted(
                job_id=match.group(1),
                title_candidates=_title_candidates(
                    card, anchor, primary=(title_text,), min_chars=min_chars
                ),
                team=team,
                location=location or _select_text(card, ".location, .job-location"),
                url=href,
            )
        )
    return out


def _ashby_app_data(html: str) -> dict | None:
    """The `window.__appData` JSON object from an Ashby board page, or None.

    Parsed out of the RAW html (not the soup) because the payload lives in
    an inline `<script>` and needs no entity decoding. The object's extent
    is found with `json.JSONDecoder.raw_decode`, which stops at the
    balanced closing brace and therefore does not care what follows the
    assignment. Best-effort by construction: any failure returns None and
    the capture becomes a documented coverage gap rather than a fabricated
    empty board.
    """
    match = _ASHBY_APP_DATA.search(html)
    if match is None:
        return None
    start = html.find("{", match.end())
    if start == -1:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[start:])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_ashby(html: str, tenant: str) -> list[_Extracted]:
    """Ashby `jobs.ashbyhq.com/{tenant}` (VERIFIED, capture 20260810).

    The served HTML contains ZERO `<a>` tags — it is a pure SPA shell — so
    there is nothing for a link scraper to find. The entire board is
    inlined as `window.__appData`, whose `jobBoard.jobPostings` entries
    carry `{id, title, locationName, teamId}` and whose `jobBoard.teams`
    maps a team id to `{name, externalName}` (`externalName` preferred: it
    is the public-facing label). No job URL is present in the payload, so
    it is reconstructed from the documented board URL template.
    """
    payload = _ashby_app_data(html)
    board = payload.get("jobBoard") if isinstance(payload, dict) else None
    if not isinstance(board, dict):
        return []
    postings = board.get("jobPostings")
    if not isinstance(postings, list):
        return []

    team_names: dict[str, str] = {}
    for team in board.get("teams") or []:
        if isinstance(team, dict) and team.get("id"):
            name = _clean(team.get("externalName")) or _clean(team.get("name"))
            if name:
                team_names[str(team["id"])] = name

    base_url = PAGE_URL_TEMPLATES["ashby"][0].format(tenant=tenant)
    out: list[_Extracted] = []
    for posting in postings:
        if not isinstance(posting, dict) or not posting.get("id"):
            continue
        job_id = str(posting["id"])
        title = _clean(posting.get("title")) if isinstance(posting.get("title"), str) else None
        team_id = posting.get("teamId")
        out.append(
            _Extracted(
                job_id=job_id,
                title_candidates=(title,) if title else (),
                team=team_names.get(str(team_id)) if team_id else None,
                location=(
                    _clean(posting.get("locationName"))
                    if isinstance(posting.get("locationName"), str)
                    else None
                ),
                url=f"{base_url}/{job_id}",
            )
        )
    return out


def _greenhouse_declared_total(soup: BeautifulSoup, html: str) -> int | None:
    """How many jobs a Greenhouse board page SAYS it has, or None if unstated.

    VERIFIED (capture 20260824 of job-boards.greenhouse.io/twilio): the page
    renders only the first 50 of 146 jobs and states the real total twice —
    in the embedded board state (`"jobPosts":{"count":50,"page":1,
    "total":146,"total_pages":3,…}`, authoritative) and in a
    `data-testid="job-count-header"` heading reading "146 jobs". Only those
    two sources are trusted; a looser "N jobs" text scan would pick up
    unrelated marketing copy.

    This matters far more than it looks: without it a paginated capture
    would be stored as a `'complete'` board missing 96 jobs, and
    `rli.history.closures` would read those 96 as closed on that date —
    fabricating exactly the absences spec.md §4 forbids.
    """
    match = _GREENHOUSE_DECLARED_TOTAL.search(html)
    if match is not None:
        return int(match.group(1))
    header = soup.select_one('[data-testid="job-count-header"]')
    if header is not None:
        text = _clean(header.get_text(" ", strip=True)) or ""
        header_match = _GREENHOUSE_COUNT_HEADER.match(text)
        if header_match is not None:
            return int(header_match.group(1).replace(",", ""))
    return None


def _extract_generic(
    soup: BeautifulSoup, ats: str, tenant: str, *, min_chars: int
) -> list[_Extracted]:
    """Last-resort anchor scan for an ATS template this module does not know.

    GUESSED heuristic: any `<a href>` whose shape matches the ATS's job-URL
    pattern is treated as a job card. Anchors inside a Lever
    `.posting-apply` wrapper are skipped explicitly; anything else that
    still yields only "Apply" is dropped by the title quality gate rather
    than persisted.
    """
    out: list[_Extracted] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if anchor.find_parent(class_="posting-apply") is not None:
            continue
        if ats == "greenhouse":
            match = _GREENHOUSE_JOB_HREF.search(href)
            job_id = match.group(1) if match and _href_is_tenant(href, tenant) else None
        else:  # ashby, lever: both use `{tenant}/{uuid}` job URLs
            match = _UUID_TAIL.search(href)
            job_id = match.group(1) if match and tenant.lower() in href.lower() else None
        if not job_id:
            continue
        card = anchor.find_parent("li") or anchor.find_parent("tr") or anchor.parent or anchor
        out.append(
            _Extracted(
                job_id=job_id,
                title_candidates=_title_candidates(
                    card, anchor, primary=(), min_chars=min_chars
                ),
                team=None,
                location=_select_text(card, ".location, .job-location"),
                url=href,
            )
        )
    return out


def _merge_entries(entries: list[_Extracted]) -> list[_Extracted]:
    """Collapse entries sharing a job id, unioning their title candidates.

    Two anchors can describe the same job (Lever's Apply button and its
    title link). The previous extractor deduped by keeping the FIRST anchor
    seen, which is precisely how "Apply" became 8k stored titles. Merging
    instead means the weak candidate only loses to a better one.
    """
    merged: dict[str, _Extracted] = {}
    for entry in entries:
        previous = merged.get(entry.job_id)
        if previous is None:
            merged[entry.job_id] = entry
            continue
        candidates = list(previous.title_candidates)
        candidates.extend(c for c in entry.title_candidates if c not in candidates)
        merged[entry.job_id] = _Extracted(
            job_id=entry.job_id,
            title_candidates=tuple(candidates),
            team=previous.team or entry.team,
            location=previous.location or entry.location,
            url=previous.url or entry.url,
        )
    return list(merged.values())


def _choose_title(candidates: tuple[str, ...], *, min_chars: int) -> tuple[str | None, str | None]:
    """First non-junk candidate, else `(None, why the best candidate failed)`."""
    first_reason: str | None = None
    for candidate in candidates:
        reason = junk_title_reason(candidate, min_chars=min_chars)
        if reason is None:
            return candidate, None
        if first_reason is None:
            first_reason = reason
    return None, first_reason or "no title text found on the job card"


def _parse_html_board(
    cfg: Config, ats: str, tenant: str, html: str
) -> tuple[list[BoardJob], str | None]:
    """Extract jobs from an archived board page, with title quality control.

    Returns `(jobs, reason)`. `reason` is `None` only when EVERY extracted
    job survived with a plausible title; otherwise it is a human-readable
    string explaining what was lost, and the caller must persist the
    capture as `coverage_status='partial'` (spec.md §4: a capture we could
    not fully parse is a coverage GAP, never evidence of absence —
    `rli.history.closures` reads absence only from `'complete'` captures,
    so downgrading here is what stops a bad scrape from fabricating
    closures). Zero surviving jobs is therefore a `'partial'` snapshot with
    no job rows, never an empty `'complete'` board.

    Extraction is per-ATS and ordered, each recipe VERIFIED against a real
    Wayback capture (see `_extract_lever` / `_extract_greenhouse` /
    `_extract_ashby`), and each falling back to markup-agnostic candidates
    (`_title_candidates`) so an older or unknown template degrades
    instead of failing. `team` and `location` are recovered where the
    markup carries them — they are the corroborating components
    `rli.history.matching` needs. `description_hash` stays `None`: a
    listing page has no description.

    Two quality gates, both configured from `[matching]` rather than
    hardcoded here, so the extractor and `rli.history.matching` cannot
    drift apart:

    1. per-job — a job whose best title candidate is junk per
       `rli.history.titles.junk_title_reason` (using
       `matching.junk_title_min_chars`) is DROPPED. A junk or `None` title
       is never persisted.
    2. per-page — if one title dominates the survivors beyond
       `matching.page_shared_title_max_fraction` (from
       `matching.page_shared_title_min_jobs` jobs up), the whole capture is
       an extraction failure and ALL its jobs are dropped.

    A third, non-dropping gate covers UNDER-extraction: if the page claims
    more jobs than were extracted — a paginated Greenhouse capture (page 1
    of 3), or a Lever `.posting` card whose job id could not be recovered —
    the jobs that WERE found are kept, since they really were open, but the
    capture is still reported partial, because the jobs that were missed
    would otherwise read as absent.

    NOTE: the junk-TITLE policy is applied to titles only. `team` and
    `location` come from verified structural selectors and are stored as
    found — running the title policy over them would be actively wrong in
    both directions: it discards the real Greenhouse department "IT" (two
    characters, below `junk_title_min_chars`) and the real Lever location
    "Remote" (deliberately listed in `rli.history.titles.GENERIC_TITLES` as
    a non-TITLE). The one exception is the nearest-preceding-heading team
    fallback in `_extract_greenhouse`, which is guarded because it is the
    only unstructured team source.
    """
    min_chars = cfg.matching.junk_title_min_chars
    soup = BeautifulSoup(html, "html.parser")

    # How many jobs the PAGE claims to hold, and why a shortfall would
    # happen. Compared against what was extracted so that under-extraction
    # (a paginated Greenhouse capture, a Lever card whose id moved) is a
    # reported gap rather than a silently short "complete" board.
    declared_total: int | None = None
    shortfall_note = ""
    if ats == "ashby":
        entries = _extract_ashby(html, tenant)
    elif ats == "lever":
        entries = _extract_lever(soup, min_chars=min_chars)
        declared_total = len(soup.select("div.posting")) or None
        shortfall_note = "unrecognized posting markup"
    else:
        entries = _extract_greenhouse(soup, tenant, min_chars=min_chars)
        declared_total = _greenhouse_declared_total(soup, html)
        shortfall_note = "paginated board"
    if not entries:
        entries = _extract_generic(soup, ats, tenant, min_chars=min_chars)
    entries = _merge_entries(entries)

    jobs: list[BoardJob] = []
    drop_reasons: list[str] = []
    for entry in entries:
        title, why = _choose_title(entry.title_candidates, min_chars=min_chars)
        if title is None:
            drop_reasons.append(why or "title is empty")
            continue
        jobs.append(
            BoardJob(
                job_id=entry.job_id,
                title=title,
                team=entry.team,
                location=entry.location,
                url=entry.url,
                description_hash=None,
            )
        )

    reasons: list[str] = []
    if not entries:
        reasons.append(
            "no job entries could be extracted from the archived board page "
            "(JS-rendered board?)"
        )
    elif drop_reasons:
        reasons.append(
            f"{len(drop_reasons)} of {len(entries)} job entries had no plausible title "
            f"({drop_reasons[0]})"
        )

    if declared_total is not None and declared_total > len(entries):
        reasons.append(
            f"board page declares {declared_total} jobs but only {len(entries)} could be "
            f"extracted ({shortfall_note})"
        )

    titles: list[str | None] = [job.title for job in jobs]
    if is_degenerate_page(
        titles,
        max_fraction=cfg.matching.page_shared_title_max_fraction,
        min_jobs=cfg.matching.page_shared_title_min_jobs,
    ):
        reasons.append(
            f"one title covers {dominant_title_fraction(titles):.0%} of {len(titles)} jobs "
            "(extraction failure)"
        )
        jobs = []

    return jobs, ("partial coverage: " + "; ".join(reasons) if reasons else None)


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

        # `parse_reason` is the HTML extractor's explanation of what it had
        # to drop. `board_snapshots` has no column for it and the schema is
        # fixed, so it is carried on the SUCCESS `capture_attempts` row for
        # this capture (`ok=True`, because the fetch itself worked) — the
        # only writable place for it. `coverage_status` is downgraded to
        # `'partial'` whenever a reason exists, so a partly-parsed capture
        # can never be read as evidence of absence (spec.md §4).
        parse_reason: str | None = None
        if kind == "api":
            jobs, parse_ok = _parse_api_body(ats, fetch_result.body)
            coverage_status = "complete"
        else:
            jobs, parse_reason = _parse_html_board(cfg, ats, tenant, fetch_result.body or "")
            parse_ok = True
            coverage_status = "partial" if parse_reason else "complete"

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
            error=parse_reason,
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
