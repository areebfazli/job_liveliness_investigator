"""Board-history features per company and per posting (PLAN.md M2 #3).

spec.md §4: "Derived history features always carry `history_days` /
`history_coverage`; missing history never means flat hiring." Both models
here carry those two fields unconditionally, and every feature that needs
history to be meaningful degrades to the `rli.models.policy_inputs.UNKNOWN`
sentinel — never to a plausible-looking default — when the history behind it
is too thin. That is the single most important behaviour in this module:
`repost_pattern` must be `UNKNOWN`, not `'none'`, for a company we have
barely observed.

`history_coverage` — exactly how "expected" is defined (GUESSED, documented
because it is the kind of denominator that quietly flatters a dataset):

* The window is `[first capture day, last capture day]` in UTC calendar
  days, over ALL `board_snapshots` for the company (own + archive, any
  `coverage_status`). `history_days` is the span between the first and last
  capture TIMESTAMPS, in days.
* `complete_capture_days` (the numerator) = distinct UTC days in that window
  carrying at least one `coverage_status='complete'` capture. A `'partial'`
  or `'gap'` day is explicitly NOT counted as covered.
* `attempted_days` (the denominator, "expected daily captures") = distinct
  UTC days in the window on which we actually tried: a day with any
  `board_snapshots` row, or with a `capture_attempts` row of
  `source='own'`. Days with neither are days the collector was not running
  at all; counting them would turn `history_coverage` into a measure of our
  own uptime rather than of the data we hold.
  **Archive-side `capture_attempts` are deliberately excluded from the
  denominator**: `rli.archive.backfill` stamps them with the backfill RUN
  time, not the historical capture date, so including them would inject
  spurious "expected" days at the end of every window.
* `history_coverage = complete_capture_days / attempted_days`, and `0.0`
  (never `1.0`) when `attempted_days == 0`. No history is not full coverage.
* Because that denominator is generous to sparse archive-only history (every
  Wayback capture day is by construction both attempted and complete),
  `calendar_coverage = complete_capture_days / calendar_days` is reported
  alongside it: the stricter "one capture per calendar day was the ideal"
  reading, under which a 12-capture year is visibly ~3% covered. Consumers
  that must not be fooled by sparse archive history should read that one.

Other GUESSED / judgment calls:

* **`age_days` is a LOWER bound.** It runs from `first_observed`, which is
  when WE first saw the posting, not when it was published; the true age is
  left-censored. Consumers must not treat it as the posting's age.
* **`long_lived` respects that censoring.** `True` as soon as the lower
  bound `age_days >= thresholds.long_lived_days` (a lower bound above the
  threshold is conclusive). `False` only when the company's history span
  itself reaches back at least `long_lived_days` — i.e. we would have seen
  the posting had it been older. Otherwise `UNKNOWN`: a young-looking
  posting in a two-week-old dataset is not evidence of a young posting.
* **`open_count` trend** is an ordinary least-squares slope in jobs/day over
  the `'complete'` captures in the window, reported alongside the point
  count and the first/last open counts so it can be checked by hand rather
  than trusted. `direction` is `'flat'` when the fitted change across the
  WHOLE window is under one job (scale-free, so it does not need a tuned
  epsilon), else `'up'`/`'down'`. Fewer than two complete captures gives
  `UNKNOWN`, not `0.0`.
* **Median posting lifetime is a RANGE, never a point** (spec.md §4:
  closures are interval-censored). For each closed posting the lifetime
  bracket is `[last_seen_open - first_observed, first_seen_absent -
  first_observed]`; the reported range is the median of the lower bounds and
  the median of the upper bounds. Right-censored (still-open) postings are
  EXCLUDED and counted separately in `right_censored_count`, because
  including their observed-so-far lifetime would bias the median downward —
  the reported range is therefore a median over CLOSED postings only, which
  is itself a documented bias, not a survival estimate.
* **`repost_rate`** = (closed postings that appear as `old_posting_id` in
  `repost_links`) / (closed postings). `None` when the company has no closed
  postings, since 0/0 is not "no reposting".
* **`repost_pattern`** classification, in order: history below
  `thresholds.min_history_days` -> `UNKNOWN`; never observed absent ->
  `'none'`; absent with no link in `repost_links` -> `'none'`; linked, best
  link has an equal description hash (and a title passing
  `repost_title_similarity`) -> `'repeated_unchanged'`; linked with a
  differing description hash -> `'changed'`; linked but the description
  component is unknown on either side -> `UNKNOWN`, because "repeated
  UNCHANGED" is a positive claim about content that a missing hash cannot
  support.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.history.closures import load_captures
from rli.models.policy_inputs import UNKNOWN, RepostPattern, Unknown
from rli.models.time import now_utc, parse_utc, to_utc_z

__all__ = [
    "CompanyHistoryFeatures",
    "CoverageWindow",
    "PostingHistoryFeatures",
    "company_features",
    "coverage_window",
    "posting_features",
]

TrendDirection = Literal["up", "down", "flat"]


class CoverageWindow(BaseModel):
    """How much board history exists for a company, and how dense it is."""

    model_config = ConfigDict(frozen=True)

    company_id: str
    first_capture_at: datetime | None = None
    last_capture_at: datetime | None = None

    # Span between the first and last capture timestamps, in days.
    history_days: float = 0.0
    # complete_capture_days / attempted_days (see module docstring).
    history_coverage: float = 0.0
    # complete_capture_days / calendar_days — the stricter reading.
    calendar_coverage: float = 0.0

    calendar_days: int = 0
    attempted_days: int = 0
    complete_capture_days: int = 0
    # Days we attempted but that produced no 'complete' capture.
    incomplete_capture_days: int = 0
    # Days inside the window on which nothing was attempted at all.
    never_attempted_days: int = 0


class CompanyHistoryFeatures(BaseModel):
    """Per-company board-history features (always carrying coverage)."""

    model_config = ConfigDict(frozen=True)

    company_id: str
    as_of: datetime

    history_days: float
    history_coverage: float
    coverage: CoverageWindow

    # Open-count trend over 'complete' captures in the window.
    open_count_points: int = 0
    open_count_first: int | None = None
    open_count_last: int | None = None
    open_count_slope_per_day: float | Unknown = UNKNOWN
    open_count_direction: TrendDirection | Unknown = UNKNOWN

    closures_last_30_days: int = 0
    closures_last_90_days: int = 0

    # Median lifetime as an interval-censored RANGE over CLOSED postings:
    # (median of lower bounds, median of upper bounds), in days. Never a
    # fabricated point value; None when no posting has closed.
    median_lifetime_days_range: tuple[float, float] | None = None
    closed_count: int = 0
    right_censored_count: int = 0

    # Fraction of closed postings later linked to a repost by
    # rli.history.matching.link_reposts. None when nothing has closed.
    repost_rate: float | None = None
    reposted_count: int = 0


class PostingHistoryFeatures(BaseModel):
    """Per-posting history features (always carrying company coverage)."""

    model_config = ConfigDict(frozen=True)

    posting_id: str
    company_id: str
    as_of: datetime

    history_days: float
    history_coverage: float

    first_observed: datetime | None = None
    last_seen_open: datetime | None = None
    first_seen_absent: datetime | None = None
    reappeared_at: datetime | None = None
    replacement_job_id: str | None = None
    censoring: Literal["right", "interval"] | Unknown = UNKNOWN

    # LOWER bound on the posting's age: measured from when we first saw it,
    # not from when it was published (left-censored).
    age_days: float | Unknown = UNKNOWN
    long_lived: bool | Unknown = UNKNOWN
    repost_pattern: RepostPattern | Unknown = UNKNOWN
    # Plain-language reason for the repost_pattern value, so an UNKNOWN is
    # explicable without re-deriving it.
    repost_pattern_reason: str = ""


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _day(value: datetime) -> date:
    return value.astimezone(UTC).date()


def coverage_window(conn: sqlite3.Connection, company_id: str) -> CoverageWindow:
    """Compute `history_days` / `history_coverage` and their inputs.

    See the module docstring for the exact definition of "expected daily
    captures" (the `history_coverage` denominator).
    """
    rows = conn.execute(
        """
        SELECT captured_at, coverage_status
        FROM board_snapshots
        WHERE company_id = ?
        ORDER BY captured_at
        """,
        (company_id,),
    ).fetchall()
    if not rows:
        return CoverageWindow(company_id=company_id)

    captured = [parse_utc(row["captured_at"]) for row in rows]
    first_capture_at, last_capture_at = captured[0], captured[-1]
    first_day, last_day = _day(first_capture_at), _day(last_capture_at)
    calendar_days = (last_day - first_day).days + 1

    capture_days = {_day(value) for value in captured}
    complete_days = {
        _day(parse_utc(row["captured_at"]))
        for row in rows
        if row["coverage_status"] == "complete"
    }

    # Own-source attempts only: archive attempts are stamped with the
    # backfill run time, not the historical capture date (module docstring).
    attempt_days = {
        _day(parse_utc(row["attempted_at"]))
        for row in conn.execute(
            """
            SELECT attempted_at FROM capture_attempts
            WHERE company_id = ? AND source = 'own' AND attempted_at >= ? AND attempted_at <= ?
            """,
            (company_id, to_utc_z(first_capture_at), to_utc_z(last_capture_at)),
        )
    }

    attempted = capture_days | attempt_days
    attempted_days = len(attempted)
    complete_capture_days = len(complete_days)

    return CoverageWindow(
        company_id=company_id,
        first_capture_at=first_capture_at,
        last_capture_at=last_capture_at,
        history_days=(last_capture_at - first_capture_at).total_seconds() / 86400.0,
        history_coverage=(
            complete_capture_days / attempted_days if attempted_days else 0.0
        ),
        calendar_coverage=complete_capture_days / calendar_days,
        calendar_days=calendar_days,
        attempted_days=attempted_days,
        complete_capture_days=complete_capture_days,
        incomplete_capture_days=attempted_days - complete_capture_days,
        never_attempted_days=calendar_days - attempted_days,
    )


# ---------------------------------------------------------------------------
# Company features
# ---------------------------------------------------------------------------


def _open_count_trend(
    conn: sqlite3.Connection, company_id: str
) -> tuple[int, int | None, int | None, float | Unknown, TrendDirection | Unknown]:
    complete = [c for c in load_captures(conn, company_id) if c.is_complete]
    if not complete:
        return 0, None, None, UNKNOWN, UNKNOWN

    counts = [len(capture.jobs) for capture in complete]
    if len(complete) < 2:
        # One point is a level, not a trend. UNKNOWN, never 0.0 (which would
        # read as a confidently flat board).
        return len(complete), counts[0], counts[-1], UNKNOWN, UNKNOWN

    origin = complete[0].captured_at
    xs = [(capture.captured_at - origin).total_seconds() / 86400.0 for capture in complete]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(counts) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:  # every capture at the same instant
        return len(complete), counts[0], counts[-1], UNKNOWN, UNKNOWN

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, counts, strict=True)) / denominator
    span_days = xs[-1]
    fitted_change = slope * span_days
    if abs(fitted_change) < 1.0:
        direction: TrendDirection = "flat"
    else:
        direction = "up" if slope > 0 else "down"
    return len(complete), counts[0], counts[-1], slope, direction


def company_features(
    conn: sqlite3.Connection,
    cfg: Config,
    company_id: str,
    *,
    now: datetime | None = None,
) -> CompanyHistoryFeatures:
    """Board-history features for one company (see module docstring)."""
    as_of = now or now_utc()
    coverage = coverage_window(conn, company_id)
    points, first_count, last_count, slope, direction = _open_count_trend(conn, company_id)

    postings = conn.execute(
        """
        SELECT posting_id, first_observed, last_seen_open, first_seen_absent
        FROM postings
        WHERE company_id = ?
        """,
        (company_id,),
    ).fetchall()

    closures_30 = 0
    closures_90 = 0
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []
    closed_count = 0
    right_censored = 0
    closed_ids: set[str] = set()

    for row in postings:
        first_seen_absent = row["first_seen_absent"]
        if first_seen_absent is None:
            right_censored += 1
            continue

        closed_count += 1
        closed_ids.add(row["posting_id"])
        absent_at = parse_utc(first_seen_absent)
        age = (as_of - absent_at).total_seconds() / 86400.0
        if 0 <= age <= 30:
            closures_30 += 1
        if 0 <= age <= 90:
            closures_90 += 1

        if row["first_observed"] is None:
            continue
        first_observed = parse_utc(row["first_observed"])
        stored_last_open = row["last_seen_open"]
        last_open = parse_utc(stored_last_open) if stored_last_open else first_observed
        lower_bounds.append((last_open - first_observed).total_seconds() / 86400.0)
        upper_bounds.append((absent_at - first_observed).total_seconds() / 86400.0)

    lifetime_range: tuple[float, float] | None = None
    if lower_bounds and upper_bounds:
        lifetime_range = (median(lower_bounds), median(upper_bounds))

    reposted_count = 0
    if closed_ids:
        linked = {
            row["old_posting_id"]
            for row in conn.execute(
                "SELECT DISTINCT old_posting_id FROM repost_links WHERE company_id = ?",
                (company_id,),
            )
        }
        reposted_count = len(closed_ids & linked)

    return CompanyHistoryFeatures(
        company_id=company_id,
        as_of=as_of,
        history_days=coverage.history_days,
        history_coverage=coverage.history_coverage,
        coverage=coverage,
        open_count_points=points,
        open_count_first=first_count,
        open_count_last=last_count,
        open_count_slope_per_day=slope,
        open_count_direction=direction,
        closures_last_30_days=closures_30,
        closures_last_90_days=closures_90,
        median_lifetime_days_range=lifetime_range,
        closed_count=closed_count,
        right_censored_count=right_censored,
        repost_rate=(reposted_count / closed_count) if closed_count else None,
        reposted_count=reposted_count,
    )


# ---------------------------------------------------------------------------
# Posting features
# ---------------------------------------------------------------------------


def _classify_repost_pattern(
    conn: sqlite3.Connection,
    cfg: Config,
    posting_id: str,
    first_seen_absent: datetime | None,
    history_days: float,
) -> tuple[RepostPattern | Unknown, str]:
    """Classify `repost_pattern`, defaulting to UNKNOWN on thin history."""
    minimum = cfg.thresholds.min_history_days
    if history_days < minimum:
        return (
            UNKNOWN,
            f"history_days={history_days:.2f} < thresholds.min_history_days={minimum}; "
            "insufficient history to have an opinion (missing history is not 'none')",
        )

    if first_seen_absent is None:
        return "none", "posting has never been observed absent in a complete capture"

    row = conn.execute(
        """
        SELECT combined_score, component_scores
        FROM repost_links
        WHERE old_posting_id = ?
        ORDER BY combined_score DESC, new_posting_id
        LIMIT 1
        """,
        (posting_id,),
    ).fetchone()
    if row is None:
        return "none", "posting closed and no repost link met the configured thresholds"

    payload = json.loads(row["component_scores"])
    scores = payload.get("scores", {})
    description = scores.get("description")
    title = scores.get("title")

    if description is None:
        return (
            UNKNOWN,
            "a repost link exists but neither side carries a description_hash, so "
            "'repeated_unchanged' (a positive claim about unchanged content) "
            "cannot be supported",
        )

    title_ok = title is not None and title >= cfg.thresholds.repost_title_similarity
    if description >= cfg.thresholds.repost_description_similarity and title_ok:
        return (
            "repeated_unchanged",
            f"repost link with matching description hash (score={description}) and "
            f"title similarity {title:.3f}",
        )
    return (
        "changed",
        f"repost link with description score={description} / title score="
        f"{'n/a' if title is None else format(title, '.3f')}: material drift",
    )


def posting_features(
    conn: sqlite3.Connection,
    cfg: Config,
    posting_id: str,
    *,
    now: datetime | None = None,
) -> PostingHistoryFeatures:
    """History features for one posting (see module docstring).

    Raises `KeyError` if `posting_id` does not exist — a missing posting is a
    caller bug, not a thin-history case, and must not be silently reported as
    an all-UNKNOWN feature row.
    """
    as_of = now or now_utc()
    row = conn.execute(
        """
        SELECT posting_id, company_id, first_observed, last_seen_open,
               first_seen_absent, reappeared_at, replacement_job_id
        FROM postings
        WHERE posting_id = ?
        """,
        (posting_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no posting row for posting_id={posting_id!r}")

    company_id = row["company_id"]
    coverage = coverage_window(conn, company_id)

    first_observed = parse_utc(row["first_observed"]) if row["first_observed"] else None
    last_seen_open = parse_utc(row["last_seen_open"]) if row["last_seen_open"] else None
    first_seen_absent = parse_utc(row["first_seen_absent"]) if row["first_seen_absent"] else None
    reappeared_at = parse_utc(row["reappeared_at"]) if row["reappeared_at"] else None

    censoring: Literal["right", "interval"] | Unknown
    if first_seen_absent is not None and reappeared_at is None:
        censoring = "interval"
    elif last_seen_open is not None:
        censoring = "right"
    else:
        censoring = UNKNOWN

    age_days: float | Unknown = UNKNOWN
    long_lived: bool | Unknown = UNKNOWN
    if first_observed is not None:
        age_days = (as_of - first_observed).total_seconds() / 86400.0
        long_lived_days = cfg.thresholds.long_lived_days
        if age_days >= long_lived_days:
            # A LOWER bound above the threshold is conclusive.
            long_lived = True
        elif coverage.history_days >= long_lived_days:
            # Our history reaches back far enough that we would have seen an
            # older posting, so "not long-lived" is a real observation.
            long_lived = False
        else:
            # Thin history: the posting may predate our first capture.
            long_lived = UNKNOWN

    pattern, reason = _classify_repost_pattern(
        conn, cfg, posting_id, first_seen_absent, coverage.history_days
    )

    return PostingHistoryFeatures(
        posting_id=posting_id,
        company_id=company_id,
        as_of=as_of,
        history_days=coverage.history_days,
        history_coverage=coverage.history_coverage,
        first_observed=first_observed,
        last_seen_open=last_seen_open,
        first_seen_absent=first_seen_absent,
        reappeared_at=reappeared_at,
        replacement_job_id=row["replacement_job_id"],
        censoring=censoring,
        age_days=age_days,
        long_lived=long_lived,
        repost_pattern=pattern,
        repost_pattern_reason=reason,
    )
