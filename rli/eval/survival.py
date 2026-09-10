"""Censoring-aware posting-behavior curves (spec.md §5/§6; PLAN.md M4).

PLAN.md M4's "lifelines interval-censored closure/repost curves" bullet is
this module. spec.md §5 ("Outcome data" -> "Posting behavior"): "closure/
repost timing from snapshots. Open postings are right-censored; archive-
derived closures are interval-censored (`last_seen_open`, `first_seen_
absent`). Use a library with interval-censored fitters (lifelines) for
evaluation ... Do not use posting survival probability as the v1 action
engine." This module produces exactly that evaluation-only artifact — a
descriptive report of how long postings stay open and how often they get
reposted — and nothing here feeds `rli.policy` or any action decision.

spec.md §4 is equally explicit that the underlying closure data must never
be sharpened into something it is not: "Archive-derived closures are
interval-censored: keep `last_seen_open` and `first_seen_absent`. Do not
invent an exact `closed_at` from sparse captures." Accordingly this module
NEVER derives its own closure timestamps — every duration and interval
bound below traces back to `rli.history.closures.build_intervals`, which is
the one place in the codebase allowed to read `board_snapshots` and produce
`PostingInterval` objects. Re-deriving closures here with parallel SQL would
risk drifting from that single source of truth and would duplicate the
coverage-gap-vs-absence logic (`coverage_status = 'complete'` gating) that
module already gets right.

--------------------------------------------------------------------------
The data model: two arms, one sample
--------------------------------------------------------------------------

`collect_survival_sample` turns every `PostingInterval` for the requested
scope into one `SurvivalObservation`, measured in DAYS from
`first_observed` (the only zero point every posting shares, since a true
"birth" date is unknown per spec.md §4's left-censoring note in
`rli.history.features`):

* `censoring == 'right'` (still open as of the last capture): the observed
  lower bound is `last_seen_open - first_observed`; the true closure time
  lies somewhere in the unobserved future, `[lower, +inf)`.
* `censoring == 'interval'` (archive-derived closure): the true closure
  time lies in `(last_seen_open - first_observed, closure_absent_at -
  first_observed]`. Both bounds are kept; no point estimate is ever
  invented.

From that ONE sample, two different fits are produced, because spec.md §5
asks for both an "honest" arm and a directly-comparable one:

1. **`fit_right_censored`** — an ordinary `KaplanMeierFitter().fit(...)`
   over a single duration per posting. Since only one number per
   observation is possible here, a still-interval-censored (closed)
   posting's "event time" is taken as the interval's UPPER bound
   (`closure_absent_at - first_observed`), i.e. "known closed by this
   point" — clearly documented rather than silently assumed, because it
   **biases the resulting median upward** (every closed posting is treated
   as if it survived all the way to the least-informative end of its
   closure bracket). This arm exists because spec.md §5 says OWN snapshots
   are right-censored on their own, and because a plain KM curve is the
   thing most readers expect to see; `fit_interval_censored` below is the
   honest fit for archive-derived closures and should be preferred for any
   real claim about typical posting lifetime.

2. **`fit_interval_censored`** — `KaplanMeierFitter().fit_interval_
   censoring(lower_bound, upper_bound)` (the Turnbull/NPMLE estimator),
   which needs no such event-time fiction: still-open postings pass
   `upper_bound = numpy.inf` (exactly what lifelines' interval-censoring
   API expects for "no upper bound is known yet") and closed postings pass
   their real `(lower, upper)` bracket.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **Zero-width intervals are treated as EXACT observations, not widened by
  an epsilon.** A zero-width bracket (`closure_absent_at == last_seen_
  open`) can only arise from two captures sharing an identical
  `captured_at` timestamp (`rli.history.closures.load_captures` documents
  this as a real, if rare, tie — an own capture and an archive capture of
  the same instant). lifelines' own contract for `fit_interval_censoring`
  is `lower_bound == upper_bound` <=> `event_observed = True`; passing the
  bounds through unchanged and letting lifelines derive `event_observed`
  itself (rather than the caller widening one side by a made-up epsilon)
  is both simpler and doesn't fabricate precision the data does not have.
  This was verified empirically against lifelines 0.30.3's NPMLE
  implementation (`lifelines.fitters.npmle.npmle`), which handles an exact
  atom at a single time point without error; `fit_interval_censored` also
  wraps the call in `try/except` regardless, so a future lifelines version
  that DOES choke on this cannot crash `behavior_report`.
* **No confidence interval for the interval-censored (Turnbull) median.**
  lifelines 0.30.3's `KaplanMeierFitter.fit_interval_censoring` computes
  `survival_function_` and `median_survival_time_` but leaves
  `confidence_interval_` unset (the CI computation in
  `lifelines/fitters/kaplan_meier_fitter.py` is present in source but
  commented out for this code path) — accessing it raises `AttributeError`.
  `fit_interval_censored`'s `SurvivalCurve.median_ci_lower/upper` are
  therefore always `None`, with `note` explaining why; this is a library
  limitation, not a bug in this module, and is called out again in the
  Markdown report's Limitations section.
* **The Turnbull median can itself be an interval, not a point.** When the
  data does not pin the median survival time down uniquely, lifelines
  returns `median_survival_time_` as a two-column DataFrame
  (`NPMLE_estimate_upper`, `NPMLE_estimate_lower`) describing the
  ambiguity range rather than a scalar. `SurvivalCurve.median_days` reports
  the MEAN of those two bounds as a single point summary — a documented
  simplification, not a claim that the median is known exactly to that
  precision.
* **`survival_at` horizons are `None` beyond the observed support, by this
  module's own definition of "observed support" — not lifelines'
  extrapolation.** lifelines will happily return a flat-extrapolated value
  for a time past the last observed duration (holding the last known
  survival probability constant forever). That extrapolation encodes no
  real information for a time we never observed, so this module instead
  reports `None` for any horizon greater than the largest FINITE duration
  that went into the fit, treating "no data past here" as "no opinion past
  here".
* **`SurvivalCurve.kind`, not two separate report types**, keeps the two
  arms structurally identical (same fields, same horizons) so the Markdown
  writer and any downstream consumer render them with one code path and a
  reader can directly compare the honest interval-censored curve against
  the upward-biased right-censored one.
* **`CoverageSummary` scopes to "companies in the sample".** When
  `company_id` is given, that single company is always included (even if
  it produced an empty sample — e.g. it has snapshots for jobs that
  reappeared and were never conclusively closed, or genuinely has none),
  so a company-scoped report always shows the coverage its scope claims.
  When unscoped, the company set is exactly the set of companies with at
  least one durable observation in the sample; a company whose EVERY board
  capture never listed a single job would be invisible to `build_intervals`
  (no job -> no interval) and is therefore also invisible here — an
  accepted, documented edge case rather than a hidden one.
* **`RepostSummary` queries `postings` / `repost_links` directly**, the
  same pattern `rli.history.features.company_features` already uses for
  `repost_rate`, rather than trying to route repost linkage through
  `SurvivalObservation` (which only carries day-offsets, not absolute
  timestamps, and has no reason to). `repost_rate` is `None` (never `0.0`)
  when there are no closed postings in scope, because 0/0 is not "no
  reposting" (spec.md §4's identical judgment call in `company_features`).
* **Nothing here ever raises out of `behavior_report` / `write_behavior_
  report`.** The real database currently has very few (possibly zero)
  archive-derived closure events; every fit function is defensively
  wrapped, `inf`/`nan` are converted to `None` at every boundary (a
  `SurvivalCurve.note` says so when it happens), and `write_behavior_report`
  always produces a readable file, including for an entirely empty
  database.
"""

from __future__ import annotations

import math
import sqlite3
import warnings
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Literal

import numpy as np
from lifelines import KaplanMeierFitter
from lifelines.utils import median_survival_times
from pydantic import BaseModel, ConfigDict, Field

from rli.config import Config
from rli.history.closures import Censoring, build_intervals
from rli.history.features import coverage_window
from rli.models.time import now_utc, parse_utc, to_utc_z

__all__ = [
    "BehaviorReport",
    "CoverageSummary",
    "RepostSummary",
    "SurvivalCurve",
    "SurvivalSample",
    "behavior_report",
    "collect_survival_sample",
    "fit_interval_censored",
    "fit_right_censored",
    "write_behavior_report",
]

# Horizons (days) at which every SurvivalCurve reports a survival
# probability. Fixed rather than configurable: these are report-shape
# constants, not tunable policy thresholds (contrast `cfg.thresholds`).
HORIZON_DAYS: tuple[int, ...] = (7, 14, 30, 60, 90)


# ---------------------------------------------------------------------------
# 1. Sample collection
# ---------------------------------------------------------------------------


class SurvivalObservation(BaseModel):
    """One posting's duration data, in days from `first_observed`.

    Not part of the module's public `__all__` (the CLI contract fixes only
    `SurvivalSample` and the report/fit functions), but a plain frozen model
    rather than a tuple-of-floats so provenance (`archive_only`, `gap_days`,
    identifiers) travels with every observation for auditability.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    company_id: str
    job_id: str
    posting_id: str | None = None

    censoring: Censoring
    # Lower bound in days: last_seen_open - first_observed. Always finite
    # and >= 0 by construction (validated in collect_survival_sample).
    lower_days: float
    # Upper bound in days for an interval-censored observation
    # (closure_absent_at - first_observed); None for a right-censored
    # observation, meaning "no upper bound observed" (lifelines' +inf).
    upper_days: float | None = None

    archive_only: bool
    gap_days: float | None = None


class SurvivalSample(BaseModel):
    """The duration data behind both survival fits, plus drop accounting.

    Built once by `collect_survival_sample` and consumed by both
    `fit_right_censored` and `fit_interval_censored`, so the two arms are
    guaranteed to be fit over identical underlying data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    company_id: str | None = None
    now: datetime

    observations: tuple[SurvivalObservation, ...] = ()

    n_right_censored: int = 0
    n_interval_censored: int = 0
    n_archive_only: int = 0
    n_own_observed: int = 0
    # Interval-censored observations whose bounds collapsed to a single
    # point (see module docstring's zero-width judgment call). Informational
    # only; these observations are NOT dropped, just counted.
    n_zero_width_exact: int = 0

    # Anything build_intervals produced that could not be turned into a
    # valid duration is dropped here, never silently. In the ordinary case
    # both are 0: build_intervals's own chronological ordering guarantees
    # non-negative, finite gaps.
    dropped_non_finite: int = 0
    dropped_negative: int = 0

    @property
    def total(self) -> int:
        return len(self.observations)


def collect_survival_sample(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    company_id: str | None = None,
) -> SurvivalSample:
    """Build duration data from `rli.history.closures.build_intervals`.

    Every `PostingInterval` in scope becomes one `SurvivalObservation`
    unless its bounds are non-finite or negative, in which case it is
    dropped and counted (never silently) in `dropped_non_finite` /
    `dropped_negative`. See the module docstring for the day-offset
    convention and the zero-width-interval judgment call.
    """
    as_of = now or now_utc()
    intervals = build_intervals(conn, company_id)

    observations: list[SurvivalObservation] = []
    n_right = n_interval = n_archive_only = n_own = n_zero_width = 0
    dropped_non_finite = dropped_negative = 0

    for interval in intervals:
        lower = (interval.last_seen_open - interval.first_observed).total_seconds() / 86400.0

        upper: float | None
        if interval.censoring == "right":
            upper = None
        else:
            if interval.closure_absent_at is None:
                # Contract violation from build_intervals (should be
                # unreachable): an 'interval' censoring with no absence
                # timestamp cannot be turned into an upper bound.
                dropped_non_finite += 1
                continue
            upper = (interval.closure_absent_at - interval.first_observed).total_seconds() / 86400.0

        if not math.isfinite(lower) or (upper is not None and not math.isfinite(upper)):
            dropped_non_finite += 1
            continue
        if lower < 0 or (upper is not None and upper < 0):
            dropped_negative += 1
            continue
        if upper is not None and upper < lower:
            # An inverted bracket is a data-quality problem, not a duration.
            dropped_negative += 1
            continue

        if upper is not None and upper == lower:
            n_zero_width += 1

        if interval.censoring == "right":
            n_right += 1
        else:
            n_interval += 1
        if interval.archive_only:
            n_archive_only += 1
        else:
            n_own += 1

        observations.append(
            SurvivalObservation(
                company_id=interval.company_id,
                job_id=interval.job_id,
                posting_id=interval.posting_id,
                censoring=interval.censoring,
                lower_days=lower,
                upper_days=upper,
                archive_only=interval.archive_only,
                gap_days=interval.gap_days,
            )
        )

    return SurvivalSample(
        company_id=company_id,
        now=as_of,
        observations=tuple(observations),
        n_right_censored=n_right,
        n_interval_censored=n_interval,
        n_archive_only=n_archive_only,
        n_own_observed=n_own,
        n_zero_width_exact=n_zero_width,
        dropped_non_finite=dropped_non_finite,
        dropped_negative=dropped_negative,
    )


# ---------------------------------------------------------------------------
# 2. Survival curves
# ---------------------------------------------------------------------------


class SurvivalCurve(BaseModel):
    """One fitted survival curve (either arm; see module docstring).

    `survival_at` is keyed by the `HORIZON_DAYS` horizons; a horizon beyond
    the observed support is `None`, never extrapolated. `median_days` /
    `median_ci_lower` / `median_ci_upper` are `None` whenever lifelines
    cannot resolve them to a finite value (unreachable median, missing CI,
    or a fit failure) — `inf`/`nan` are never allowed to leave this model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["right_censored", "interval_censored"]
    n_observations: int = 0
    n_events: int = 0
    n_censored: int = 0
    median_days: float | None = None
    median_ci_lower: float | None = None
    median_ci_upper: float | None = None
    survival_at: dict[int, float | None] = Field(default_factory=dict)
    note: str = ""

    def describe(self) -> str:
        median_text = "n/a" if self.median_days is None else f"{self.median_days:.1f}d"
        ci_text = (
            ""
            if self.median_ci_lower is None and self.median_ci_upper is None
            else f" (95% CI {self._fmt(self.median_ci_lower)}-{self._fmt(self.median_ci_upper)})"
        )
        text = (
            f"{self.kind}: n={self.n_observations} events={self.n_events} "
            f"censored={self.n_censored} median={median_text}{ci_text}"
        )
        return f"{text} [{self.note}]" if self.note else text

    @staticmethod
    def _fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f}d"

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _finite_or_none(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _finite_mean_or_none(value: object) -> float | None:
    """Like `_finite_or_none`, but averages a pandas-shaped multi-value result.

    lifelines' Turnbull fitter reports `median_survival_time_` /
    `predict(...)` as a two-column (upper/lower NPMLE bound) result when the
    estimate is not uniquely identified; see module docstring.
    """
    if hasattr(value, "to_numpy"):
        try:
            arr = np.asarray(value.to_numpy(), dtype=float).reshape(-1)
        except (TypeError, ValueError):
            return None
        if arr.size == 0:
            return None
        mean_value = float(np.mean(arr))
        return mean_value if math.isfinite(mean_value) else None
    return _finite_or_none(value)


def _clip_probability(value: float) -> float:
    return min(1.0, max(0.0, value))


def _median_ci_right(kmf: KaplanMeierFitter) -> tuple[float | None, float | None, str]:
    try:
        ci_df = kmf.confidence_interval_
        result = median_survival_times(ci_df)
        lower = float(result.iloc[0, 0])
        upper = float(result.iloc[0, 1])
    except Exception as exc:  # pragma: no cover - defensive; lifelines internals
        return None, None, f"median CI unavailable ({exc.__class__.__name__}: {exc})"
    return (
        lower if math.isfinite(lower) else None,
        upper if math.isfinite(upper) else None,
        "",
    )


def _predict_right(kmf: KaplanMeierFitter, horizon: int, max_support: float) -> float | None:
    if horizon > max_support:
        return None
    try:
        value = float(kmf.survival_function_at_times([horizon]).iloc[0])
    except Exception:  # pragma: no cover - defensive
        return None
    return _clip_probability(value) if math.isfinite(value) else None


def _predict_interval(
    kmf: KaplanMeierFitter, horizon: int, max_support: float, min_support: float
) -> float | None:
    if horizon > max_support:
        return None
    if horizon < min_support:
        # Before the earliest possible failure time in the sample, survival
        # is trivially 1.0 (nobody CAN have closed yet) — this is within
        # the observed support, not extrapolation. lifelines' `predict`
        # returns NaN here instead of 1.0 (it only interpolates between
        # Turnbull breakpoints), so this case is handled directly rather
        # than routed through `kmf.predict`.
        return 1.0
    try:
        predicted = kmf.predict(float(horizon))
    except Exception:  # pragma: no cover - defensive
        return None
    value = _finite_mean_or_none(predicted)
    return _clip_probability(value) if value is not None else None


def _empty_curve(kind: Literal["right_censored", "interval_censored"], note: str) -> SurvivalCurve:
    return SurvivalCurve(
        kind=kind,
        n_observations=0,
        n_events=0,
        n_censored=0,
        survival_at=dict.fromkeys(HORIZON_DAYS),
        note=note,
    )


def fit_right_censored(sample: SurvivalSample) -> SurvivalCurve:
    """`KaplanMeierFitter().fit(durations, event_observed)` over `sample`.

    Every observation contributes exactly one duration (see module
    docstring's event-time judgment call for the interval-censored arm).
    Never raises: an empty sample or a lifelines failure degrades to a
    `SurvivalCurve` with `median_days=None` and an explanatory `note`.
    """
    durations: list[float] = []
    events: list[int] = []
    for obs in sample.observations:
        if obs.censoring == "right":
            durations.append(obs.lower_days)
            events.append(0)
        else:
            assert obs.upper_days is not None  # guaranteed by collect_survival_sample
            durations.append(obs.upper_days)
            events.append(1)

    n = len(durations)
    n_events = sum(events)
    n_censored = n - n_events
    if n == 0:
        return _empty_curve(
            "right_censored", "empty sample: no right- or interval-censored postings to fit"
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # lifelines' own tiny-n statistics warnings
            kmf = KaplanMeierFitter()
            kmf.fit(durations, event_observed=events)
    except Exception as exc:  # pragma: no cover - defensive; lifelines internals
        return SurvivalCurve(
            kind="right_censored",
            n_observations=n,
            n_events=n_events,
            n_censored=n_censored,
            survival_at=dict.fromkeys(HORIZON_DAYS),
            note=f"KaplanMeierFitter.fit failed ({exc.__class__.__name__}: {exc})",
        )

    median_days = _finite_or_none(kmf.median_survival_time_)
    ci_lower, ci_upper, ci_note = _median_ci_right(kmf)
    max_support = float(np.max(kmf.timeline)) if len(kmf.timeline) else 0.0
    survival_at = {h: _predict_right(kmf, h, max_support) for h in HORIZON_DAYS}

    notes = [n for n in (ci_note,) if n]
    if n_events == 0:
        notes.append(
            "no closure events observed (spec.md §5: open postings are right-censored); "
            "median and survival curve reflect right-censoring only"
        )
    if median_days is None and n_events > 0:
        notes.append("median survival time not reached within the observed data")

    return SurvivalCurve(
        kind="right_censored",
        n_observations=n,
        n_events=n_events,
        n_censored=n_censored,
        median_days=median_days,
        median_ci_lower=ci_lower,
        median_ci_upper=ci_upper,
        survival_at=survival_at,
        note="; ".join(notes),
    )


def fit_interval_censored(sample: SurvivalSample) -> SurvivalCurve:
    """`KaplanMeierFitter().fit_interval_censoring(lower, upper)` (Turnbull).

    The honest fit for archive-derived closures (spec.md §5). Still-open
    postings pass `upper_bound = numpy.inf`, exactly as lifelines expects.
    Never raises: see module docstring for the zero-width-interval and
    missing-CI judgment calls, and `fit_right_censored`'s docstring for the
    shared robustness contract.
    """
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []
    for obs in sample.observations:
        lower_bounds.append(obs.lower_days)
        upper_bounds.append(math.inf if obs.upper_days is None else obs.upper_days)

    n = len(lower_bounds)
    # NOTE this is a domain-level event/censored split (closed vs. still
    # open), NOT lifelines' own `event_observed = lower == upper` notion
    # (exact vs. bracketed time). Under lifelines' definition almost every
    # archive-derived closure counts as "censored" too, because we know
    # THAT it closed but not the exact instant — that would make n_events
    # near-zero even when every posting in the sample has in fact closed,
    # which is not what a reader means by "closure events observed".
    n_events = sum(1 for hi in upper_bounds if math.isfinite(hi))
    n_censored = n - n_events
    if n == 0:
        return _empty_curve("interval_censored", "empty sample: no intervals to fit")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            kmf = KaplanMeierFitter()
            kmf.fit_interval_censoring(lower_bounds, upper_bounds)
    except Exception as exc:  # pragma: no cover - defensive; lifelines internals
        return SurvivalCurve(
            kind="interval_censored",
            n_observations=n,
            n_events=n_events,
            n_censored=n_censored,
            survival_at=dict.fromkeys(HORIZON_DAYS),
            note=f"Turnbull fit failed ({exc.__class__.__name__}: {exc})",
        )

    median_days = _finite_mean_or_none(kmf.median_survival_time_)
    # lifelines 0.30.3 does not compute confidence_interval_ for
    # fit_interval_censoring (see module docstring) — always None here.
    ci_note = (
        "median CI not available: lifelines 0.30.3's Turnbull/NPMLE estimator does not "
        "compute a confidence interval for fit_interval_censoring"
    )

    finite_times = [float(t) for t in kmf.timeline if math.isfinite(t)]
    max_support = max(finite_times) if finite_times else 0.0
    min_support = min(finite_times) if finite_times else 0.0
    survival_at = {h: _predict_interval(kmf, h, max_support, min_support) for h in HORIZON_DAYS}

    notes = [ci_note]
    if n_events == 0:
        notes.append(
            "no closure events observed; curve reflects right-censored (still-open) postings only"
        )

    return SurvivalCurve(
        kind="interval_censored",
        n_observations=n,
        n_events=n_events,
        n_censored=n_censored,
        median_days=median_days,
        median_ci_lower=None,
        median_ci_upper=None,
        survival_at=survival_at,
        note="; ".join(notes),
    )


# ---------------------------------------------------------------------------
# 3. Coverage and repost summaries
# ---------------------------------------------------------------------------


class CoverageSummary(BaseModel):
    """Aggregated `rli.history.features.coverage_window` over the sample's companies.

    spec.md §4: "Derived history features always carry `history_days` /
    `history_coverage`." `company_count == 0` means no company had usable
    history in scope; every aggregate is `None` in that case rather than a
    fabricated `0.0`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    company_count: int = 0
    history_days_min: float | None = None
    history_days_median: float | None = None
    history_days_max: float | None = None
    mean_history_coverage: float | None = None
    mean_calendar_coverage: float | None = None
    companies_below_min_history: int = 0
    min_history_days_threshold: float = 0.0

    def describe(self) -> str:
        if self.company_count == 0:
            return "coverage: no companies in scope"
        return (
            f"coverage: {self.company_count} company/companies, history_days "
            f"min/median/max={self.history_days_min:.1f}/{self.history_days_median:.1f}/"
            f"{self.history_days_max:.1f}, mean history_coverage="
            f"{self.mean_history_coverage:.1%}, mean calendar_coverage="
            f"{self.mean_calendar_coverage:.1%}, below min_history_days threshold "
            f"({self.min_history_days_threshold:.0f}d): {self.companies_below_min_history}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _coverage_summary(
    conn: sqlite3.Connection, cfg: Config, sample: SurvivalSample
) -> CoverageSummary:
    threshold = float(cfg.thresholds.min_history_days)
    company_ids = sorted({obs.company_id for obs in sample.observations})
    if not company_ids and sample.company_id is not None:
        company_ids = [sample.company_id]
    if not company_ids:
        return CoverageSummary(min_history_days_threshold=threshold)

    windows = [coverage_window(conn, cid) for cid in company_ids]
    history_days = [w.history_days for w in windows]
    history_coverage = [w.history_coverage for w in windows]
    calendar_coverage = [w.calendar_coverage for w in windows]

    return CoverageSummary(
        company_count=len(windows),
        history_days_min=min(history_days),
        history_days_median=median(history_days),
        history_days_max=max(history_days),
        mean_history_coverage=sum(history_coverage) / len(history_coverage),
        mean_calendar_coverage=sum(calendar_coverage) / len(calendar_coverage),
        companies_below_min_history=sum(1 for d in history_days if d < threshold),
        min_history_days_threshold=threshold,
    )


class RepostSummary(BaseModel):
    """Repost rate and observed re-listing gap for closed postings in scope.

    `repost_rate` and `median_days_to_repost` are `None` (never `0.0`) when
    their denominator is zero — see module docstring's repeat of
    `rli.history.features.company_features`'s 0/0 judgment call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    closed_postings: int = 0
    reposted_count: int = 0
    repost_rate: float | None = None
    repost_links_total: int = 0
    median_days_to_repost: float | None = None
    median_days_to_repost_n: int = 0

    def describe(self) -> str:
        rate = "n/a (no closed postings)" if self.repost_rate is None else f"{self.repost_rate:.1%}"
        gap = (
            "n/a"
            if self.median_days_to_repost is None
            else f"{self.median_days_to_repost:.1f}d (n={self.median_days_to_repost_n})"
        )
        return (
            f"reposts: {self.reposted_count}/{self.closed_postings} closed postings "
            f"reposted ({rate}); {self.repost_links_total} repost_links row(s) in scope; "
            f"median days to repost: {gap}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _repost_summary(conn: sqlite3.Connection, sample: SurvivalSample) -> RepostSummary:
    company_ids = sorted({obs.company_id for obs in sample.observations})
    if not company_ids and sample.company_id is not None:
        company_ids = [sample.company_id]
    if not company_ids:
        return RepostSummary()

    placeholders = ",".join("?" for _ in company_ids)
    closed_rows = conn.execute(
        f"""
        SELECT posting_id, first_seen_absent
        FROM postings
        WHERE company_id IN ({placeholders}) AND first_seen_absent IS NOT NULL
        """,
        company_ids,
    ).fetchall()
    total_links = conn.execute(
        f"SELECT COUNT(*) AS n FROM repost_links WHERE company_id IN ({placeholders})",
        company_ids,
    ).fetchone()["n"]

    closed_count = len(closed_rows)
    if closed_count == 0:
        return RepostSummary(closed_postings=0, repost_links_total=total_links)

    closed_by_id = {row["posting_id"]: row for row in closed_rows}
    posting_ids = list(closed_by_id)
    link_placeholders = ",".join("?" for _ in posting_ids)
    link_rows = conn.execute(
        f"""
        SELECT old_posting_id, new_posting_id FROM repost_links
        WHERE old_posting_id IN ({link_placeholders})
        """,
        posting_ids,
    ).fetchall()
    linked_old_ids = {row["old_posting_id"] for row in link_rows}

    gap_days: list[float] = []
    for row in link_rows:
        old_row = closed_by_id.get(row["old_posting_id"])
        if old_row is None:
            continue
        new_row = conn.execute(
            "SELECT first_observed FROM postings WHERE posting_id = ?", (row["new_posting_id"],)
        ).fetchone()
        if new_row is None or new_row["first_observed"] is None:
            continue
        absent_at = parse_utc(old_row["first_seen_absent"])
        new_first_observed = parse_utc(new_row["first_observed"])
        gap = (new_first_observed - absent_at).total_seconds() / 86400.0
        if math.isfinite(gap):
            gap_days.append(gap)

    reposted_count = len(linked_old_ids)
    return RepostSummary(
        closed_postings=closed_count,
        reposted_count=reposted_count,
        repost_rate=reposted_count / closed_count,
        repost_links_total=total_links,
        median_days_to_repost=median(gap_days) if gap_days else None,
        median_days_to_repost_n=len(gap_days),
    )


# ---------------------------------------------------------------------------
# 4. BehaviorReport
# ---------------------------------------------------------------------------


class BehaviorReport(BaseModel):
    """The complete posting-behavior report for one scope (PLAN.md M4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: str
    now: datetime
    company_id: str | None = None

    total_intervals: int = 0
    closed_count: int = 0
    right_censored_count: int = 0
    archive_only_count: int = 0
    own_observed_count: int = 0
    dropped_non_finite: int = 0
    dropped_negative: int = 0

    right_censored_curve: SurvivalCurve
    interval_censored_curve: SurvivalCurve
    coverage: CoverageSummary
    reposts: RepostSummary

    def describe(self) -> str:
        scope = self.company_id or "(all companies)"
        lines = [
            f"behavior report: scope={scope} generated_at={self.generated_at}",
            f"  sample: total={self.total_intervals} closed={self.closed_count} "
            f"right_censored={self.right_censored_count} "
            f"archive_only={self.archive_only_count} own_observed={self.own_observed_count} "
            f"dropped(non_finite={self.dropped_non_finite}, negative={self.dropped_negative})",
            f"  {self.right_censored_curve.describe()}",
            f"  {self.interval_censored_curve.describe()}",
            f"  {self.coverage.describe()}",
            f"  {self.reposts.describe()}",
        ]
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def behavior_report(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    now: datetime | None = None,
    company_id: str | None = None,
) -> BehaviorReport:
    """Build the full posting-behavior report for `company_id` (or all companies).

    Never raises: every stage degrades gracefully on an empty sample, a
    zero-event sample, or a single-observation sample (see module
    docstring). Do not use the resulting curves as a v1 action-policy input
    (spec.md §5).
    """
    as_of = now or now_utc()
    sample = collect_survival_sample(conn, now=as_of, company_id=company_id)

    right_curve = fit_right_censored(sample)
    interval_curve = fit_interval_censored(sample)
    coverage = _coverage_summary(conn, cfg, sample)
    reposts = _repost_summary(conn, sample)

    return BehaviorReport(
        generated_at=to_utc_z(now_utc()),
        now=as_of,
        company_id=company_id,
        total_intervals=sample.total,
        closed_count=sample.n_interval_censored,
        right_censored_count=sample.n_right_censored,
        archive_only_count=sample.n_archive_only,
        own_observed_count=sample.n_own_observed,
        dropped_non_finite=sample.dropped_non_finite,
        dropped_negative=sample.dropped_negative,
        right_censored_curve=right_curve,
        interval_censored_curve=interval_curve,
        coverage=coverage,
        reposts=reposts,
    )


# ---------------------------------------------------------------------------
# 5. Markdown report writer
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _days(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _curve_table(curve: SurvivalCurve) -> str:
    rows = [
        ["n_observations", str(curve.n_observations)],
        ["n_events", str(curve.n_events)],
        ["n_censored", str(curve.n_censored)],
        ["median_days", _days(curve.median_days)],
        [
            "median 95% CI",
            f"{_days(curve.median_ci_lower)} - {_days(curve.median_ci_upper)}",
        ],
    ]
    for horizon in HORIZON_DAYS:
        rows.append([f"survival_at_{horizon}d", _pct(curve.survival_at.get(horizon))])
    if curve.note:
        rows.append(["note", curve.note])
    return _markdown_table(["metric", "value"], rows)


def write_behavior_report(path: str | Path, report: BehaviorReport) -> Path:
    """Write `report` as Markdown to `path`, creating parent directories as needed.

    Mirrors `rli.eval.baseline.write_baseline_report`'s "create parent
    directories, always produce a valid file" convention. Never raises for
    any `BehaviorReport`, including the all-zero report from an empty
    database.
    """
    destination = Path(path)
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)

    scope = report.company_id or "(all companies)"
    coverage = report.coverage
    reposts = report.reposts

    coverage_table = _markdown_table(
        ["metric", "value"],
        [
            ["companies in scope", str(coverage.company_count)],
            [
                "history_days (min/median/max)",
                (
                    "n/a"
                    if coverage.company_count == 0
                    else f"{_days(coverage.history_days_min)} / "
                    f"{_days(coverage.history_days_median)} / {_days(coverage.history_days_max)}"
                ),
            ],
            ["mean history_coverage", _pct(coverage.mean_history_coverage)],
            ["mean calendar_coverage", _pct(coverage.mean_calendar_coverage)],
            [
                f"companies below min_history_days ({coverage.min_history_days_threshold:.0f}d)",
                str(coverage.companies_below_min_history),
            ],
        ],
    )

    repost_table = _markdown_table(
        ["metric", "value"],
        [
            ["closed postings in scope", str(reposts.closed_postings)],
            ["reposted (linked in repost_links)", str(reposts.reposted_count)],
            ["repost rate", _pct(reposts.repost_rate)],
            ["repost_links rows in scope", str(reposts.repost_links_total)],
            [
                "median days: first_seen_absent -> replacement first_observed",
                (
                    "n/a"
                    if reposts.median_days_to_repost is None
                    else (
                        f"{reposts.median_days_to_repost:.1f} (n={reposts.median_days_to_repost_n})"
                    )
                ),
            ],
        ],
    )

    lines = [
        "# Posting-behavior report",
        "",
        f"Scope: `{scope}` · Generated: `{report.generated_at}` · As of: `{to_utc_z(report.now)}`",
        "",
        "## Sample accounting",
        "",
        _markdown_table(
            ["metric", "value"],
            [
                ["total intervals (postings/jobs observed)", str(report.total_intervals)],
                ["closed (interval-censored)", str(report.closed_count)],
                ["still open (right-censored)", str(report.right_censored_count)],
                ["archive-only (never seen by our own captures)", str(report.archive_only_count)],
                ["own-observed", str(report.own_observed_count)],
                ["dropped: non-finite bounds", str(report.dropped_non_finite)],
                ["dropped: negative/inverted bounds", str(report.dropped_negative)],
            ],
        ),
        "",
        "## Right-censored curve (KaplanMeierFitter.fit)",
        "",
        _curve_table(report.right_censored_curve),
        "",
        "## Interval-censored curve (KaplanMeierFitter.fit_interval_censoring — Turnbull)",
        "",
        _curve_table(report.interval_censored_curve),
        "",
        "## Coverage (rli.history.features.coverage_window)",
        "",
        coverage_table,
        "",
        "## Reposting",
        "",
        repost_table,
        "",
        "## Limitations and interpretation",
        "",
        "- **Closures are interval-censored; no exact `closed_at` is ever invented.** "
        "spec.md §4: archive-derived closures keep only `last_seen_open` and "
        "`first_seen_absent` — the true closure time is bracketed, never pinpointed, "
        "and both curves above are fit from those brackets (or, for the right-censored "
        "arm, the bracket's upper bound) rather than from any fabricated point estimate.",
        "- **The right-censored arm's median is biased UPWARD.** Its event time for a "
        "closed posting is the interval's upper bound (`closure_absent_at`), i.e. "
        '"known closed by this point" — the least-informative end of the bracket. '
        "Prefer the interval-censored (Turnbull) curve for any real claim about "
        "typical posting lifetime; the right-censored arm exists because spec.md §5 "
        "separately describes own-snapshot closures as right-censored on their own.",
        "- **`history_coverage`'s denominator is our own attempt days**, not calendar "
        "days — see `rli.history.features.coverage_window`. A company with sparse "
        "attempts can show high `history_coverage` while having very little actual "
        "calendar span observed; `calendar_coverage` in the table above is the "
        "stricter reading.",
        "- **Do not use posting survival probability as the v1 action engine** "
        "(spec.md §5, verbatim). This report is an evaluation artifact only; nothing "
        "in `rli.policy` reads `SurvivalCurve` or `BehaviorReport`.",
        "- **Sparse archive coverage makes these curves indicative, not authoritative.** "
        "With few closure events (see the sample-accounting table above), both curves "
        "can be dominated by a handful of observations; treat medians and survival "
        "probabilities as a rough sense of shape, not a calibrated estimate, until "
        "spec.md §6's headline evaluation targets (≥100 observed closure events) are met.",
        "",
    ]

    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination
