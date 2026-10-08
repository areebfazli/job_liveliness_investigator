"""Pilot (2026-10-08): archived per-job ATS pages as point-in-time evidence.

Question: do Wayback captures of individual job pages (not boards) add
dated publish/open/closed observations that our own collection and the
archived board captures lack?

Pipeline (each step resumable; every raw response cached on disk):

1. `ingest_cdx` — one CDX prefix query per (target, URL prefix) over the
   window, NO status filter and NO digest collapse (a collapsed run of
   identical captures would lose the last time a page was seen open).
   Every row that is a job page (or its application page) is normalised to
   `(ats, tenant, job_id, page_kind)` and mapped to our `posting_id` via
   `(company_id, ats_job_id)`.
2. `plan_fetches` / `run_fetches` — fetch a capped, prioritised subset of
   captures through the raw `id_` form, WITHOUT following the archived
   redirect (the redirect target is the closure signal on Greenhouse).
3. `parse_capture` — per-ATS parsers return a state (`open` / `closed` /
   `unknown`) plus the page's publish date when it carries one.
4. `infer_unfetched` — captures that were not fetched get a state inferred
   from the CDX status code only where that mapping was validated on the
   fetched sample (see the report); everything else stays `unknown`.

Rules carried over from spec.md §4: a throttled or failed fetch is a gap,
never a closure; no capture never means closed; a closure is the interval
between the last open and the first closed observation.

Nothing here writes to `data/rli.db`; it is opened with `mode=ro`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from rli.config import Config
from rli.net import RateLimiter, check_allowed
from rli.resolvers.common import parse_flexible_datetime
from rli.resolvers.jsonld import extract_job_posting

__all__ = [
    "ALLOWLIST",
    "TARGETS",
    "USER_AGENT",
    "JobUrl",
    "PageParse",
    "RawResponse",
    "Target",
    "WaybackClient",
    "cdx_prefixes",
    "infer_from_cdx",
    "normalize_job_url",
    "open_pilot_db",
    "parse_ashby_page",
    "parse_capture",
    "parse_greenhouse_page",
    "parse_lever_page",
    "select_captures",
]

# Same identity the repo's other archive/board scripts send (scripts/discover_boards.py).
USER_AGENT = "job-liveliness-investigator/1.0 (contact: uzair.khan@progbid.com)"
ALLOWLIST = ["web.archive.org"]
CDX_URL = "https://web.archive.org/cdx/search/cdx"
CDX_FIELDS = "urlkey,timestamp,original,mimetype,statuscode,digest,length"
WINDOW_FROM = "20251001"
WINDOW_TO = "20261008"


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    company_id: str
    ats: str
    tenant: str
    why: str


# Stratified 9 Greenhouse / 8 Ashby / 3 Lever. "dev" / "company" = the company
# has cases in dev-7d-v4 / company-7d-v4. "boards:N" = archived board captures
# already in rli.db, used to mix densely and sparsely archived companies.
# reddit, canonical and cohere deliberately left out (instruction).
TARGETS: tuple[Target, ...] = (
    Target("affirm.com", "greenhouse", "affirm", "dev; boards:19; known dense job-page archive"),
    Target("carta.com", "greenhouse", "carta", "dev; boards:26"),
    Target("discord.com", "greenhouse", "discord", "dev; boards:38"),
    Target("vercel.com", "greenhouse", "vercel", "dev; boards:14"),
    Target("webflow.com", "greenhouse", "webflow", "dev; boards:30"),
    Target("figma.com", "greenhouse", "figma", "dev; boards:14"),
    Target("brex.com", "greenhouse", "brex", "company; no archived boards in rli.db"),
    Target("instacart.com", "greenhouse", "instacart", "company; no archived boards"),
    Target("duolingo.com", "greenhouse", "duolingo", "company; no archived boards"),
    Target("notion.com", "ashby", "notion", "dev; boards:9"),
    Target("vanta.com", "ashby", "vanta", "dev; boards:9"),
    Target("cognition.com", "ashby", "cognition", "dev; boards:10"),
    Target("incident.io", "ashby", "incident", "dev; boards:2"),
    Target("modal.com", "ashby", "modal", "dev; boards:14"),
    Target("cursor.com", "ashby", "cursor", "company; boards:1"),
    Target("temporal.io", "ashby", "temporal", "company; no archived boards"),
    Target("drata.com", "ashby", "drata", "company; no archived boards"),
    Target("binance.com", "lever", "binance", "company; no archived boards"),
    Target("includedhealth.com", "lever", "includedhealth", "dev; boards:5"),
    Target("extremenetworks.com", "lever", "extremenetworks", "dev; boards:10"),
)


def cdx_prefixes(ats: str, tenant: str) -> list[str]:
    """URL prefixes (CDX `matchType=prefix`) holding a tenant's job pages.

    The trailing slash keeps a prefix from also matching a different tenant
    whose slug merely starts with this one (`acme` vs `acme-corp`).
    """
    if ats == "greenhouse":
        return [f"job-boards.greenhouse.io/{tenant}/jobs/", f"boards.greenhouse.io/{tenant}/jobs/"]
    if ats == "ashby":
        return [f"jobs.ashbyhq.com/{tenant}/"]
    if ats == "lever":
        return [f"jobs.lever.co/{tenant}/"]
    raise ValueError(f"unsupported ats {ats!r}")


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_GH_PATH = re.compile(r"^/(?P<t>[^/]+)/jobs/(?P<id>\d{4,20})(?P<rest>/[^?]*)?/?$")
_ASHBY_PATH = re.compile(rf"^/(?P<t>[^/]+)/(?P<id>{_UUID})(?P<rest>/[^?]*)?/?$")
_LEVER_PATH = re.compile(rf"^/(?P<t>[^/]+)/(?P<id>{_UUID})(?P<rest>/[^?]*)?/?$")


@dataclass(frozen=True)
class JobUrl:
    """A capture URL reduced to the job it shows."""

    ats: str
    tenant: str
    job_id: str
    page_kind: str  # "job" | "application"
    host: str

    @property
    def canonical(self) -> str:
        if self.ats == "greenhouse":
            return f"https://job-boards.greenhouse.io/{self.tenant}/jobs/{self.job_id}"
        if self.ats == "ashby":
            return f"https://jobs.ashbyhq.com/{self.tenant}/{self.job_id}"
        return f"https://jobs.lever.co/{self.tenant}/{self.job_id}"


def normalize_job_url(original: str, ats: str, tenant: str) -> JobUrl | None:
    """Reduce a captured URL to `(job_id, page_kind)`, or None if not a job page.

    Query strings (`gh_src`, `utm_*`, `embed=true`, ...) and the scheme are
    dropped; the tenant must match case-insensitively. Greenhouse
    `/confirmation` pages (shown after applying) are not job pages.
    """
    try:
        parsed = urlparse(original.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if ats == "greenhouse":
        if host not in {"job-boards.greenhouse.io", "boards.greenhouse.io"}:
            return None
        m = _GH_PATH.match(path)
        if not m or m["t"].lower() != tenant.lower():
            return None
        rest = (m["rest"] or "").strip("/")
        if rest:
            return None
        return JobUrl("greenhouse", tenant, m["id"], "job", host)
    if ats == "ashby":
        if host != "jobs.ashbyhq.com":
            return None
        m = _ASHBY_PATH.match(path)
        if not m or m["t"].lower() != tenant.lower():
            return None
        rest = (m["rest"] or "").strip("/")
        if rest not in ("", "application"):
            return None
        kind = "application" if rest else "job"
        return JobUrl("ashby", tenant, m["id"].lower(), kind, host)
    if ats == "lever":
        if host != "jobs.lever.co":
            return None
        m = _LEVER_PATH.match(path)
        if not m or m["t"].lower() != tenant.lower():
            return None
        rest = (m["rest"] or "").strip("/")
        if rest not in ("", "apply"):
            return None
        kind = "application" if rest else "job"
        return JobUrl("lever", tenant, m["id"].lower(), kind, host)
    return None


# ---------------------------------------------------------------------------
# Page parsers
# ---------------------------------------------------------------------------


@dataclass
class PageParse:
    """What one archived job-page response says about the job at capture time."""

    state: str  # "open" | "closed" | "unknown"
    parser: str
    published_raw: str | None = None
    published_at: datetime | None = None
    updated_raw: str | None = None
    title: str | None = None
    notes: str = ""


_GH_PUBLISHED = re.compile(r'"published_at"\s*:\s*"([^"]+)"')
_GH_TITLE = re.compile(r'"jobPost"\s*:\s*\{[^{}]*?"title"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _location_target(location: str | None) -> str | None:
    """The archived URL a Wayback `id_` redirect points at (strip `/web/<ts>id_/`)."""
    if not location:
        return None
    m = re.search(r"/web/\d{1,14}(?:id_)?/(.+)$", location)
    return m.group(1) if m else location


def parse_greenhouse_page(status: int, location: str | None, body: str | None) -> PageParse:
    """Greenhouse hosted job page (job-boards / boards.greenhouse.io).

    * 200 with an embedded `jobPost` → open; `published_at` is the job's
      first-publish time (matched the board API's `first_published` on the
      spot checks — see report).
    * 3xx to `<board>?error=true` → closed (Greenhouse's "job not found").
    * 3xx to the same job on another Greenhouse host → unknown (host move).
    * 404/410 → closed. Anything else → unknown.
    """
    if status in (301, 302, 303, 307, 308):
        target = _location_target(location) or ""
        if "error=true" in target:
            return PageParse("closed", "gh_redirect_error", notes=target[:200])
        if "greenhouse.io" in target and "/jobs/" in target:
            return PageParse("unknown", "gh_redirect_host_move", notes=target[:200])
        if "gh_jid=" in target:
            return PageParse("unknown", "gh_redirect_careers_site", notes=target[:200])
        return PageParse("unknown", "gh_redirect_other", notes=target[:200])
    if status in (404, 410):
        return PageParse("closed", "gh_http_gone")
    if status != 200 or not body:
        return PageParse("unknown", "gh_status_other", notes=f"status={status}")
    if '"jobPost"' not in body and "job_post" not in body:
        return PageParse("unknown", "gh_200_no_job", notes="200 without a job post")
    m = _GH_PUBLISHED.search(body)
    title_m = _GH_TITLE.search(body)
    title = None
    if title_m:
        try:
            title = json.loads(f'"{title_m.group(1)}"')
        except ValueError:
            title = title_m.group(1)
    if not m:
        return PageParse("open", "gh_200_no_date", title=title)
    raw = m.group(1)
    return PageParse(
        "open",
        "gh_published_at",
        published_raw=raw,
        published_at=parse_flexible_datetime(raw),
        title=title,
    )


def _ashby_app_data(body: str) -> dict | None:
    idx = body.find("window.__appData")
    if idx < 0:
        return None
    start = body.find("{", idx)
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(body, start)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_ashby_page(status: int, location: str | None, body: str | None) -> PageParse:
    """Ashby hosted job page (`jobs.ashbyhq.com/<org>/<uuid>[/application]`).

    The server-rendered `window.__appData` carries the posting:
    * `posting` object with `isListed: true` → open; `publishedDate` is the
      LAST publish date (spec.md §3 amendment 2026-10-07), kept as such.
    * `posting` object with `isListed: false` → unknown (unlisted, still
      reachable by link; not evidence of closure).
    * `posting: null` → unknown. The pilot found such empty shells for jobs
      our own collection saw open on the same day, so a shell is NOT a
      closure (contrary to the prior note).
    * 404/410 → closed.
    """
    if status in (404, 410):
        return PageParse("closed", "ashby_http_gone")
    if status in (301, 302, 303, 307, 308):
        return PageParse(
            "unknown", "ashby_redirect", notes=(_location_target(location) or "")[:200]
        )
    if status != 200 or not body:
        return PageParse("unknown", "ashby_status_other", notes=f"status={status}")
    app = _ashby_app_data(body)
    if app is None:
        return PageParse("unknown", "ashby_no_appdata")
    posting = app.get("posting")
    org = app.get("organization")
    if not isinstance(posting, dict):
        kind = "org_present" if isinstance(org, dict) else "org_null"
        return PageParse("unknown", "ashby_shell", notes=f"posting=null {kind}")
    raw = posting.get("publishedDate") if isinstance(posting.get("publishedDate"), str) else None
    if raw is None:
        jl = extract_job_posting(body)
        raw = jl.date_posted_raw if jl else None
    updated = posting.get("updatedAt") if isinstance(posting.get("updatedAt"), str) else None
    title = posting.get("title") if isinstance(posting.get("title"), str) else None
    listed = posting.get("isListed")
    state = "open" if listed is not False else "unknown"
    parser = "ashby_posting" if listed is not False else "ashby_unlisted"
    return PageParse(
        state,
        parser,
        published_raw=raw,
        published_at=parse_flexible_datetime(raw),
        updated_raw=updated,
        title=title,
    )


def parse_lever_page(status: int, location: str | None, body: str | None) -> PageParse:
    """Lever hosted job page (`jobs.lever.co/<org>/<uuid>[/apply]`).

    * 404 → closed (Lever's page says the posting "might have closed, or it
      has been removed").
    * 200 job page with JSON-LD `JobPosting` → open; `datePosted` →
      page_structured publish date.
    * 200 apply page → open (no date on it).
    """
    if status in (404, 410):
        return PageParse("closed", "lever_http_gone")
    if status in (301, 302, 303, 307, 308):
        return PageParse(
            "unknown", "lever_redirect", notes=(_location_target(location) or "")[:200]
        )
    if status != 200 or not body:
        return PageParse("unknown", "lever_status_other", notes=f"status={status}")
    jl = extract_job_posting(body)
    if jl is not None:
        return PageParse(
            "open",
            "lever_jsonld",
            published_raw=jl.date_posted_raw,
            published_at=jl.date_posted,
            title=jl.title,
        )
    if "posting-headline" in body or "template-btn-submit" in body or "application-form" in body:
        return PageParse("open", "lever_page_no_jsonld")
    return PageParse("unknown", "lever_200_unrecognised")


def parse_capture(ats: str, status: int, location: str | None, body: str | None) -> PageParse:
    if ats == "greenhouse":
        return parse_greenhouse_page(status, location, body)
    if ats == "ashby":
        return parse_ashby_page(status, location, body)
    if ats == "lever":
        return parse_lever_page(status, location, body)
    raise ValueError(f"unsupported ats {ats!r}")


ASHBY_FULL_PAGE_MIN_LENGTH = 6000


def infer_from_cdx(
    ats: str, host: str, page_kind: str, statuscode: str, length: int | None = None
) -> tuple[str, str]:
    """State implied by a CDX status code alone, as `(state, rule)`.

    Used ONLY for captures that were not fetched, and only for rules whose
    fetched-sample agreement is reported. An Ashby 200 is decided by the
    body: a server-rendered page with the posting is large (>= 6,000 bytes
    compressed in the CDX `length`), the empty shell is ~3 KB. Only the
    large form is inferred (open); a shell stays unknown.
    """
    if ats == "greenhouse":
        h = "jb" if host == "job-boards.greenhouse.io" else "b"
        if statuscode == "200" and h == "jb":
            return "open", "gh_jb_cdx_200"
        if statuscode == "302" and h == "jb":
            return "closed", "gh_jb_cdx_302"
        return "unknown", f"gh_{h}_cdx_{statuscode}"
    if ats == "lever":
        if statuscode == "200":
            return "open", f"lever_cdx_200_{page_kind}"
        if statuscode == "404":
            return "closed", "lever_cdx_404"
        return "unknown", f"lever_cdx_{statuscode}"
    if ats == "ashby":
        if statuscode == "404":
            return "closed", "ashby_cdx_404"
        if statuscode == "200" and length is not None:
            if length >= ASHBY_FULL_PAGE_MIN_LENGTH:
                return "open", "ashby_cdx_200_full"
            return "unknown", "ashby_cdx_200_small"
        return "unknown", f"ashby_cdx_{statuscode}"
    return "unknown", "unsupported"


# ---------------------------------------------------------------------------
# Capture selection
# ---------------------------------------------------------------------------


def select_captures(timestamps: Sequence[str], max_n: int = 8) -> list[tuple[str, int]]:
    """Pick up to `max_n` capture timestamps of ONE job URL, with a priority.

    Returns `(timestamp, rank)` pairs, rank 0 = most important: the earliest
    (0), the latest (1), the earliest capture at or after each calendar-month
    boundary (2), then evenly spread fill (3). Input order does not matter.
    """
    ts = sorted(set(timestamps))
    if not ts:
        return []
    chosen: dict[str, int] = {ts[0]: 0}
    chosen.setdefault(ts[-1], 1)
    seen_months: set[str] = set()
    for t in ts:
        month = t[:6]
        if month not in seen_months:
            seen_months.add(month)
            chosen.setdefault(t, 2)
    remaining = [t for t in ts if t not in chosen]
    if remaining:
        step = max(1, len(remaining) // max(1, max_n))
        for t in remaining[::step]:
            chosen.setdefault(t, 3)
    ordered = sorted(chosen.items(), key=lambda kv: (kv[1], kv[0]))
    if len(ordered) > max_n:
        # Keep earliest/latest, then thin the month boundaries evenly.
        head = [kv for kv in ordered if kv[1] < 2]
        rest = [kv for kv in ordered if kv[1] >= 2]
        k = max_n - len(head)
        if k > 0 and rest:
            idx = sorted({round(i * (len(rest) - 1) / max(1, k - 1)) for i in range(k)})
            rest = [rest[i] for i in idx][:k]
        else:
            rest = []
        ordered = head + rest
    return ordered


# ---------------------------------------------------------------------------
# HTTP client with disk cache
# ---------------------------------------------------------------------------


@dataclass
class RawResponse:
    url: str
    status: int | None
    location: str | None
    body: str | None
    fetched_at: str
    memento: bool = False
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and self.error is None


def _now_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class WaybackClient:
    """Polite Wayback client: allowlist, per-host token bucket, adaptive backoff.

    Redirects are NOT followed (the archived redirect target is data).
    Terminal responses are cached gzip-JSON under `cache_dir`, so a rerun
    costs no requests. 429 / 5xx / transport errors are retried with
    backoff and, when retries run out, returned as a gap (`error` set) and
    not cached.
    """

    cache_dir: Path
    cfg: Config | None = None
    max_retries: int = 4
    cdx_min_interval_s: float = 5.0
    sleep: Callable[[float], None] = time.sleep
    client: httpx.Client | None = None
    log: Callable[[str], None] = print
    _limiter: RateLimiter = field(init=False)
    _last_cdx: float = field(default=0.0, init=False)
    _extra_spacing: float = field(default=0.0, init=False)
    stats: dict[str, int] = field(default_factory=lambda: defaultdict(int), init=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.cfg is not None:
            self._limiter = RateLimiter.from_config(self.cfg, sleep=self.sleep)
        else:
            self._limiter = RateLimiter(default_rps=0.5, default_burst=2, sleep=self.sleep)
        if self.client is None:
            self.client = httpx.Client(
                headers={"User-Agent": USER_AGENT},
                timeout=httpx.Timeout(60.0, connect=20.0),
                follow_redirects=False,
            )

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json.gz"

    def cached(self, url: str) -> RawResponse | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return RawResponse(
            url=data["url"],
            status=data["status"],
            location=data.get("location"),
            body=data.get("body"),
            fetched_at=data["fetched_at"],
            memento=bool(data.get("memento")),
            from_cache=True,
        )

    def _store(self, resp: RawResponse) -> None:
        path = self._cache_path(resp.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(
                {
                    "url": resp.url,
                    "status": resp.status,
                    "location": resp.location,
                    "body": resp.body,
                    "fetched_at": resp.fetched_at,
                    "memento": resp.memento,
                },
                fh,
            )
        tmp.replace(path)

    def get(self, url: str, *, is_cdx: bool = False) -> RawResponse:
        hit = self.cached(url)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit
        check_allowed(url, ALLOWLIST)
        host = urlparse(url).hostname or ""
        last_error = "no attempt"
        for attempt in range(self.max_retries + 1):
            if is_cdx:
                wait = self.cdx_min_interval_s - (time.monotonic() - self._last_cdx)
                if wait > 0:
                    self.sleep(wait)
            if self._extra_spacing > 0:
                self.sleep(self._extra_spacing)
            self._limiter.acquire(host)
            if is_cdx:
                self._last_cdx = time.monotonic()
            self.stats["requests"] += 1
            try:
                r = self.client.get(url)  # type: ignore[union-attr]
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.stats["transport_errors"] += 1
                self.sleep(min(120.0, 5.0 * 2**attempt) * random.uniform(0.5, 1.0))
                continue
            archived = "memento-datetime" in r.headers  # the capture itself was a 5xx
            if r.status_code == 429 or (r.status_code >= 500 and not archived):
                self.stats[f"http_{r.status_code}"] += 1
                last_error = f"HTTP {r.status_code}"
                retry_after = r.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 0.0
                except ValueError:
                    delay = 0.0
                if r.status_code == 429:
                    self._extra_spacing = min(30.0, max(2.0, self._extra_spacing * 2 or 2.0))
                    delay = max(delay, 60.0 * (attempt + 1))
                else:
                    delay = max(delay, min(120.0, 5.0 * 2**attempt))
                self.log(f"  {last_error} on {url[:120]} -> sleep {delay:.0f}s")
                self.sleep(min(delay, 300.0))
                continue
            resp = RawResponse(
                url=url,
                status=r.status_code,
                location=r.headers.get("location"),
                body=r.text,
                fetched_at=_now_z(),
                memento="memento-datetime" in r.headers,
            )
            if self._extra_spacing > 0:
                self._extra_spacing = max(0.0, self._extra_spacing - 0.1)
            self._store(resp)
            return resp
        self.stats["gaps"] += 1
        return RawResponse(
            url=url, status=None, location=None, body=None, fetched_at=_now_z(), error=last_error
        )

    # -- CDX -----------------------------------------------------------------

    def cdx(
        self,
        prefix: str,
        from_: str = WINDOW_FROM,
        to: str = WINDOW_TO,
        page_limit: int = 25000,
        max_pages: int = 20,
    ) -> tuple[list[list[str]], str | None]:
        """All CDX rows under `prefix` (paginated via resumeKey). Returns (rows, error)."""
        rows: list[list[str]] = []
        resume: str | None = None
        for _ in range(max_pages):
            params = {
                "url": prefix,
                "matchType": "prefix",
                "output": "json",
                "from": from_,
                "to": to,
                "fl": CDX_FIELDS,
                "limit": str(page_limit),
                "showResumeKey": "true",
            }
            if resume:
                params["resumeKey"] = resume
            url = str(httpx.URL(CDX_URL, params=params))
            resp = self.get(url, is_cdx=True)
            if not resp.ok or resp.status != 200:
                return rows, resp.error or f"HTTP {resp.status}"
            try:
                data = json.loads(resp.body or "[]")
            except ValueError:
                return rows, "CDX body not JSON"
            if not isinstance(data, list) or len(data) <= 1:
                break
            body_rows = data[1:]
            resume = None
            if len(body_rows) >= 2 and body_rows[-2] == [] and len(body_rows[-1]) == 1:
                resume = body_rows[-1][0]
                body_rows = body_rows[:-2]
            elif body_rows and body_rows[-1] == []:
                body_rows = body_rows[:-1]
            rows.extend(r for r in body_rows if len(r) >= 7)
            if not resume:
                break
        return rows, None

    def fetch_capture(self, timestamp: str, original: str) -> RawResponse:
        """Raw `id_` capture; follows ONLY a Wayback nearest-timestamp hop for the same URL."""
        url = f"https://web.archive.org/web/{timestamp}id_/{original}"
        resp = self.get(url)
        for _ in range(2):
            if resp.status not in (301, 302, 303, 307, 308) or resp.memento:
                break
            target = _location_target(resp.location)
            if not target or _strip_scheme(target) != _strip_scheme(original):
                break
            # Wayback hop to a neighbouring timestamp of the same URL (not an archived redirect).
            nxt = urljoin("https://web.archive.org/", resp.location or "")
            resp = self.get(nxt)
        return resp


def _strip_scheme(url: str) -> str:
    return re.sub(r"^https?://", "", url).rstrip("/")


# ---------------------------------------------------------------------------
# Pilot DB
# ---------------------------------------------------------------------------

PILOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    company_id TEXT PRIMARY KEY, ats TEXT NOT NULL, tenant TEXT NOT NULL, why TEXT
);
CREATE TABLE IF NOT EXISTS cdx_queries (
    prefix TEXT PRIMARY KEY, company_id TEXT NOT NULL, queried_at TEXT NOT NULL,
    ok INTEGER NOT NULL, n_rows INTEGER NOT NULL, n_job_rows INTEGER NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL, ats TEXT NOT NULL, tenant TEXT NOT NULL,
    job_id TEXT NOT NULL, page_kind TEXT NOT NULL, host TEXT NOT NULL,
    url TEXT NOT NULL,            -- normalised job URL
    original TEXT NOT NULL, urlkey TEXT NOT NULL,
    capture_ts TEXT NOT NULL, capture_at TEXT NOT NULL,
    statuscode TEXT, digest TEXT, length INTEGER, mimetype TEXT,
    posting_id TEXT,              -- our posting, NULL when the job is not in rli.db
    UNIQUE (original, capture_ts)
);
CREATE INDEX IF NOT EXISTS idx_captures_job ON captures (company_id, job_id);
CREATE TABLE IF NOT EXISTS fetches (
    capture_id INTEGER PRIMARY KEY REFERENCES captures (id),
    fetched_at TEXT NOT NULL, http_status INTEGER, location TEXT, memento INTEGER,
    ok INTEGER NOT NULL, error TEXT, from_cache INTEGER NOT NULL, plan_reason TEXT
);
-- One row per capture: the pilot's observation table.
CREATE TABLE IF NOT EXISTS page_obs (
    capture_id INTEGER PRIMARY KEY REFERENCES captures (id),
    posting_id TEXT, company_id TEXT NOT NULL, ats TEXT NOT NULL, job_id TEXT NOT NULL,
    url TEXT NOT NULL, capture_ts TEXT NOT NULL, capture_at TEXT NOT NULL,
    http_status TEXT, state TEXT NOT NULL CHECK (state IN ('open', 'closed', 'unknown')),
    evidence TEXT NOT NULL CHECK (evidence IN ('fetched', 'cdx_inferred', 'gap')),
    published_raw TEXT, published_at TEXT, updated_raw TEXT, title TEXT,
    parser TEXT NOT NULL, notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_page_obs_posting ON page_obs (posting_id, capture_at);
"""


def open_pilot_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.executescript(PILOT_SCHEMA)
    return conn


def open_main_db_readonly(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _ts_to_iso(ts: str) -> str:
    ts = (ts + "00000000000000")[:14]
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}.000000Z"


def posting_index(main: sqlite3.Connection, company_id: str) -> dict[str, str]:
    """`job_id (lower) -> posting_id` for one company; real postings beat `archive:` rows."""
    out: dict[str, str] = {}
    rows = main.execute(
        "SELECT posting_id, ats_job_id FROM postings "
        "WHERE company_id = ? AND ats_job_id IS NOT NULL",
        (company_id,),
    ).fetchall()
    for row in sorted(
        rows, key=lambda r: (r["posting_id"].startswith("archive:"), r["posting_id"])
    ):
        out.setdefault(str(row["ats_job_id"]).lower(), row["posting_id"])
    return out


def ingest_cdx(
    pilot: sqlite3.Connection,
    main: sqlite3.Connection,
    client: WaybackClient,
    targets: Iterable[Target] = TARGETS,
    *,
    log: Callable[[str], None] = print,
) -> None:
    for t in targets:
        pilot.execute(
            "INSERT OR REPLACE INTO targets VALUES (?, ?, ?, ?)",
            (t.company_id, t.ats, t.tenant, t.why),
        )
        index = posting_index(main, t.company_id)
        for prefix in cdx_prefixes(t.ats, t.tenant):
            done = pilot.execute(
                "SELECT ok FROM cdx_queries WHERE prefix = ?", (prefix,)
            ).fetchone()
            if done is not None and done["ok"]:
                continue
            rows, error = client.cdx(prefix)
            n_job = 0
            for r in rows:
                urlkey, ts, original, mimetype, statuscode, digest, length = r[:7]
                ju = normalize_job_url(original, t.ats, t.tenant)
                if ju is None:
                    continue
                n_job += 1
                pilot.execute(
                    """INSERT OR IGNORE INTO captures (company_id, ats, tenant, job_id,
                    page_kind, host, url, original, urlkey, capture_ts, capture_at, statuscode,
                    digest, length, mimetype, posting_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        t.company_id,
                        t.ats,
                        t.tenant,
                        ju.job_id,
                        ju.page_kind,
                        ju.host,
                        ju.canonical,
                        original,
                        urlkey,
                        ts,
                        _ts_to_iso(ts),
                        statuscode,
                        digest,
                        int(length) if str(length).isdigit() else None,
                        mimetype,
                        index.get(ju.job_id.lower()),
                    ),
                )
            pilot.execute(
                "INSERT OR REPLACE INTO cdx_queries VALUES (?, ?, ?, ?, ?, ?, ?)",
                (prefix, t.company_id, _now_z(), int(error is None), len(rows), n_job, error),
            )
            pilot.commit()
            log(f"cdx {prefix}: rows={len(rows)} job_rows={n_job} error={error}")


# ---------------------------------------------------------------------------
# Fetch planning and execution
# ---------------------------------------------------------------------------


def plan_fetches(
    pilot: sqlite3.Connection,
    *,
    cap: int = 3000,
    per_url: int = 8,
    validation_per_class: int = 8,
    seed: int = 20261008,
) -> list[tuple[int, str]]:
    """Ordered `(capture_id, reason)` list, at most `cap` long.

    Priority: (1) a validation sample of every inferable CDX status class per
    company, half of it from the own-collection era (>= 2026-09-07) so it
    can be checked against our own board captures; (2) the earliest 200 job
    page of every mapped posting (publish date); (3) Ashby captures of
    mapped postings by `select_captures` rank (the body decides the state);
    (4) the earliest 200 of unmapped jobs.
    """
    rng = random.Random(seed)
    rows = pilot.execute(
        "SELECT id, company_id, ats, host, page_kind, job_id, capture_ts, statuscode, posting_id "
        "FROM captures ORDER BY id"
    ).fetchall()
    plan: list[tuple[int, str]] = []
    seen: set[int] = set()

    def add(cid: int, reason: str) -> None:
        if cid not in seen:
            seen.add(cid)
            plan.append((cid, reason))

    # (1) validation sample
    classes: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r["ats"] == "ashby" and r["statuscode"] == "200":
            continue  # always body-decided
        key = (r["company_id"], r["host"], r["page_kind"], r["statuscode"])
        classes[key].append(r)
    for key in sorted(classes):
        group = classes[key]
        own_era = [r for r in group if r["capture_ts"] >= "20260907" and r["posting_id"]]
        other = [r for r in group if r not in own_era]
        half = validation_per_class // 2
        pick = rng.sample(own_era, min(half, len(own_era)))
        pick += rng.sample(other, min(validation_per_class - len(pick), len(other)))
        for r in pick:
            add(r["id"], "validation")

    by_job: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        by_job[(r["company_id"], r["job_id"])].append(r)

    # (2) earliest 200 job page of mapped GH/Lever postings
    mapped = [k for k, v in by_job.items() if v[0]["posting_id"]]
    unmapped = [k for k, v in by_job.items() if not v[0]["posting_id"]]
    for key in sorted(mapped):
        group = by_job[key]
        if group[0]["ats"] == "ashby":
            continue
        ok = sorted(
            (
                r
                for r in group
                if r["statuscode"] == "200"
                and r["page_kind"] == "job"
                and r["host"] != "boards.greenhouse.io"
            ),
            key=lambda r: r["capture_ts"],
        )
        if ok:
            add(ok[0]["id"], "publish_date")

    # (3) Ashby mapped postings, interleaved by rank
    ranked: list[tuple[int, str, int]] = []
    for key in sorted(mapped):
        group = by_job[key]
        if group[0]["ats"] != "ashby":
            continue
        ok = [r for r in group if r["statuscode"] == "200"]
        by_ts = {r["capture_ts"]: r for r in sorted(ok, key=lambda r: r["page_kind"] != "job")}
        for ts, rank in select_captures(list(by_ts), per_url):
            ranked.append((rank, ts, by_ts[ts]["id"]))
    for rank, _ts, cid in sorted(ranked):
        add(cid, f"ashby_rank{rank}")

    # (4) unmapped jobs: earliest 200
    for key in sorted(unmapped):
        ok = sorted(
            (r for r in by_job[key] if r["statuscode"] == "200"), key=lambda r: r["capture_ts"]
        )
        if ok:
            add(ok[0]["id"], "unmapped_earliest")

    return plan[:cap]


def _obs_row(cap: sqlite3.Row, parse: PageParse, evidence: str, http_status: str | None) -> tuple:
    return (
        cap["id"],
        cap["posting_id"],
        cap["company_id"],
        cap["ats"],
        cap["job_id"],
        cap["url"],
        cap["capture_ts"],
        cap["capture_at"],
        http_status,
        parse.state,
        evidence,
        parse.published_raw,
        parse.published_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if parse.published_at else None,
        parse.updated_raw,
        parse.title,
        parse.parser,
        parse.notes,
    )


_INSERT_OBS = """INSERT OR REPLACE INTO page_obs (capture_id, posting_id, company_id, ats, job_id,
    url, capture_ts, capture_at, http_status, state, evidence, published_raw, published_at,
    updated_raw, title, parser, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def run_fetches(
    pilot: sqlite3.Connection,
    client: WaybackClient,
    plan: Sequence[tuple[int, str]],
    *,
    log: Callable[[str], None] = print,
    max_consecutive_gaps: int = 25,
) -> dict[str, int]:
    """Fetch every planned capture not fetched yet; store fetch + observation."""
    done = {r[0] for r in pilot.execute("SELECT capture_id FROM fetches WHERE ok = 1")}
    counts: dict[str, int] = defaultdict(int)
    consecutive_gaps = 0
    started = time.monotonic()
    for i, (cid, reason) in enumerate(plan):
        if cid in done:
            counts["already"] += 1
            continue
        cap = pilot.execute("SELECT * FROM captures WHERE id = ?", (cid,)).fetchone()
        resp = client.fetch_capture(cap["capture_ts"], cap["original"])
        if not resp.ok:
            counts["gap"] += 1
            consecutive_gaps += 1
            pilot.execute(
                "INSERT OR REPLACE INTO fetches VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    cid,
                    resp.fetched_at,
                    resp.status,
                    resp.location,
                    int(resp.memento),
                    resp.error,
                    int(resp.from_cache),
                    reason,
                ),
            )
            parse = PageParse("unknown", "fetch_gap", notes=resp.error or "")
            pilot.execute(_INSERT_OBS, _obs_row(cap, parse, "gap", cap["statuscode"]))
            if consecutive_gaps >= max_consecutive_gaps:
                log(f"stopping: {consecutive_gaps} consecutive gaps (archive.org unhappy)")
                pilot.commit()
                break
            continue
        consecutive_gaps = 0
        counts["fetched_cache" if resp.from_cache else "fetched_net"] += 1
        parse = parse_capture(cap["ats"], int(resp.status or 0), resp.location, resp.body)
        if not resp.memento and resp.status == 404:
            # Wayback's own "not archived" 404, not an archived 404.
            parse = PageParse("unknown", "wayback_not_archived")
        pilot.execute(
            "INSERT OR REPLACE INTO fetches VALUES (?, ?, ?, ?, ?, 1, NULL, ?, ?)",
            (
                cid,
                resp.fetched_at,
                resp.status,
                resp.location,
                int(resp.memento),
                int(resp.from_cache),
                reason,
            ),
        )
        pilot.execute(_INSERT_OBS, _obs_row(cap, parse, "fetched", str(resp.status)))
        if i % 25 == 0:
            pilot.commit()
            el = time.monotonic() - started
            log(f"[{i + 1}/{len(plan)}] {dict(counts)} stats={dict(client.stats)} {el:.0f}s")
    pilot.commit()
    return dict(counts)


def infer_unfetched(pilot: sqlite3.Connection) -> int:
    """Give every capture without a fetched observation a CDX-inferred one."""
    rows = pilot.execute(
        """SELECT c.* FROM captures c LEFT JOIN page_obs o ON o.capture_id = c.id
        WHERE o.capture_id IS NULL OR o.evidence IN ('cdx_inferred', 'gap')"""
    ).fetchall()
    for cap in rows:
        state, rule = infer_from_cdx(
            cap["ats"], cap["host"], cap["page_kind"], cap["statuscode"], cap["length"]
        )
        pilot.execute(
            _INSERT_OBS, _obs_row(cap, PageParse(state, rule), "cdx_inferred", cap["statuscode"])
        )
    pilot.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Optional: Common Crawl index counts (index only; feasibility check)
# ---------------------------------------------------------------------------

CC_INDEX = "https://index.commoncrawl.org"
CC_CRAWLS = (
    "CC-MAIN-2025-43",
    "CC-MAIN-2025-47",
    "CC-MAIN-2025-51",
    "CC-MAIN-2026-04",
    "CC-MAIN-2026-08",
    "CC-MAIN-2026-12",
    "CC-MAIN-2026-17",
    "CC-MAIN-2026-21",
    "CC-MAIN-2026-25",
    "CC-MAIN-2026-30",
    "CC-MAIN-2026-34",
    "CC-MAIN-2026-39",
)

CC_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_rows (
    crawl TEXT NOT NULL, company_id TEXT NOT NULL, ats TEXT NOT NULL, job_id TEXT NOT NULL,
    page_kind TEXT NOT NULL, url TEXT NOT NULL, capture_ts TEXT NOT NULL, status TEXT,
    filename TEXT, offset INTEGER, length INTEGER, posting_id TEXT,
    UNIQUE (crawl, url, capture_ts)
);
CREATE TABLE IF NOT EXISTS cc_queries (
    crawl TEXT NOT NULL, prefix TEXT NOT NULL, ok INTEGER NOT NULL, n_rows INTEGER,
    error TEXT, PRIMARY KEY (crawl, prefix)
);
"""


def _cc_get(
    client: httpx.Client, url: str, sleep: Callable[[float], None]
) -> tuple[str | None, str | None]:
    error: str | None = "no attempt"
    for attempt in range(4):
        sleep(3.0)
        try:
            r = client.get(url)
        except httpx.HTTPError as exc:
            error = type(exc).__name__
            sleep(10.0 * (attempt + 1))
            continue
        if r.status_code == 404:  # "No Captures found"
            return "", None
        if r.status_code in (429, 500, 502, 503, 504):
            error = f"HTTP {r.status_code}"
            sleep(30.0 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.text, None
    return None, error


def ingest_cc_index(
    pilot: sqlite3.Connection,
    main: sqlite3.Connection,
    cache_dir: Path,
    targets: Iterable[Target] = TARGETS,
    *,
    crawls: Sequence[str] = CC_CRAWLS,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    """Count Common Crawl index rows for the same job-page prefixes (first page per query)."""
    pilot.executescript(CC_SCHEMA)
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120.0)
    for t in targets:
        index = posting_index(main, t.company_id)
        for prefix in cdx_prefixes(t.ats, t.tenant):
            for crawl in crawls:
                done = pilot.execute(
                    "SELECT ok FROM cc_queries WHERE crawl = ? AND prefix = ?", (crawl, prefix)
                ).fetchone()
                if done is not None and done["ok"]:
                    continue
                params = {"url": prefix + "*", "output": "json"}
                url = str(httpx.URL(f"{CC_INDEX}/{crawl}-index", params=params))
                path = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".ndjson.gz")
                if path.exists():
                    with gzip.open(path, "rt", encoding="utf-8") as fh:
                        body, error = fh.read(), None
                else:
                    body, error = _cc_get(client, url, sleep)
                    if body is not None and error is None:
                        with gzip.open(path, "wt", encoding="utf-8") as fh:
                            fh.write(body)
                n = 0
                for line in (body or "").splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ju = normalize_job_url(rec.get("url", ""), t.ats, t.tenant)
                    if ju is None:
                        continue
                    n += 1
                    pilot.execute(
                        "INSERT OR IGNORE INTO cc_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            crawl,
                            t.company_id,
                            t.ats,
                            ju.job_id,
                            ju.page_kind,
                            ju.canonical,
                            rec.get("timestamp", ""),
                            rec.get("status"),
                            rec.get("filename"),
                            int(rec.get("offset", 0) or 0),
                            int(rec.get("length", 0) or 0),
                            index.get(ju.job_id.lower()),
                        ),
                    )
                pilot.execute(
                    "INSERT OR REPLACE INTO cc_queries VALUES (?, ?, ?, ?, ?)",
                    (crawl, prefix, int(error is None), n, error),
                )
                pilot.commit()
                log(f"cc {crawl} {prefix}: job_rows={n} error={error}")
