"""Discover and live-verify new Greenhouse / Ashby / Lever job boards.

Grows `scripts/targets.csv` without using a web search engine. The pipeline
is three stages, all of them resumable via a single durable cache
(`scripts/targets_candidates_discovered.csv`):

1. **Discover** candidate ATS tenant slugs from the Wayback Machine CDX
   Server API (`web.archive.org/cdx/search/cdx`), with Common Crawl's index
   as a fallback when CDX comes back thin for an ATS. Nothing here proves a
   board is alive -- an archived URL only proves it *once* existed.
2. **Verify live** each candidate against that ATS's public postings API and
   count currently-open jobs. Only boards in the 8-400 open-job band are
   kept (the band the existing targets.csv is curated to).
3. **Resolve identity** -- company name and a real `website_domain` -- from
   one actual job-posting HTML page per board, via
   `rli.resolvers.jsonld.extract_job_posting` (JSON-LD `hiringOrganization`)
   with a link-mining fallback for boards that publish no structured data
   (Greenhouse's rendered pages, notably, carry none). A kept board whose
   domain cannot be resolved is parked in `scripts/targets_needs_domain.csv`
   rather than guessed at.

Politeness, mirroring `rli/archive/cdx.py` and `scripts/wayback_spotcheck.py`:
CDX/Common Crawl are called at 0.5 req/s, every other host at 1 req/s, with a
custom User-Agent, a 10s timeout, and exponential backoff on 429/5xx/timeout
(honouring `Retry-After` when present). A throttled request is logged and
retried -- it is treated as a coverage gap, never as evidence that a board is
dead.

Usage::

    python scripts/discover_boards.py --ats ashby --max-new 50
    python scripts/discover_boards.py --ats all --max-new 230
    python scripts/discover_boards.py --ats lever --discover-only
    python scripts/discover_boards.py --ats all --stats
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import json
import random
import re
import signal
import sys
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rli.resolvers.common import normalize_domain  # noqa: E402
from rli.resolvers.jsonld import extract_job_posting  # noqa: E402

DEFAULT_TARGETS = PROJECT_ROOT / "scripts" / "targets.csv"
CANDIDATE_CACHE = PROJECT_ROOT / "scripts" / "targets_candidates_discovered.csv"
NEEDS_DOMAIN = PROJECT_ROOT / "scripts" / "targets_needs_domain.csv"

USER_AGENT = "job-liveliness-investigator/1.0 (contact: uzair.khan@progbid.com)"
REQUEST_TIMEOUT_S = 10.0
HTML_TIMEOUT_S = 20.0
CDX_TIMEOUT_S = 180.0  # CDX prefix scans are genuinely slow; not a per-host politeness knob

ARCHIVE_INTERVAL_S = 2.0  # 0.5 req/s for web.archive.org / index.commoncrawl.org
HOST_INTERVAL_S = 1.0  # 1 req/s for every other host

MIN_JOBS = 8
MAX_JOBS = 400

TARGETS_FIELDS = ["company_name", "website_domain", "ats", "tenant", "open_job_count", "checked_at"]
CACHE_FIELDS = [
    "ats",
    "tenant",
    "source",
    "discovered_at",
    "status",
    "open_job_count",
    "company_name",
    "website_domain",
    "checked_at",
    "note",
]
NEEDS_DOMAIN_FIELDS = ["company_name", "ats", "tenant", "open_job_count", "checked_at"]

ALL_ATS = ("greenhouse", "ashby", "lever")

# Wayback CDX prefix patterns, one per public board host per ATS.
CDX_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "ashby": ("jobs.ashbyhq.com",),
    "lever": ("jobs.lever.co",),
}

# A single `url=<host>/*` scan is capped at `limit` rows and returned in
# urlkey (i.e. alphabetical) order, so one popular tenant with thousands of
# archived posting URLs can eat the entire budget. Sharding the scan by the
# first character of the tenant slug spreads the budget across the alphabet;
# the shard order is shuffled with a fixed seed so that a run stopped early
# (--discover-target) is not biased to slugs starting with "a".
SHARD_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"
SHARD_ORDER = random.Random(20260913).sample(list(SHARD_CHARS), len(SHARD_CHARS))

CDX_TEMPLATE = (
    "https://web.archive.org/cdx/search/cdx?url={pattern}&collapse=urlkey&fl=original"
    "&filter=statuscode:200&limit={limit}&from=2025"
)
COMMONCRAWL_COLLINFO = "https://index.commoncrawl.org/collinfo.json"

# Path segments that are ATS infrastructure, not tenant slugs.
RESERVED_SLUGS = {
    "embed",
    "api",
    "apis",
    "assets",
    "static",
    "public",
    "images",
    "img",
    "css",
    "js",
    "fonts",
    "favicon.ico",
    "robots.txt",
    "sitemap.xml",
    "sitemap",
    "login",
    "signin",
    "sign-in",
    "signup",
    "sign-up",
    "logout",
    "account",
    "accounts",
    "admin",
    "search",
    "jobs",
    "job",
    "jobseeker",
    "job-seeker-support",
    "applications",
    "application",
    "privacy",
    "terms",
    "legal",
    "cookies",
    "support",
    "help",
    "about",
    "blog",
    "pricing",
    "customers",
    "index.html",
    "home",
    "error",
    "404",
    "healthz",
    "status",
    "posting-api",
    "recruiting",
    "companies",
    "company",
    "boards",
    "board",
    "departments",
    "offices",
    "people",
    "v0",
    "v1",
}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")

# Registrable domains that appear on job pages but never *are* the employer.
DOMAIN_BLOCKLIST = {
    # the ATSes themselves + their CDNs
    "greenhouse.io",
    "greenhouse.com",
    "lever.co",
    "ashbyhq.com",
    "ashbyprd.com",
    "greenhouse-mail.io",
    "workday.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "breezy.hr",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
    "paylocity.com",
    "rippling.com",
    "gem.com",
    # social / aggregators
    "linkedin.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "fb.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "threads.net",
    "bsky.app",
    "mastodon.social",
    "github.com",
    "gitlab.io",
    "glassdoor.com",
    "indeed.com",
    "builtin.com",
    "wellfound.com",
    "angel.co",
    "crunchbase.com",
    "medium.com",
    "substack.com",
    "redditmedia.com",
    "vimeo.com",
    "spotify.com",
    "soundcloud.com",
    "wikipedia.org",
    "wikimedia.org",
    # generic tooling / infra / trackers
    "google.com",
    "googleapis.com",
    "google-analytics.com",
    "googletagmanager.com",
    "gstatic.com",
    "goo.gl",
    "forms.gle",
    "cloudflare.com",
    "cloudfront.net",
    "amazonaws.com",
    "akamaized.net",
    "fastly.net",
    "jsdelivr.net",
    "unpkg.com",
    "typeform.com",
    "calendly.com",
    "docsend.com",
    "notion.site",
    "notion.so",
    "airtableusercontent.com",
    "slack.com",
    "zoom.us",
    "bit.ly",
    "tinyurl.com",
    "w3.org",
    "schema.org",
    "mozilla.org",
    "adobe.com",
    "apple.com",
    "microsoft.com",
    "gmail.com",
    "outlook.com",
    "sentry.io",
    "segment.com",
    "intercom.com",
    "hubspot.com",
    "mailchimp.com",
    "transcend.io",
    "transcend-cdn.com",
    "onetrust.com",
    "cookielaw.org",
    "chromevox.com",
    "licdn.com",
    "ctfassets.net",
    "contentful.com",
    "cloudinary.com",
    "imgix.net",
    "sanity.io",
    "prismic.io",
    "datocms-assets.com",
    "algolia.net",
    "hotjar.com",
    "mixpanel.com",
    "amplitude.com",
    "fullstory.com",
    "gravatar.com",
    "cdninstagram.com",
    "vercel.app",
    "netlify.app",
    "herokuapp.com",
    "pages.dev",
    "workers.dev",
    "webflow.io",
    "wixsite.com",
    "squarespace.com",
    "hsforms.com",
    "hubspotusercontent-na1.net",
    "recaptcha.net",
    "jquery.com",
    "bootstrapcdn.com",
    "fontawesome.com",
    "checkr.com",
    "service-now.com",
    "servicenow.com",
    "adp.com",
    "trinet.com",
    "justworks.com",
    "insperity.com",
    "sequoia.com",
    "lyrahealth.com",
    "modernhealth.com",
    "spring.health",
    "headspace.com",
    "talkspace.com",
    "onemedical.com",
    "carrotfertility.com",
    "get-carrot.com",
    "maven.com",
    "wellhub.com",
    "gympass.com",
    "guideline.com",
    "betterment.com",
    "fidelity.com",
    "empower.com",
    "principal.com",
    "metlife.com",
    "unum.com",
    "aetna.com",
    "cigna.com",
    "uhc.com",
    "anthem.com",
    "kaiserpermanente.org",
    "deltadental.com",
    "healthequity.com",
    "benefitfocus.com",
    "moka.care",
    "alan.com",
    "greenhouse-mail.com",
    "greenhouse.zendesk.com",
    "zendesk.com",
    "docusign.net",
    "eeoc.eeoc.gov",
    # compliance boilerplate that shows up in every EEO footer
    "eeoc.gov",
    "dol.gov",
    "uscis.gov",
    "ada.gov",
    "justice.gov",
    "hhs.gov",
    "sec.gov",
    "usa.gov",
    "ftc.gov",
    "irs.gov",
    "nlrb.gov",
    "ca.gov",
    "ny.gov",
    "gov.uk",
    "europa.eu",
    "finra.org",
    "sec.org",
}

# Public suffixes that need three labels to reach a registrable domain.
MULTI_LABEL_SUFFIXES = {
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "me.uk",
    "com.au",
    "net.au",
    "org.au",
    "co.nz",
    "co.za",
    "co.jp",
    "co.kr",
    "co.in",
    "co.il",
    "co.id",
    "co.th",
    "com.br",
    "com.mx",
    "com.ar",
    "com.sg",
    "com.tr",
    "com.cn",
    "com.hk",
    "com.tw",
    "com.my",
    "com.ph",
    "com.pl",
    "com.ua",
    "com.co",
    "com.pe",
    "com.vn",
    "or.jp",
    "ne.jp",
    "co.ke",
    "co.ug",
}


# --------------------------------------------------------------------------
# HTTP plumbing: per-host rate limiting + backoff. Never raises.
# --------------------------------------------------------------------------


@dataclass
class Stats:
    """Counters for the run, surfaced in the coverage-audit write-up."""

    requests: int = 0
    timeouts: int = 0
    throttled_429: int = 0
    server_5xx: int = 0
    transport_errors: int = 0
    give_ups: int = 0
    cdx_pages: int = 0
    notes: Counter = field(default_factory=Counter)


class RateLimiter:
    """Minimum spacing between requests, tracked independently per host."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def wait(self, key: str, interval: float) -> None:
        last = self._last.get(key)
        if last is not None:
            delay = interval - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
        self._last[key] = time.monotonic()


class Fetcher:
    """`httpx.Client` wrapper: rate-limited, retrying, and non-raising.

    Returns `None` when every attempt failed. A `None` from a *verification*
    call must be recorded as "unknown / coverage gap", not as "board is
    dead" -- the caller is responsible for that distinction.
    """

    RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}

    def __init__(self, stats: Stats, *, attempts: int = 4, verbose: bool = True) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT_S,
        )
        self.limiter = RateLimiter()
        self.stats = stats
        self.attempts = attempts
        self.verbose = verbose

    def close(self) -> None:
        self.client.close()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"    ! {msg}", flush=True)

    def get(
        self,
        url: str,
        *,
        host_key: str | None = None,
        interval: float = HOST_INTERVAL_S,
        timeout: float = REQUEST_TIMEOUT_S,
        attempts: int | None = None,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> httpx.Response | None:
        key = host_key or (urllib.parse.urlsplit(url).hostname or url)
        backoff = 2.0
        tries = attempts if attempts is not None else self.attempts
        for attempt in range(tries):
            self.limiter.wait(key, interval)
            self.stats.requests += 1
            try:
                resp = self.client.get(url, timeout=timeout)
            except httpx.TimeoutException:
                self.stats.timeouts += 1
                self._log(f"timeout ({attempt + 1}/{tries}) {url}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            except httpx.HTTPError as exc:
                self.stats.transport_errors += 1
                self._log(f"transport error ({attempt + 1}/{tries}) {url}: {exc!r}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            if resp.status_code in ok_statuses:
                return resp
            if resp.status_code in self.RETRY_STATUSES:
                if resp.status_code == 429:
                    self.stats.throttled_429 += 1
                elif resp.status_code >= 500:
                    self.stats.server_5xx += 1
                retry_after = resp.headers.get("Retry-After", "")
                wait_s = backoff
                if retry_after.strip().isdigit():
                    wait_s = max(backoff, min(float(retry_after.strip()), 120.0))
                self._log(
                    f"HTTP {resp.status_code} ({attempt + 1}/{tries}) {url} "
                    f"-> backing off {wait_s:.0f}s"
                )
                time.sleep(wait_s)
                backoff = min(backoff * 2, 60.0)
                continue
            # Terminal status (404, 403, 410, ...) -- a real answer, hand it back.
            return resp

        self.stats.give_ups += 1
        return None


# --------------------------------------------------------------------------
# CSV state
# --------------------------------------------------------------------------


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def load_existing_targets(path: Path) -> tuple[set[tuple[str, str]], int]:
    rows = read_csv_rows(path)
    return {(r["ats"], r["tenant"]) for r in rows}, len(rows)


class CandidateCache:
    """Durable candidate + verification-outcome cache (append/rewrite CSV).

    Rows with `status == "shard_marker"` are bookkeeping, not candidates:
    they record that one CDX/Common Crawl shard has already been scanned, so
    a rerun does not re-query it even if it yielded nothing new.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[dict[str, str]] = read_csv_rows(path)
        self.by_key: dict[tuple[str, str], dict[str, str]] = {}
        self.sources_done: set[str] = set()
        for row in self.rows:
            if row.get("status") == "shard_marker":
                self.sources_done.add(row.get("source", ""))
                continue
            self.by_key[(row.get("ats", ""), row.get("tenant", ""))] = row
        self._dirty = False

    def candidates(self, ats: str) -> list[dict[str, str]]:
        return [r for r in self.rows if r.get("ats") == ats and r.get("status") != "shard_marker"]

    def discovered_count(self, ats: str) -> int:
        return len(self.candidates(ats))

    def add_candidate(self, ats: str, tenant: str, source: str) -> bool:
        key = (ats, tenant)
        if key in self.by_key:
            return False
        row = {
            "ats": ats,
            "tenant": tenant,
            "source": source,
            "discovered_at": datetime.now(UTC).isoformat(),
            "status": "new",
            "open_job_count": "",
            "company_name": "",
            "website_domain": "",
            "checked_at": "",
            "note": "",
        }
        self.rows.append(row)
        self.by_key[key] = row
        self._dirty = True
        return True

    def mark_shard_done(self, ats: str, source: str) -> None:
        if source in self.sources_done:
            return
        self.rows.append(
            {
                "ats": ats,
                "tenant": "",
                "source": source,
                "discovered_at": datetime.now(UTC).isoformat(),
                "status": "shard_marker",
                "open_job_count": "",
                "company_name": "",
                "website_domain": "",
                "checked_at": "",
                "note": "",
            }
        )
        self.sources_done.add(source)
        self._dirty = True

    def update(self, ats: str, tenant: str, **fields: str) -> None:
        row = self.by_key.get((ats, tenant))
        if row is None:
            return
        for k, v in fields.items():
            row[k] = "" if v is None else str(v)
        self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True

    def flush(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CACHE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in self.rows:
                writer.writerow({k: row.get(k, "") for k in CACHE_FIELDS})
        tmp.replace(self.path)
        self._dirty = False


def append_row(path: Path, fieldnames: list[str], row: dict[str, str]) -> None:
    """Append one row, writing the header first if the file is new/empty.

    Also repairs a missing trailing newline on an existing file so an append
    can never corrupt the last pre-existing row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    if not new_file:
        with path.open("rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                with path.open("a", encoding="utf-8") as af:
                    af.write("\n")
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
        f.flush()


def rewrite_needs_domain(cache: CandidateCache) -> int:
    """Regenerate targets_needs_domain.csv from the cache (idempotent).

    Rows are appended as they are found so an interrupted run still records
    them, but the file is regenerated at the end of every run so that a board
    later rescued by `--retry-no-domain` disappears from it instead of
    lingering as a stale duplicate.
    """
    rows = [r for r in cache.rows if r.get("status") == "reject_no_domain"]
    rows.sort(key=lambda r: (r.get("ats", ""), r.get("tenant", "")))
    NEEDS_DOMAIN.parent.mkdir(parents=True, exist_ok=True)
    with NEEDS_DOMAIN.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=NEEDS_DOMAIN_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "company_name": row.get("company_name", ""),
                    "ats": row.get("ats", ""),
                    "tenant": row.get("tenant", ""),
                    "open_job_count": row.get("open_job_count", ""),
                    "checked_at": row.get("checked_at", ""),
                }
            )
    return len(rows)


# --------------------------------------------------------------------------
# Stage 1 -- discovery
# --------------------------------------------------------------------------


def tenant_from_url(url: str, host: str) -> str | None:
    """First path segment of `url` under `host`, if it looks like a tenant slug."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower()
    if hostname != host and hostname != f"www.{host}":
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    slug = urllib.parse.unquote(segments[0]).strip().lower()
    if slug in RESERVED_SLUGS or not SLUG_RE.match(slug):
        return None
    if "." in slug and slug.rsplit(".", 1)[-1] in {
        "html",
        "htm",
        "php",
        "xml",
        "json",
        "js",
        "css",
        "png",
        "jpg",
        "svg",
        "ico",
        "txt",
        "pdf",
    }:
        return None
    return slug


def cdx_shard(fetcher: Fetcher, host: str, shard: str, limit: int) -> list[str] | None:
    """One CDX prefix scan. Returns raw captured URLs, or None on failure."""
    pattern = urllib.parse.quote(f"{host}/{shard}*", safe="")
    url = CDX_TEMPLATE.format(pattern=pattern, limit=limit)
    resp = fetcher.get(
        url,
        host_key="web.archive.org",
        interval=ARCHIVE_INTERVAL_S,
        timeout=CDX_TIMEOUT_S,
        attempts=3,
    )
    if resp is None or resp.status_code != 200:
        return None
    fetcher.stats.cdx_pages += 1
    return [line.strip() for line in resp.text.splitlines() if line.strip()]


def commoncrawl_index_url(fetcher: Fetcher) -> str | None:
    resp = fetcher.get(
        COMMONCRAWL_COLLINFO,
        host_key="index.commoncrawl.org",
        interval=ARCHIVE_INTERVAL_S,
        timeout=60.0,
        attempts=2,
    )
    if resp is None or resp.status_code != 200:
        return None
    try:
        collections = resp.json()
    except ValueError:
        return None
    if not isinstance(collections, list) or not collections:
        return None
    latest = collections[0]
    api = latest.get("cdx-api")
    if isinstance(api, str) and api.startswith("http"):
        return api
    crawl_id = latest.get("id")
    if isinstance(crawl_id, str):
        return f"https://index.commoncrawl.org/{crawl_id}-index"
    return None


def commoncrawl_shard(
    fetcher: Fetcher, index_url: str, host: str, shard: str, limit: int
) -> list[str] | None:
    query = urllib.parse.urlencode(
        {"url": f"{host}/{shard}*", "output": "json", "limit": str(limit)}
    )
    resp = fetcher.get(
        f"{index_url}?{query}",
        host_key="index.commoncrawl.org",
        interval=ARCHIVE_INTERVAL_S,
        timeout=120.0,
        attempts=2,
    )
    if resp is None or resp.status_code != 200:
        return None
    urls: list[str] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        candidate = record.get("url")
        if isinstance(candidate, str):
            urls.append(candidate)
    return urls


def discover(
    fetcher: Fetcher,
    cache: CandidateCache,
    ats: str,
    *,
    target: int,
    limit: int,
    max_shards: int,
    cc_threshold: int,
    rediscover: bool,
) -> None:
    """Fill the cache with candidate tenants for `ats` (CDX, then Common Crawl)."""
    pending = sum(1 for r in cache.candidates(ats) if r.get("status") == "new")
    if pending >= target and not rediscover:
        print(f"[{ats}] discovery: {pending} unverified candidates already cached; skipping CDX")
        return

    hosts = CDX_HOSTS[ats]
    shards_run = 0
    for shard in SHARD_ORDER:
        for host in hosts:
            source = f"cdx:{host}:{shard}"
            if source in cache.sources_done and not rediscover:
                continue
            if shards_run >= max_shards:
                print(f"[{ats}] discovery: hit --max-shards ({max_shards}); stopping")
                cache.flush()
                return
            urls = cdx_shard(fetcher, host, shard, limit)
            shards_run += 1
            if urls is None:
                # Throttled/failed: a coverage gap. Do NOT mark the shard done,
                # so a later rerun retries it.
                fetcher.stats.notes[f"cdx-gap:{host}:{shard}"] += 1
                print(f"[{ats}] CDX {host}/{shard}* -> FAILED (coverage gap, will retry on rerun)")
                continue
            added = 0
            for url in urls:
                tenant = tenant_from_url(url, host)
                if tenant and cache.add_candidate(ats, tenant, source):
                    added += 1
            cache.mark_shard_done(ats, source)
            cache.flush()
            pending = sum(1 for r in cache.candidates(ats) if r.get("status") == "new")
            print(
                f"[{ats}] CDX {host}/{shard}* -> {len(urls)} urls, +{added} new tenants "
                f"({pending} unverified cached)"
            )
            if pending >= target:
                print(f"[{ats}] discovery target {target} reached")
                cache.flush()
                return

    total = cache.discovered_count(ats)
    if total < cc_threshold:
        print(f"[{ats}] CDX thin ({total} candidates < {cc_threshold}); trying Common Crawl")
        index_url = commoncrawl_index_url(fetcher)
        if index_url is None:
            print(f"[{ats}] Common Crawl index unavailable; skipping fallback")
            fetcher.stats.notes["commoncrawl-unavailable"] += 1
            return
        for shard in SHARD_ORDER:
            for host in hosts:
                source = f"commoncrawl:{host}:{shard}"
                if source in cache.sources_done:
                    continue
                urls = commoncrawl_shard(fetcher, index_url, host, shard, limit)
                if urls is None:
                    fetcher.stats.notes[f"cc-gap:{host}:{shard}"] += 1
                    continue
                added = 0
                for url in urls:
                    tenant = tenant_from_url(url, host)
                    if tenant and cache.add_candidate(ats, tenant, source):
                        added += 1
                cache.mark_shard_done(ats, source)
                cache.flush()
                print(f"[{ats}] CC {host}/{shard}* -> {len(urls)} urls, +{added} new tenants")
                pending = sum(1 for r in cache.candidates(ats) if r.get("status") == "new")
                if pending >= target:
                    return
    cache.flush()


# --------------------------------------------------------------------------
# Stage 2 -- live verification
# --------------------------------------------------------------------------


@dataclass
class BoardProbe:
    """Outcome of one live board probe."""

    state: str  # "live" | "dead" | "unknown"
    job_count: int = 0
    jobs: list = field(default_factory=list)
    company_name: str | None = None
    note: str = ""


def probe_board(fetcher: Fetcher, ats: str, tenant: str) -> BoardProbe:
    quoted = urllib.parse.quote(tenant, safe="")
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{quoted}/jobs"
        host = "boards-api.greenhouse.io"
    elif ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{quoted}"
        host = "api.ashbyhq.com"
    else:
        url = f"https://api.lever.co/v0/postings/{quoted}?mode=json"
        host = "api.lever.co"

    resp = fetcher.get(url, host_key=host, ok_statuses=(200,))
    if resp is None:
        return BoardProbe("unknown", note="no response after retries")
    if resp.status_code in (404, 410):
        return BoardProbe("dead", note=f"http {resp.status_code}")
    if resp.status_code != 200:
        return BoardProbe("unknown", note=f"http {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return BoardProbe("unknown", note="non-json body")

    company_name = None
    if ats == "lever":
        jobs = payload if isinstance(payload, list) else None
    else:
        if not isinstance(payload, dict):
            return BoardProbe("unknown", note="unexpected payload shape")
        jobs = payload.get("jobs")
        for key in ("organizationName", "name", "companyName", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                company_name = value.strip()
                break
    if not isinstance(jobs, list):
        return BoardProbe("unknown", note="no jobs array")
    if not jobs:
        return BoardProbe("dead", job_count=0, note="board live but zero open jobs")
    return BoardProbe("live", job_count=len(jobs), jobs=jobs, company_name=company_name)


# --------------------------------------------------------------------------
# Stage 3 -- company name + website domain from a real job-posting page
# --------------------------------------------------------------------------


def registrable_domain(host: str | None) -> str | None:
    if not host:
        return None
    labels = host.split(".")
    if len(labels) < 2:
        return None
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# A resolved "domain" is only believable if its TLD is real. Without this gate,
# link-mining happily returns things like `you.if` (a typo'd href) as a company
# domain. Rather than pin a full IANA list that would rot, the rule is: a
# two-letter TLD must be a real ccTLD, and anything longer must at least look
# like a TLD (alphabetic, or an `xn--` IDN TLD).
# ISO 3166-1 alpha-2 country codes, plus the handful of legacy/reserved two-letter
# TLDs still in the DNS root (ac, eu, su, tp, uk).
CC_TLDS = set(
    """ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm bn
    bo bq br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de
    dj dk dm do dz ec ee eg eh er es et eu fi fj fk fm fo fr ga gb gd ge gf gg gh gi gl gm
    gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp
    ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh
    mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om
    pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sj
    sk sl sm sn so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tp tr tt tv tw
    tz ua ug uk um us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw""".split()
)


def valid_tld(tld: str) -> bool:
    tld = tld.lower()
    if len(tld) == 2:
        return tld in CC_TLDS
    if tld.startswith("xn--"):
        return len(tld) > 4
    return 3 <= len(tld) <= 24 and tld.isalpha()


def plausible_domain(domain: str | None) -> bool:
    """True if `domain` looks like a real registrable employer domain."""
    if not domain or "." not in domain:
        return False
    if domain in DOMAIN_BLOCKLIST:
        return False
    labels = domain.split(".")
    if any(not label or len(label) > 63 for label in labels):
        return False
    if not valid_tld(labels[-1]):
        return False
    # Subdomains of a blocklisted vendor (e.g. jobs.ashbyhq.com) are never the employer.
    for i in range(1, len(labels) - 1):
        if ".".join(labels[i:]) in DOMAIN_BLOCKLIST:
            return False
    return True


def _norm_name(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


HREF_RE = re.compile(r'href=\\?"(https?://[^"\\\s]+)')


def name_matches_domain(domain: str, *names: str | None) -> bool:
    """True if `domain`'s registrable label plausibly names one of `names`."""
    core = _norm_name(domain.split(".")[0])
    if not core:
        return False
    for name in names:
        normalized = _norm_name(name)
        if normalized and (core in normalized or normalized in core):
            return True
    return False


def mine_domain_from_links(
    html: str, company_name: str | None, tenant: str, extra_urls=()
) -> tuple[str | None, str]:
    """Fallback domain resolution for pages with no JSON-LD `hiringOrganization`.

    Greenhouse's rendered job pages carry no structured data at all, and
    Lever's JSON-LD omits `sameAs`/`url`, so the employer's own domain has to
    come from the links actually present on the page (Lever's header logo
    link; the handbook/benefits/EEO links Greenhouse descriptions embed).

    Everything ATS-, social-, tracker-, benefits-vendor- and compliance-shaped
    is filtered out first, then a surviving domain is accepted only if it
    either **names** the employer (or its tenant slug), or is overwhelmingly
    dominant on the page (>= 4 links and at least twice the runner-up). The
    dominance bar is deliberately high: an earlier, looser "appears at least
    twice" rule produced confident-looking but wrong domains -- Convera
    resolved to its application vendor `service-now.com` -- and a wrong domain
    is far worse here than a board parked in targets_needs_domain.csv.
    """
    counts: Counter[str] = Counter()
    for url in list(extra_urls) + HREF_RE.findall(html):
        domain = registrable_domain(normalize_domain(url))
        if not plausible_domain(domain):
            continue
        assert domain is not None
        if domain.endswith((".gov", ".mil", ".edu")):
            continue
        counts[domain] += 1
    if not counts:
        return None, ""

    for domain, _ in counts.most_common():
        if name_matches_domain(domain, company_name, tenant):
            return domain, "name"

    ranked = counts.most_common(2)
    top, hits = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if hits >= 4 and hits >= 2 * max(runner_up, 1):
        return top, "dominant"
    return None, ""


CORPORATE_STOPWORDS = {
    "inc",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "company",
    "co",
    "the",
    "and",
    "group",
    "holdings",
    "holding",
    "plc",
    "gmbh",
    "bv",
    "ag",
    "sa",
    "llp",
    "lp",
    "pbc",
    "technologies",
    "technology",
    "solutions",
    "services",
    "systems",
    "labs",
    "international",
    "global",
}


def confirm_domain_live(fetcher: Fetcher, domain: str, company_name: str) -> bool:
    """Fetch `https://{domain}` and check the site actually names the employer.

    Only used for the `dominant` link-mining verdict, where the domain does
    *not* name the employer and so could just as easily be a vendor the job
    description happens to link four times (Capital Farm Credit's postings
    link only `baiworks.com`; a CATORCE posting links only `ddb.com`). One
    extra live fetch turns a guess into an observation: if the site itself
    does not mention the company, the board is parked in
    targets_needs_domain.csv rather than given a plausible-looking wrong
    domain.
    """
    tokens = {
        t
        for t in re.split(r"[^a-z0-9]+", (company_name or "").lower())
        if len(t) >= 5 and t not in CORPORATE_STOPWORDS
    }
    full = _norm_name(company_name)
    if not tokens and len(full) < 5:
        return False

    resp = fetcher.get(f"https://{domain}", timeout=HTML_TIMEOUT_S, attempts=2)
    if resp is None or resp.status_code != 200:
        return False

    # A redirect to the employer's real brand domain is itself confirmation.
    final_domain = registrable_domain(normalize_domain(str(resp.url)))
    if final_domain and name_matches_domain(final_domain, company_name):
        return True

    body = resp.text[:400_000].lower()
    squashed = re.sub(r"[^a-z0-9]", "", body)
    if full and len(full) >= 5 and full in squashed:
        return True
    return any(token in body for token in tokens)


def _jsonld_org_name(html: str) -> str | None:
    """`hiringOrganization.name` from the first JobPosting JSON-LD block."""
    for match in re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            payload = json.loads(match)
        except ValueError:
            continue
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                stack.extend(node["@graph"])
            org = node.get("hiringOrganization")
            if isinstance(org, dict) and isinstance(org.get("name"), str):
                name = org["name"].strip()
                if name:
                    return name
    return None


def job_page_urls(ats: str, tenant: str, jobs: list) -> list[str]:
    """Up to two individual job-posting *HTML* page URLs for this board."""
    urls: list[str] = []
    for job in jobs[:6]:
        if not isinstance(job, dict):
            continue
        if ats == "greenhouse":
            job_id = job.get("id")
            if job_id is None:
                continue
            urls.append(
                f"https://job-boards.greenhouse.io/{urllib.parse.quote(tenant, safe='')}"
                f"/jobs/{job_id}"
            )
        elif ats == "ashby":
            url = job.get("jobUrl")
            if isinstance(url, str) and url.startswith("http"):
                urls.append(url)
        else:
            url = job.get("hostedUrl")
            if isinstance(url, str) and url.startswith("http"):
                urls.append(url)
        if len(urls) >= 2:
            break
    return urls


def greenhouse_board_name(fetcher: Fetcher, tenant: str) -> str | None:
    resp = fetcher.get(
        f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(tenant, safe='')}",
        host_key="boards-api.greenhouse.io",
    )
    if resp is None or resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    name = payload.get("name") if isinstance(payload, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


GENERIC_BOARD_NAMES = {
    "jobboard",
    "jobsboard",
    "jobs",
    "job",
    "careers",
    "career",
    "careerpage",
    "openings",
    "openroles",
    "openpositions",
    "wearehiring",
    "hiring",
    "joinus",
    "workwithus",
    "home",
    "main",
    "test",
    "demo",
}


def titleize(slug: str) -> str:
    words = [w for w in re.split(r"[-_.]+", slug) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or slug


def resolve_identity(
    fetcher: Fetcher, ats: str, tenant: str, probe: BoardProbe
) -> tuple[str, str | None, str]:
    """Return `(company_name, website_domain_or_None, note)` for a kept board."""
    company_name = (probe.company_name or "").strip()
    if ats == "greenhouse" and not company_name:
        company_name = greenhouse_board_name(fetcher, tenant) or ""
    if _norm_name(company_name) in GENERIC_BOARD_NAMES:
        # Plenty of Greenhouse boards are titled literally "Job Board" /
        # "Careers"; that is not a company name and it poisons name-matching.
        company_name = ""

    # Greenhouse's postings API echoes the employer name on every job.
    if not company_name and ats == "greenhouse":
        for job in probe.jobs[:3]:
            if isinstance(job, dict) and isinstance(job.get("company_name"), str):
                company_name = job["company_name"].strip()
                break

    extra_urls: list[str] = []
    if ats == "greenhouse":
        for job in probe.jobs[:10]:
            if isinstance(job, dict) and isinstance(job.get("absolute_url"), str):
                extra_urls.append(job["absolute_url"])

    domain: str | None = None
    notes: list[str] = []
    for page_url in job_page_urls(ats, tenant, probe.jobs):
        resp = fetcher.get(
            page_url,
            host_key=urllib.parse.urlsplit(page_url).hostname or "job-page",
            timeout=HTML_TIMEOUT_S,
            attempts=3,
        )
        if resp is None or resp.status_code != 200:
            notes.append(f"job page {resp.status_code if resp else 'no-response'}")
            continue
        html = resp.text
        if not company_name:
            company_name = _jsonld_org_name(html) or ""
        posting = extract_job_posting(html)
        if posting is not None and posting.company_domain_candidate:
            # JSON-LD `hiringOrganization.sameAs` is authoritative, but it is
            # still remote, untrusted content: Ashby boards that leave it unset
            # fall back to `jobs.ashbyhq.com`, which is not an employer domain.
            candidate = normalize_domain(posting.company_domain_candidate)
            registrable = registrable_domain(candidate)
            if candidate and plausible_domain(registrable):
                domain = candidate
                # The JSON-LD node names the employer *and* its site together,
                # so prefer its name over the ATS board title, which can lag a
                # rebrand or acquisition (Greenhouse tenant "classpass" now
                # serves Playlist, and the board is still titled "ClassPass").
                jsonld_name = _jsonld_org_name(html)
                if jsonld_name and _norm_name(jsonld_name) not in GENERIC_BOARD_NAMES:
                    company_name = jsonld_name
                notes.append("domain from json-ld")
                break
            if candidate:
                notes.append(f"rejected json-ld domain {candidate}")
        domain, verdict = mine_domain_from_links(html, company_name, tenant, extra_urls)
        if domain and verdict == "dominant":
            if confirm_domain_live(fetcher, domain, company_name or titleize(tenant)):
                notes.append(f"domain from page links ({domain} confirmed live)")
                break
            notes.append(f"rejected unconfirmed link domain {domain}")
            domain = None
        elif domain:
            notes.append("domain from page links (name match)")
            break
        if not domain:
            notes.append("no domain on this page")

    if not company_name:
        company_name = titleize(tenant)
        notes.append("name from slug")
    # Board titles and JSON-LD names arrive HTML-escaped often enough
    # ("Canopy A&amp;D") that the entities have to be decoded before storing.
    company_name = html_module.unescape(company_name)
    company_name = re.sub(r"\s+", " ", company_name).strip()[:120]
    return company_name, domain, "; ".join(notes)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

STOP = {"flag": False}


def _handle_sigint(_sig, _frame):  # pragma: no cover - interactive only
    if STOP["flag"]:
        raise KeyboardInterrupt
    STOP["flag"] = True
    print(
        "\n[stop] interrupt received -- finishing current board, then flushing state\n",
        flush=True,
    )


def handle_candidate(
    fetcher: Fetcher,
    cache: CandidateCache,
    ats: str,
    tenant: str,
    out_path: Path,
    existing: set[tuple[str, str]],
    budget: dict[str, int],
) -> None:
    """Probe one candidate board, and append it to `out_path` if it qualifies."""
    probe = probe_board(fetcher, ats, tenant)
    now = datetime.now(UTC).isoformat()

    if probe.state == "unknown":
        # Transient (timeout / 429 / 5xx after retries). Leave status="new" so a
        # rerun retries it: a throttled probe is a coverage gap, not a dead board.
        cache.update(ats, tenant, status="new", checked_at=now, note=f"transient: {probe.note}")
        fetcher.stats.notes[f"transient:{ats}"] += 1
        print(f"  [{ats}/{tenant}] transient failure ({probe.note}) -- left for rerun")
        cache.flush()
        return

    if probe.state == "dead":
        cache.update(
            ats,
            tenant,
            status="reject_dead",
            open_job_count=probe.job_count,
            checked_at=now,
            note=probe.note,
        )
        cache.flush()
        return

    if probe.job_count < MIN_JOBS:
        cache.update(
            ats,
            tenant,
            status="reject_too_few",
            open_job_count=probe.job_count,
            checked_at=now,
            note=f"{probe.job_count} < {MIN_JOBS}",
        )
        cache.flush()
        return

    if probe.job_count > MAX_JOBS:
        cache.update(
            ats,
            tenant,
            status="reject_too_many",
            open_job_count=probe.job_count,
            checked_at=now,
            note=f"{probe.job_count} > {MAX_JOBS}",
        )
        cache.flush()
        return

    company_name, domain, note = resolve_identity(fetcher, ats, tenant, probe)
    checked_at = datetime.now(UTC).isoformat()

    if not domain:
        cache.update(
            ats,
            tenant,
            status="reject_no_domain",
            open_job_count=probe.job_count,
            company_name=company_name,
            checked_at=checked_at,
            note=note,
        )
        append_row(
            NEEDS_DOMAIN,
            NEEDS_DOMAIN_FIELDS,
            {
                "company_name": company_name,
                "ats": ats,
                "tenant": tenant,
                "open_job_count": probe.job_count,
                "checked_at": checked_at,
            },
        )
        cache.flush()
        print(f"  [{ats}/{tenant}] {probe.job_count} jobs, NO DOMAIN -> needs_domain ({note})")
        return

    append_row(
        out_path,
        TARGETS_FIELDS,
        {
            "company_name": company_name,
            "website_domain": domain,
            "ats": ats,
            "tenant": tenant,
            "open_job_count": probe.job_count,
            "checked_at": checked_at,
        },
    )
    existing.add((ats, tenant))
    budget["remaining"] -= 1
    budget["added"] += 1
    cache.update(
        ats,
        tenant,
        status="added",
        open_job_count=probe.job_count,
        company_name=company_name,
        website_domain=domain,
        checked_at=checked_at,
        note=note,
    )
    cache.flush()
    print(
        f"  [{ats}/{tenant}] ADDED {company_name} ({domain}) {probe.job_count} jobs "
        f"-- {budget['added']} added, {budget['remaining']} left in budget"
    )


def verify_candidates(
    fetcher: Fetcher,
    cache: CandidateCache,
    ats_list: tuple[str, ...],
    args: argparse.Namespace,
    out_path: Path,
    existing: set[tuple[str, str]],
    budget: dict[str, int],
    deadline: float | None,
) -> None:
    """Probe cached candidates, round-robining across the ATSes.

    Each ATS has its own API host and its own 1 req/s budget, so cycling
    between them means the per-host politeness sleep for one ATS is spent
    doing useful work on another -- roughly 3x the throughput of draining
    one ATS at a time, with identical per-host request rates.
    """
    queues: dict[str, list[str]] = {}
    for ats in ats_list:
        pending = [r["tenant"] for r in cache.candidates(ats) if r.get("status") == "new"]
        queues[ats] = pending
        print(f"[{ats}] {len(pending)} cached candidates pending verification")
    print(f"budget: {budget['remaining']} new rows this invocation")

    cursors = dict.fromkeys(ats_list, 0)
    processed = dict.fromkeys(ats_list, 0)
    active = [a for a in ats_list if queues[a]]
    turn = 0

    while active and budget["remaining"] > 0 and not STOP["flag"]:
        if deadline is not None and time.monotonic() > deadline:
            print("time budget exhausted; stopping cleanly")
            break
        ats = active[turn % len(active)]
        turn += 1
        queue = queues[ats]
        index = cursors[ats]
        if index >= len(queue) or (args.max_verify and processed[ats] >= args.max_verify):
            active.remove(ats)
            turn = 0
            continue
        cursors[ats] = index + 1
        tenant = queue[index]
        if (ats, tenant) in existing:
            cache.update(ats, tenant, status="already_in_targets", note="present in --out")
            continue
        processed[ats] += 1
        handle_candidate(fetcher, cache, ats, tenant, out_path, existing, budget)

    cache.flush()


def print_stats(cache: CandidateCache, ats_list: tuple[str, ...]) -> None:
    print("\n=== candidate cache summary ===")
    for ats in ats_list:
        rows = cache.candidates(ats)
        counts = Counter(r.get("status", "") for r in rows)
        live = sum(
            counts[s] for s in ("added", "reject_too_few", "reject_too_many", "reject_no_domain")
        )
        print(
            f"{ats:11s} discovered={len(rows):5d}  unverified={counts['new']:5d}  "
            f"verified_live={live:4d}  added={counts['added']:4d}  "
            f"dead={counts['reject_dead']:4d}  too_few={counts['reject_too_few']:4d}  "
            f"too_many={counts['reject_too_many']:4d}  no_domain={counts['reject_no_domain']:4d}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ats", choices=[*ALL_ATS, "all"], default="all")
    parser.add_argument(
        "--max-new",
        type=int,
        default=50,
        help="cap on new verified+resolved rows appended this invocation",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--rediscover",
        action="store_true",
        help="re-run CDX shards even if already recorded in the cache",
    )
    parser.add_argument(
        "--discover-target",
        type=int,
        default=1200,
        help="stop CDX sharding once this many unverified candidates are cached",
    )
    parser.add_argument(
        "--max-shards",
        type=int,
        default=40,
        help="cap on CDX shard requests per ATS per invocation",
    )
    parser.add_argument("--cdx-limit", type=int, default=20000)
    parser.add_argument(
        "--commoncrawl-threshold",
        type=int,
        default=200,
        help="fall back to Common Crawl if CDX yielded fewer candidates",
    )
    parser.add_argument(
        "--max-verify",
        type=int,
        default=0,
        help="cap on candidates probed per ATS this invocation (0 = unlimited)",
    )
    parser.add_argument(
        "--time-budget",
        type=float,
        default=0.0,
        help="seconds after which the run stops cleanly (0 = unlimited)",
    )
    parser.add_argument(
        "--retry-no-domain",
        action="store_true",
        help="re-queue boards previously parked in targets_needs_domain.csv",
    )
    parser.add_argument("--stats", action="store_true", help="print cache stats and exit")
    args = parser.parse_args()

    out_path = args.out if args.out.is_absolute() else (PROJECT_ROOT / args.out)
    ats_list = ALL_ATS if args.ats == "all" else (args.ats,)

    cache = CandidateCache(CANDIDATE_CACHE)
    if args.stats:
        print_stats(cache, ALL_ATS)
        return

    existing, existing_count = load_existing_targets(out_path)
    print(f"{out_path} currently has {existing_count} data rows")

    if args.retry_no_domain:
        requeued = 0
        for row in cache.rows:
            if row.get("ats") in ats_list and row.get("status") == "reject_no_domain":
                row["status"] = "new"
                row["note"] = "requeued by --retry-no-domain"
                requeued += 1
        if requeued:
            cache.mark_dirty()
            cache.flush()
        print(f"re-queued {requeued} previously unresolvable boards")

    signal.signal(signal.SIGINT, _handle_sigint)
    stats = Stats()
    fetcher = Fetcher(stats)
    budget = {"remaining": args.max_new, "added": 0}
    deadline = time.monotonic() + args.time_budget if args.time_budget else None

    try:
        if not args.verify_only:
            # Discovery is sequential: every ATS shares one archive host, so its
            # 0.5 req/s budget cannot be split across ATSes the way the live
            # verification hosts can.
            for ats in ats_list:
                if STOP["flag"]:
                    break
                if deadline is not None and time.monotonic() > deadline:
                    break
                discover(
                    fetcher,
                    cache,
                    ats,
                    target=args.discover_target,
                    limit=args.cdx_limit,
                    max_shards=args.max_shards,
                    cc_threshold=args.commoncrawl_threshold,
                    rediscover=args.rediscover,
                )
        if not args.discover_only:
            verify_candidates(fetcher, cache, ats_list, args, out_path, existing, budget, deadline)
    except KeyboardInterrupt:
        print("\n[stop] hard interrupt")
    finally:
        cache.flush(force=True)
        parked = rewrite_needs_domain(cache)
        print(f"\n{NEEDS_DOMAIN} rebuilt: {parked} boards kept but lacking a resolvable domain")
        fetcher.close()

    _, final_count = load_existing_targets(out_path)
    print(f"\nappended {budget['added']} rows; {out_path} now has {final_count} data rows")
    print(
        f"http: {stats.requests} requests, {stats.timeouts} timeouts, "
        f"{stats.throttled_429} x 429, {stats.server_5xx} x 5xx, "
        f"{stats.transport_errors} transport errors, {stats.give_ups} gave up after retries, "
        f"{stats.cdx_pages} CDX pages"
    )
    if stats.notes:
        print("notes: " + ", ".join(f"{k}={v}" for k, v in stats.notes.most_common(12)))
    print_stats(cache, ats_list)


if __name__ == "__main__":
    main()
