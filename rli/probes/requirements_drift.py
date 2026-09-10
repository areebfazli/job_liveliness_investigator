"""`requirements_drift` — the medium-cost, history-gated version-diff probe.

spec.md §4 lists `requirements_drift` as a dynamic probe returning a
"structured version diff" at `medium` cost with `history required = yes`.
This module fetches the posting's CURRENT content from its ATS (the only
authoritative source of what the role asks for today) and compares it
against what this system already recorded about the posting — the
`postings` row's title/team/location, the most recent
`board_snapshot_jobs.description_hash`, and, when one exists, the earliest
archived capture of the posting page.

Like every probe it is read-only and never raises for a network or parse
problem (`rli.probes.base`).

What this comparison can and cannot see (be honest about the schema)
--------------------------------------------------------------------
The schema stores a `description_hash`, never raw description text
(`rli.history.matching` documents the same constraint for repost scoring).
So the description comparison is a HASH comparison: equal / different /
unknown. There is no semantic diff, no "the seniority requirement moved
from 5 to 8 years" — that would need stored text this system does not have.

Consequently:

* `title_changed` / `location_changed` / `team_changed` compare the live ATS
  value against the stored `postings` value, after
  `rli.history.matching.normalize_text` folding (case and punctuation), so
  `"Sr. Engineer (Remote)"` vs `"Senior Engineer, Remote"` is not reported
  as drift on formatting alone — though normalization is crude and
  "Sr." vs "Senior" DOES still read as a change here.
* `description_changed` compares the live content hash against the most
  recent known `board_snapshot_jobs.description_hash`. **A hash missing on
  either side yields `None` (unknown), never `True`.** Scoring missing data
  as "changed" would manufacture evidence of difference out of a coverage
  hole — exactly the failure mode spec.md §4 forbids and that
  `rli.history.matching._description_score` already avoids.

GUESSED / judgment calls made in this module
--------------------------------------------

* **The line diff is descriptive context only; it never decides the claim.**
  When both a live body and an archived body are available, a coarse
  stdlib `difflib.unified_diff` over whitespace-normalized lines is
  reported (`data["line_diff"]`: added/removed counts plus a bounded
  sample). It is NOT folded into `requirements_changed`, because the two
  sides are different KINDS of document: the live side is the ATS API's
  description HTML, the archived side is a whole archived web page
  including site chrome, navigation and the archive's own banner. A large
  line delta between them is expected even for a posting whose
  requirements never moved, so letting it flip the claim would produce
  confident false "changed" evidence. This is a deliberate deviation from
  a literal "or the line diff is non-trivial" reading of the task; revisit
  if raw description text ever gets stored, at which point the two sides
  become genuinely comparable and this diff can be promoted to a signal.
* **No claim at all when nothing was comparable.** If every field
  comparison is `None` (no live fetch, or no stored counterpart), the probe
  emits ZERO claims rather than `requirements_unchanged`. "Unchanged" is a
  positive claim about content; it needs at least one real comparison
  behind it. Again a deviation from "emit exactly one claim", for the same
  reason the rest of this codebase distinguishes Unknown from False.
* **A failed live fetch degrades the comparison, it does not abandon it.**
  `ok`/`error`/`retryable` report the authoritative live lookup (mirroring
  `rli.probes.resolve_posting`, where `ok` tracks the authoritative
  lookup while `data` still carries everything gathered), but any
  archive-side facts collected are still returned. A 404 from the ATS, or
  a job absent from the board listing, is NOT a transport failure: it is a
  successful observation that the job is gone, so `ok` stays `True` with
  `live_job_found=False`.
* **`ats = 'other'` (no adapter) is not a failure either** — the posting was
  already resolved by `resolve_posting`, so "this ATS has no API we can
  diff against" is a known limitation, reported as
  `live_fetch="no_adapter"`, not as `ok=False`.
* **Prior description hash = the most recent capture that actually carries
  one**, not merely the most recent capture that listed the job. A capture
  row with a NULL `description_hash` (common for thin archive scrapes)
  would otherwise blank out a perfectly good comparison against a slightly
  older capture. `data["prior_description_captured_at"]` reports which
  capture was used, so the choice is auditable rather than implicit.
* **A stored `capture_url` may point outside this probe's allowlist.** It is
  data read from the database, not a URL chosen by this code, so a
  `DisallowedHostError` from `rli.net` (which treats an off-allowlist fetch
  as a programming error and raises) is caught here and recorded as
  `archive_fetch="disallowed_host"`. A probe must not raise for the shape
  of stored data.
* **History gating is identical to `rli.probes.repost_history`**: below
  `thresholds.min_history_days` the probe returns `ok=True`,
  `usable_history=False` and no claims (spec.md §4).
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from typing import Any, ClassVar

from pydantic import BaseModel

from rli.history.features import coverage_window
from rli.history.matching import normalize_text
from rli.models.probe import ProbeResult
from rli.net import DisallowedHostError
from rli.probes.base import Probe, ProbeClaim, ProbeContext
from rli.probes.lookups import has_usable_history, posting_row
from rli.resolvers import ashby, greenhouse, lever

__all__ = ["RequirementsDriftArgs", "RequirementsDriftProbe", "requirements_drift"]

PROBE_NAME = "requirements_drift"

# Self-describing, deliberately non-fetchable fallback source_url, in the
# style of rli.history.closures.ARCHIVE_ONLY_URL_PLACEHOLDER.
POSTING_HISTORY_URL_PLACEHOLDER = "posting-history:{posting_id}"

# Bound on how much of the coarse line diff is carried in `data` — a diff
# sample is context for a human reader, not a payload to be stored whole.
MAX_DIFF_SAMPLE_LINES = 10


class RequirementsDriftArgs(BaseModel):
    posting_id: str


class _LiveJob(BaseModel):
    """The subset of a live ATS job this probe compares against."""

    title: str | None = None
    team: str | None = None
    location: str | None = None
    body: str | None = None
    content_hash: str | None = None
    url: str | None = None


def _fetch_live(
    row: sqlite3.Row, ctx: ProbeContext
) -> tuple[_LiveJob | None, str, bool, str | None, bool]:
    """Fetch the posting's CURRENT ATS content.

    Returns `(job, status, ok, error, retryable)` where `status` is one of
    `"found"`, `"not_found"`, `"no_adapter"`, `"fetch_failed"` (see the
    module docstring for why only the last flips `ok`).
    """
    ats = row["ats"]
    tenant = row["ats_tenant_id"]
    job_id = row["ats_job_id"]

    if ats not in ("greenhouse", "ashby", "lever") or not tenant or not job_id:
        return None, "no_adapter", True, None, False

    net = ctx.net_client(PROBE_NAME)

    if ats == "greenhouse":
        fetch = greenhouse.fetch_job(net, tenant, job_id)
        if fetch.status == 404:
            return None, "not_found", True, None, False
        if not fetch.ok:
            return None, "fetch_failed", False, fetch.error, fetch.retryable
        if fetch.data is None:
            return None, "not_found", True, None, False
        job = fetch.data
        return (
            _LiveJob(
                title=job.title,
                team=job.departments[0] if job.departments else None,
                location=job.location,
                body=job.content,
                content_hash=job.content_hash,
                url=job.absolute_url,
            ),
            "found",
            True,
            None,
            False,
        )

    if ats == "ashby":
        board = ashby.fetch_board(net, tenant)
        if not board.ok:
            return None, "fetch_failed", False, board.error, board.retryable
        if board.data is None:
            return None, "not_found", True, None, False
        found = ashby.find_job(board.data, job_id)
        if found is None:
            return None, "not_found", True, None, False
        return (
            _LiveJob(
                title=found.title,
                team=found.team or found.department,
                location=found.location,
                body=found.description_html,
                content_hash=found.content_hash,
                url=found.job_url,
            ),
            "found",
            True,
            None,
            False,
        )

    board = lever.fetch_board(net, tenant)
    if not board.ok:
        return None, "fetch_failed", False, board.error, board.retryable
    if board.data is None:
        return None, "not_found", True, None, False
    found = lever.find_job(board.data, job_id)
    if found is None:
        return None, "not_found", True, None, False
    return (
        _LiveJob(
            title=found.text,
            team=found.team,
            location=found.location,
            body=found.description_plain,
            content_hash=found.content_hash,
            url=found.hosted_url,
        ),
        "found",
        True,
        None,
        False,
    )


def _earliest_archive_snapshot(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, captured_at, capture_url, content_hash
        FROM posting_snapshots
        WHERE posting_id = ? AND source = 'archive'
        ORDER BY captured_at, id
        LIMIT 1
        """,
        (posting_id,),
    ).fetchone()


def _fetch_archive_body(capture_url: str | None, ctx: ProbeContext) -> tuple[str | None, str]:
    """Fetch an archived capture body. Returns `(body, status)`.

    Never raises: a `capture_url` is stored data whose host this probe's
    allowlist may not cover (module docstring), so `DisallowedHostError` is
    caught rather than propagated out of a probe.
    """
    if not capture_url:
        return None, "no_capture_url"
    try:
        result = ctx.net_client(PROBE_NAME).get(capture_url)
    except DisallowedHostError:
        return None, "disallowed_host"
    if not result.ok:
        return None, "fetch_failed"
    return result.body, "found"


def _prior_description(
    conn: sqlite3.Connection, company_id: str, ats_job_id: str | None
) -> tuple[str | None, str | None]:
    """Most recent non-NULL `description_hash` for this job, with its capture time."""
    if ats_job_id is None:
        return None, None
    row = conn.execute(
        """
        SELECT j.description_hash, s.captured_at
        FROM board_snapshot_jobs AS j
        JOIN board_snapshots AS s ON s.id = j.board_snapshot_id
        WHERE s.company_id = ? AND j.job_id = ? AND j.description_hash IS NOT NULL
        ORDER BY s.captured_at DESC, s.id DESC
        LIMIT 1
        """,
        (company_id, ats_job_id),
    ).fetchone()
    if row is None:
        return None, None
    return row["description_hash"], row["captured_at"]


def _changed(live: str | None, stored: str | None) -> bool | None:
    """`True`/`False`/`None` (uncomparable) for one normalized text field."""
    left, right = normalize_text(live), normalize_text(stored)
    if not left or not right:
        return None
    return left != right


def _line_diff(live_body: str | None, archive_body: str | None) -> dict[str, Any] | None:
    """Coarse, best-effort line diff — context only, never a verdict.

    See the module docstring: the two sides are different kinds of document,
    so this reports magnitude and a sample and nothing more.
    """
    if not live_body or not archive_body:
        return None

    old = [line.strip() for line in archive_body.splitlines() if line.strip()]
    new = [line.strip() for line in live_body.splitlines() if line.strip()]

    added: list[str] = []
    removed: list[str] = []
    for line in difflib.unified_diff(old, new, lineterm="", n=0):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])

    return {
        "added_lines": len(added),
        "removed_lines": len(removed),
        "added_sample": added[:MAX_DIFF_SAMPLE_LINES],
        "removed_sample": removed[:MAX_DIFF_SAMPLE_LINES],
        "comparable": False,  # see the module docstring: different document kinds
    }


def _insufficient_history(
    posting_id: str, company_id: str, history_days: float, history_coverage: float
) -> ProbeResult:
    return ProbeResult(
        ok=True,
        data={
            "posting_id": posting_id,
            "company_id": company_id,
            "usable_history": False,
            "history_days": history_days,
            "history_coverage": history_coverage,
            "title_changed": None,
            "location_changed": None,
            "team_changed": None,
            "description_changed": None,
            "line_diff": None,
            "evidence": [],
        },
    )


def requirements_drift(posting_id: str, ctx: ProbeContext) -> ProbeResult:
    """Pure function backing `RequirementsDriftProbe.run` (spec.md §4)."""
    now = ctx.now()

    row = posting_row(ctx.conn, posting_id)
    if row is None:
        return ProbeResult(
            ok=False,
            error=f"no posting row for posting_id={posting_id!r}",
            retryable=False,
            data={"posting_id": posting_id, "evidence": []},
        )

    company_id = row["company_id"]
    coverage = coverage_window(ctx.conn, company_id)
    if coverage.history_days < ctx.config.thresholds.min_history_days:
        return _insufficient_history(
            posting_id, company_id, coverage.history_days, coverage.history_coverage
        )

    live, live_status, ok, error, retryable = _fetch_live(row, ctx)

    archive_row = _earliest_archive_snapshot(ctx.conn, posting_id)
    archive_body, archive_status = _fetch_archive_body(
        archive_row["capture_url"] if archive_row is not None else None, ctx
    )

    prior_hash, prior_captured_at = _prior_description(ctx.conn, company_id, row["ats_job_id"])

    title_changed = _changed(live.title if live else None, row["title"])
    location_changed = _changed(live.location if live else None, row["location"])
    team_changed = _changed(live.team if live else None, row["team"])

    live_hash = live.content_hash if live else None
    description_changed = (
        None if (live_hash is None or prior_hash is None) else live_hash != prior_hash
    )

    line_diff = _line_diff(live.body if live else None, archive_body)

    comparisons = {
        "title_changed": title_changed,
        "location_changed": location_changed,
        "team_changed": team_changed,
        "description_changed": description_changed,
    }
    known = {name: value for name, value in comparisons.items() if value is not None}

    claims: list[ProbeClaim] = []
    if known:
        changed = any(known.values())
        if live_status == "found":
            source_url = (live.url if live and live.url else None) or row["canonical_url"]
            source_quality = "ats_native"
        elif archive_row is not None and archive_row["capture_url"]:
            source_url = archive_row["capture_url"]
            source_quality = "archive"
        else:
            source_url = POSTING_HISTORY_URL_PLACEHOLDER.format(posting_id=posting_id)
            source_quality = "archive"

        claims.append(
            ProbeClaim(
                claim_type="requirements_changed" if changed else "requirements_unchanged",
                value=(json.dumps(known, sort_keys=True) if changed else "no changes detected"),
                source_url=source_url,
                raw_excerpt=json.dumps(comparisons, sort_keys=True),
                source_quality=source_quality,
                available_at=now,
                fetched_at=now,
            )
        )

    return ProbeResult(
        ok=ok,
        error=error,
        retryable=retryable,
        data={
            "posting_id": posting_id,
            "company_id": company_id,
            "usable_history": True,
            "history_days": coverage.history_days,
            "history_coverage": coverage.history_coverage,
            "live_fetch": live_status,
            "live_job_found": live_status == "found",
            "archive_fetch": archive_status,
            "archive_captured_at": (
                archive_row["captured_at"] if archive_row is not None else None
            ),
            "prior_description_hash": prior_hash,
            "prior_description_captured_at": prior_captured_at,
            "live_description_hash": live_hash,
            "title_changed": title_changed,
            "location_changed": location_changed,
            "team_changed": team_changed,
            "description_changed": description_changed,
            "line_diff": line_diff,
            "evidence": claims,
        },
    )


class RequirementsDriftProbe(Probe):
    """Dynamic probe: structured version diff for one posting (spec.md §4)."""

    name: ClassVar[str] = PROBE_NAME
    cost_tier: ClassVar[str] = "medium"
    history_required: ClassVar[bool] = True
    populates: ClassVar[frozenset[str]] = frozenset({"repost_pattern"})
    ArgsModel: ClassVar[type[BaseModel]] = RequirementsDriftArgs

    @classmethod
    def eligible(cls, ctx: ProbeContext, args: RequirementsDriftArgs) -> bool:
        """Controller-side preflight; identical rule to `RepostHistoryProbe`."""
        row = posting_row(ctx.conn, args.posting_id)
        if row is None:
            return False
        return has_usable_history(ctx.conn, ctx.config, row["company_id"])

    def run(self, args: RequirementsDriftArgs, ctx: ProbeContext) -> ProbeResult:
        return requirements_drift(args.posting_id, ctx)
