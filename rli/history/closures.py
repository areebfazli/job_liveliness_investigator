"""Interval-censored closure derivation from board snapshots (PLAN.md M2 #1).

spec.md §4: "Archive-derived closures are **interval-censored**: keep
`last_seen_open` and `first_seen_absent`. Do not invent an exact `closed_at`
from sparse captures." This module is that derivation, run over ALL
`board_snapshots` for a company — own daily captures and Wayback-derived
archive captures together — and projected onto the `postings` lifecycle
columns by `apply_to_postings`.

Nothing here ever writes a `closed_at`; there is no such column and there is
no such fact. A closed posting is represented by the pair
`(last_seen_open, first_seen_absent)`, which brackets the unknown true
closure time, plus `gap_days` = the width of that bracket, so a consumer can
see how precise the bracket is instead of trusting a fabricated point.

Coverage rules (spec.md §4: "a throttled or failed capture is recorded as a
coverage gap, never as an absence"):

* PRESENCE is read from a capture of ANY `coverage_status`. If a job is
  listed in a capture, we saw it, even if that capture was `'partial'`
  (an HTML board page that we only partly scraped) or `'gap'`.
* ABSENCE is read ONLY from a capture with `coverage_status = 'complete'`.
  A job missing from a `'partial'` or `'gap'` capture is a coverage hole,
  not a disappearance: the search for "the capture where this job was first
  missing" skips straight over such captures to the next `'complete'` one.

GUESSED / judgment calls made in this module:

* **Correlation key `(company_id, ats_job_id)`.** `board_snapshots` /
  `board_snapshot_jobs` carry no ATS or tenant column — only `company_id` —
  so `postings.posting_id` (`"{ats}:{tenant}:{job_id}"`, built by
  `rli.snapshots.daily`) cannot be reconstructed from a snapshot row alone.
  The join key that does exist is `(postings.company_id,
  postings.ats_job_id)` against `(board_snapshots.company_id,
  board_snapshot_jobs.job_id)`: `daily.py` populates `ats_job_id` with the
  bare `BoardJob.job_id`, exactly the value stored in
  `board_snapshot_jobs.job_id`. Known limitation, accepted rather than
  engineered around: two different ATS tenants under the SAME `company_id`
  that happen to reuse the same bare job id would collide into one interval.
  When more than one `postings` row matches a key, the lowest `posting_id`
  wins (deterministic) and the collision is counted in the summary's
  `ambiguous_job_ids`.
* **`first_seen_absent` is the FIRST observed absence, not the terminal
  one.** A literal reading of "the earliest complete capture after
  `last_seen_open` that does not contain the job" would make `reappeared_at`
  unreachable by construction, since no capture after `last_seen_open` can
  contain the job. So `first_seen_absent` is defined as the earliest
  `'complete'` capture AFTER `first_observed` that does not list the job —
  matching `rli.snapshots.daily`, which sets `first_seen_absent` once and
  never overwrites it — and the absence that brackets the CLOSURE is
  exposed separately as `closure_absent_at` (the earliest `'complete'`
  capture after `last_seen_open` that does not list the job).
  `closure_absent_at == first_seen_absent` for every posting that never
  reappeared, which is the ordinary case; they differ only for a
  disappear/reappear/disappear history, where `gap_days` must bracket the
  LAST disappearance, not the first.
* **Job facts (title/team/location/description_hash/url) are taken from the
  most recent capture that listed the job**, not the first, so downstream
  repost matching compares the posting as it last appeared.
* **Archive-only postings** (a `(company_id, job_id)` seen in snapshots that
  matches no `postings` row) get a row created by `apply_to_postings` with
  `posting_id = "archive:{company_id}:{job_id}"`, so the synthetic identity
  is self-describing and can never collide with a real
  `"{ats}:{tenant}:{job_id}"`. `board_snapshots` carries no ATS/tenant, so
  `ats` is borrowed from the company's most common existing `postings.ats`
  (ties broken alphabetically) and falls back to `'other'` when the company
  has no postings at all; `ats_tenant_id` is left NULL, because guessing it
  would be fabrication. `canonical_url` uses `board_snapshot_jobs.url` when
  present and otherwise the clearly-invalid placeholder
  `"archive-only:{company_id}/{job_id}"` (the column is NOT NULL) — never a
  plausible-looking but unverified ATS URL.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.models.time import now_utc, parse_utc, to_utc_z

__all__ = [
    "ClosureApplySummary",
    "PostingInterval",
    "apply_to_postings",
    "build_intervals",
    "load_captures",
]

Censoring = Literal["right", "interval"]

# Placeholder canonical_url for an archive-only posting whose snapshot row
# carried no url. Deliberately not a valid https URL: `canonical_url` is NOT
# NULL, and a wrong-but-plausible board URL would be worse than an obviously
# synthetic one (a reader, or a later resolver, must be able to tell that
# this posting has no verified URL).
ARCHIVE_ONLY_URL_PLACEHOLDER = "archive-only:{company_id}/{job_id}"


class JobFacts(BaseModel):
    """The descriptive fields of one job as listed in one board capture."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    title: str | None = None
    team: str | None = None
    location: str | None = None
    description_hash: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Capture:
    """One `board_snapshots` row plus the jobs it listed."""

    snapshot_id: int
    captured_at: datetime
    source: str
    coverage_status: str
    jobs: dict[str, JobFacts]

    @property
    def is_complete(self) -> bool:
        """True when this capture may be used as evidence of ABSENCE."""
        return self.coverage_status == "complete"


class PostingInterval(BaseModel):
    """Interval-censored lifecycle of one job, derived from board captures.

    `censoring`:

    * `'right'` — no `'complete'` capture after `last_seen_open` is missing
      this job, so as far as the captures go it is still open. The true
      closure time lies somewhere in the unobserved future; it is NOT
      "unknown" in the sense of missing data, it is right-censored.
    * `'interval'` — the job is absent from some `'complete'` capture at
      `closure_absent_at`, so the true closure time lies in
      `(last_seen_open, closure_absent_at]`. `gap_days` is the width of that
      interval in days; a wide `gap_days` means a low-confidence closure
      date, and there is deliberately no point estimate.
    """

    model_config = ConfigDict(frozen=True)

    company_id: str
    job_id: str
    # Resolved by `apply_to_postings`; None on a bare `build_intervals` call
    # for a job that has no `postings` row yet.
    posting_id: str | None = None

    title: str | None = None
    team: str | None = None
    location: str | None = None
    description_hash: str | None = None
    url: str | None = None

    first_observed: datetime
    last_seen_open: datetime
    first_seen_absent: datetime | None = None
    reappeared_at: datetime | None = None
    # The absence that brackets the CLOSURE (see module docstring). Equal to
    # `first_seen_absent` unless the job reappeared after that first absence.
    closure_absent_at: datetime | None = None

    censoring: Censoring
    gap_days: float | None = None

    # Provenance: which capture sources ever listed this job, and how many
    # captures did. `archive_only` drives posting-row creation.
    sources: tuple[str, ...] = ()
    capture_count: int = 0

    @property
    def archive_only(self) -> bool:
        """True when no `source='own'` capture ever listed this job."""
        return "own" not in self.sources


@dataclass
class ClosureApplySummary:
    """Counters for one `apply_to_postings` run (all writes are auditable)."""

    intervals: int = 0
    postings_matched: int = 0
    postings_created: int = 0
    ambiguous_job_ids: int = 0
    first_observed_tightened: int = 0
    last_seen_open_advanced: int = 0
    first_seen_absent_tightened: int = 0
    first_seen_absent_blocked: int = 0
    reappeared_at_set: int = 0

    def describe(self) -> str:
        return (
            f"intervals={self.intervals} matched={self.postings_matched} "
            f"created={self.postings_created} ambiguous={self.ambiguous_job_ids} | "
            f"first_observed<-{self.first_observed_tightened} "
            f"last_seen_open<-{self.last_seen_open_advanced} "
            f"first_seen_absent<-{self.first_seen_absent_tightened} "
            f"(blocked={self.first_seen_absent_blocked}) "
            f"reappeared_at<-{self.reappeared_at_set}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_captures(conn: sqlite3.Connection, company_id: str) -> list[Capture]:
    """Load every board capture for `company_id`, oldest first.

    Ordered by `(captured_at, id)`: `captured_at` is a `to_utc_z` string, so
    lexical ordering is chronological ordering (see `rli.models.time`), and
    the `id` tiebreak keeps two captures with an identical timestamp (an own
    capture and an archive capture of the same instant) in a stable,
    reproducible order.
    """
    headers = conn.execute(
        """
        SELECT id, captured_at, source, coverage_status
        FROM board_snapshots
        WHERE company_id = ?
        ORDER BY captured_at, id
        """,
        (company_id,),
    ).fetchall()
    if not headers:
        return []

    jobs_by_snapshot: dict[int, dict[str, JobFacts]] = defaultdict(dict)
    for row in conn.execute(
        """
        SELECT j.board_snapshot_id, j.job_id, j.title, j.team, j.location,
               j.description_hash, j.url
        FROM board_snapshot_jobs AS j
        JOIN board_snapshots AS s ON s.id = j.board_snapshot_id
        WHERE s.company_id = ?
        """,
        (company_id,),
    ):
        jobs_by_snapshot[int(row["board_snapshot_id"])][row["job_id"]] = JobFacts(
            job_id=row["job_id"],
            title=row["title"],
            team=row["team"],
            location=row["location"],
            description_hash=row["description_hash"],
            url=row["url"],
        )

    return [
        Capture(
            snapshot_id=int(row["id"]),
            captured_at=parse_utc(row["captured_at"]),
            source=row["source"],
            coverage_status=row["coverage_status"],
            jobs=jobs_by_snapshot.get(int(row["id"]), {}),
        )
        for row in headers
    ]


def _company_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        row["company_id"]
        for row in conn.execute(
            "SELECT DISTINCT company_id FROM board_snapshots ORDER BY company_id"
        )
    ]


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _first_absence_after(captures: list[Capture], job_id: str, after_index: int) -> int | None:
    """Index of the first `'complete'` capture after `after_index` lacking `job_id`.

    Captures with `coverage_status` `'partial'` or `'gap'` are skipped
    entirely: a company-wide capture failure (or a JS-rendered board that
    yielded no job links) is a coverage hole, and a job missing from one is
    not evidence that the job went away (spec.md §4).
    """
    for index in range(after_index + 1, len(captures)):
        capture = captures[index]
        if capture.is_complete and job_id not in capture.jobs:
            return index
    return None


def _interval_for_job(
    company_id: str, job_id: str, captures: list[Capture], present: list[int]
) -> PostingInterval:
    first_index, last_index = present[0], present[-1]
    facts = captures[last_index].jobs[job_id]

    first_absent_index = _first_absence_after(captures, job_id, first_index)
    reappear_index: int | None = None
    if first_absent_index is not None:
        reappear_index = next((i for i in present if i > first_absent_index), None)

    closure_absent_index = _first_absence_after(captures, job_id, last_index)

    if closure_absent_index is None:
        censoring: Censoring = "right"
        gap_days: float | None = None
        closure_absent_at: datetime | None = None
    else:
        censoring = "interval"
        closure_absent_at = captures[closure_absent_index].captured_at
        gap_days = (
            closure_absent_at - captures[last_index].captured_at
        ).total_seconds() / 86400.0

    return PostingInterval(
        company_id=company_id,
        job_id=job_id,
        title=facts.title,
        team=facts.team,
        location=facts.location,
        description_hash=facts.description_hash,
        url=facts.url,
        first_observed=captures[first_index].captured_at,
        last_seen_open=captures[last_index].captured_at,
        first_seen_absent=(
            None if first_absent_index is None else captures[first_absent_index].captured_at
        ),
        reappeared_at=(None if reappear_index is None else captures[reappear_index].captured_at),
        closure_absent_at=closure_absent_at,
        censoring=censoring,
        gap_days=gap_days,
        sources=tuple(sorted({captures[i].source for i in present})),
        capture_count=len(present),
    )


def build_intervals(
    conn: sqlite3.Connection, company_id: str | None = None
) -> list[PostingInterval]:
    """Derive one `PostingInterval` per job seen in any board capture.

    With `company_id=None`, every company that has at least one
    `board_snapshots` row is processed. Results are sorted by
    `(company_id, job_id)` so callers (and CSV samples) are reproducible.

    `posting_id` is populated for jobs whose `(company_id, job_id)` already
    resolves to a `postings` row via the documented correlation key; it stays
    None for archive-only jobs until `apply_to_postings` creates their row.
    """
    company_ids = [company_id] if company_id is not None else _company_ids(conn)

    intervals: list[PostingInterval] = []
    for cid in company_ids:
        captures = load_captures(conn, cid)
        if not captures:
            continue

        present_by_job: dict[str, list[int]] = defaultdict(list)
        for index, capture in enumerate(captures):
            for job_id in capture.jobs:
                present_by_job[job_id].append(index)

        existing = _existing_postings(conn, cid)
        for job_id in sorted(present_by_job):
            interval = _interval_for_job(cid, job_id, captures, present_by_job[job_id])
            match = existing.get(job_id)
            if match is not None:
                interval = interval.model_copy(update={"posting_id": match[0]})
            intervals.append(interval)

    return intervals


# ---------------------------------------------------------------------------
# Projection onto `postings`
# ---------------------------------------------------------------------------


def _existing_postings(
    conn: sqlite3.Connection, company_id: str
) -> dict[str, tuple[str, sqlite3.Row, int]]:
    """Map `ats_job_id -> (posting_id, row, match_count)` for one company.

    The correlation key is `(company_id, ats_job_id)` (see module docstring).
    `match_count > 1` flags the documented tenant-collision limitation; the
    lowest `posting_id` is the one used.
    """
    rows = conn.execute(
        """
        SELECT posting_id, company_id, ats, ats_tenant_id, ats_job_id, canonical_url,
               title, team, location,
               first_observed, last_seen_open, first_seen_absent, reappeared_at
        FROM postings
        WHERE company_id = ? AND ats_job_id IS NOT NULL
        ORDER BY posting_id
        """,
        (company_id,),
    ).fetchall()

    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["ats_job_id"]] += 1

    out: dict[str, tuple[str, sqlite3.Row, int]] = {}
    for row in rows:
        job_id = row["ats_job_id"]
        if job_id not in out:  # rows are ordered by posting_id: lowest wins
            out[job_id] = (row["posting_id"], row, counts[job_id])
    return out


def _fallback_ats(conn: sqlite3.Connection, company_id: str) -> str:
    """Borrow this company's most common `postings.ats`, else `'other'`.

    `board_snapshots` has no ATS column, so an archive-only posting has no
    knowable ATS. Companies are expected to run one ATS at a time, so the
    company's dominant existing value is the least-wrong guess; `'other'` is
    the honest fallback when the company has no postings at all (and is a
    legal value for the `postings.ats` CHECK constraint).
    """
    row = conn.execute(
        """
        SELECT ats, COUNT(*) AS n
        FROM postings
        WHERE company_id = ?
        GROUP BY ats
        ORDER BY n DESC, ats
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    return row["ats"] if row is not None else "other"


def _create_archive_posting(
    conn: sqlite3.Connection, interval: PostingInterval, now: datetime
) -> str:
    posting_id = f"archive:{interval.company_id}:{interval.job_id}"
    canonical_url = interval.url or ARCHIVE_ONLY_URL_PLACEHOLDER.format(
        company_id=interval.company_id, job_id=interval.job_id
    )
    now_str = to_utc_z(now)
    conn.execute(
        """
        INSERT INTO postings (
            posting_id, company_id, ats, ats_tenant_id, ats_job_id, canonical_url,
            title, team, location, created_at, updated_at,
            first_observed, last_seen_open, first_seen_absent, reappeared_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            posting_id,
            interval.company_id,
            _fallback_ats(conn, interval.company_id),
            interval.job_id,
            canonical_url,
            interval.title,
            interval.team,
            interval.location,
            now_str,
            now_str,
            to_utc_z(interval.first_observed),
            to_utc_z(interval.last_seen_open),
            None if interval.first_seen_absent is None else to_utc_z(interval.first_seen_absent),
            None if interval.reappeared_at is None else to_utc_z(interval.reappeared_at),
        ),
    )
    return posting_id


def _merge_into_posting(
    conn: sqlite3.Connection,
    posting_id: str,
    stored: sqlite3.Row,
    interval: PostingInterval,
    summary: ClosureApplySummary,
    now: datetime,
) -> None:
    """Apply the merge rule below to one existing `postings` row.

    MERGE RULE (the correctness property of this module; archive-derived
    data may only ever make a stored lifecycle MORE precise, never less):

    * `first_observed` <- **the EARLIER** of (stored, derived). An earlier
      first sighting is always strictly more correct: it is a real
      observation of the posting existing at that time, and "first" can only
      move backwards as more history is discovered.
    * `last_seen_open` <- **the LATER** of (stored, derived). A more recent
      open sighting is always strictly more correct for the same reason in
      the other direction.
    * `first_seen_absent` <- **only ever moves EARLIER**, and only under the
      guard below. A tighter (earlier) upper bound narrows the censoring
      interval `(last_seen_open, first_seen_absent]`, so it is more precise;
      a later one would widen it and is discarded.
      - If the stored value is NULL (the posting is own-tracked and still
        open), a derived absence is written ONLY IF the stored
        `last_seen_open` is strictly EARLIER than that derived absence.
        Archive data must never regress an own-confirmed-open posting to
        "absent": if own snapshots saw the posting open at or after the
        moment the archive claims it was missing, the archive capture is the
        less-informed observer (a partial Wayback crawl, a board page that
        did not list every job) and is ignored. This case is counted as
        `first_seen_absent_blocked`.
      - If the stored value is non-NULL, it is replaced only by a strictly
        earlier derived value.
    * `reappeared_at` <- keep an existing non-NULL value untouched. It is
      set from derived data only when currently NULL AND the derived
      reappearance is strictly after the posting's effective
      `first_seen_absent` (i.e. it is a real reappearance relative to the
      absence the row actually records, not merely a later sighting).

    `title`/`team`/`location` are refreshed from the most recent capture
    only when the stored value is NULL, so an own capture's richer metadata
    is never overwritten by a thinner archive scrape.
    """
    updates: dict[str, str | None] = {}

    stored_first_observed = _parse_opt(stored["first_observed"])
    if stored_first_observed is None or interval.first_observed < stored_first_observed:
        if stored_first_observed is not None:
            summary.first_observed_tightened += 1
        updates["first_observed"] = to_utc_z(interval.first_observed)

    stored_last_seen_open = _parse_opt(stored["last_seen_open"])
    if stored_last_seen_open is None or interval.last_seen_open > stored_last_seen_open:
        if stored_last_seen_open is not None:
            summary.last_seen_open_advanced += 1
        updates["last_seen_open"] = to_utc_z(interval.last_seen_open)

    stored_first_seen_absent = _parse_opt(stored["first_seen_absent"])
    derived_absent = interval.first_seen_absent
    effective_absent = stored_first_seen_absent
    if derived_absent is not None:
        if stored_first_seen_absent is None:
            # Guard: never let archive data close a posting that own
            # snapshots show open at or after the claimed absence.
            if stored_last_seen_open is not None and stored_last_seen_open >= derived_absent:
                summary.first_seen_absent_blocked += 1
            else:
                updates["first_seen_absent"] = to_utc_z(derived_absent)
                summary.first_seen_absent_tightened += 1
                effective_absent = derived_absent
        elif derived_absent < stored_first_seen_absent:
            updates["first_seen_absent"] = to_utc_z(derived_absent)
            summary.first_seen_absent_tightened += 1
            effective_absent = derived_absent

    stored_reappeared_at = _parse_opt(stored["reappeared_at"])
    if (
        stored_reappeared_at is None
        and interval.reappeared_at is not None
        and effective_absent is not None
        and interval.reappeared_at > effective_absent
    ):
        updates["reappeared_at"] = to_utc_z(interval.reappeared_at)
        summary.reappeared_at_set += 1

    for column, value in (
        ("title", interval.title),
        ("team", interval.team),
        ("location", interval.location),
    ):
        if stored[column] is None and value is not None:
            updates[column] = value

    if not updates:
        return

    updates["updated_at"] = to_utc_z(now)
    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        # `assignments` is built only from the fixed column names above,
        # never from caller input; every value is bound as a parameter.
        f"UPDATE postings SET {assignments} WHERE posting_id = ?",
        (*updates.values(), posting_id),
    )


def _parse_opt(value: str | None) -> datetime | None:
    return None if value is None else parse_utc(value)


def apply_to_postings(
    conn: sqlite3.Connection,
    company_id: str | None = None,
    *,
    now: datetime | None = None,
) -> ClosureApplySummary:
    """Project derived intervals onto `postings`, creating archive-only rows.

    Existing rows are merged under the rule documented on
    `_merge_into_posting` (archive data may only make a lifecycle more
    precise, never less). Jobs with no `postings` row at all get one created
    per the module docstring's archive-only rules.

    Returns a `ClosureApplySummary` of exactly which fields moved, so a run
    is auditable without diffing the table.
    """
    reference = now or now_utc()
    summary = ClosureApplySummary()

    company_ids = [company_id] if company_id is not None else _company_ids(conn)
    for cid in company_ids:
        existing = _existing_postings(conn, cid)
        for interval in build_intervals(conn, cid):
            summary.intervals += 1
            match = existing.get(interval.job_id)
            if match is None:
                _create_archive_posting(conn, interval, reference)
                summary.postings_created += 1
                continue

            posting_id, stored, match_count = match
            summary.postings_matched += 1
            if match_count > 1:
                summary.ambiguous_job_ids += 1
            _merge_into_posting(conn, posting_id, stored, interval, summary, reference)

    conn.commit()
    return summary
