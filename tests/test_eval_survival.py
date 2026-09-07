"""Tests for `rli.eval.survival` — censoring-aware closure/repost curves."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest
from test_history_helpers import COMPANY, add_capture, add_posting, at, job

from rli.config import Config
from rli.eval.survival import (
    HORIZON_DAYS,
    BehaviorReport,
    behavior_report,
    collect_survival_sample,
    fit_interval_censored,
    fit_right_censored,
    write_behavior_report,
)
from rli.history.closures import apply_to_postings
from rli.models.time import to_utc_z

# ---------------------------------------------------------------------------
# Cohort builders
# ---------------------------------------------------------------------------


def _build_staggered_cohort(
    conn: sqlite3.Connection,
    *,
    n: int = 20,
    start_close_day: int = 10,
    close_step_days: int = 2,
    total_days: int = 60,
) -> None:
    """`n` postings, job `i` open on days `[0, start_close_day + i*close_step_days)`.

    Every posting eventually closes (interval-censored) inside `total_days`.
    """
    for i in range(n):
        add_posting(conn, job_id=f"j{i}", tenant="acme")
    for day in range(total_days):
        jobs = [
            job(f"j{i}", title=f"Role {i}")
            for i in range(n)
            if day < start_close_day + i * close_step_days
        ]
        add_capture(conn, at(day), jobs)


def _build_never_closing_cohort(conn: sqlite3.Connection, *, n: int = 10, days: int = 15) -> None:
    for i in range(n):
        add_posting(conn, job_id=f"open{i}", tenant="acme")
    for day in range(days):
        jobs = [job(f"open{i}", title=f"Open role {i}") for i in range(n)]
        add_capture(conn, at(day), jobs)


def _all_floats(value: object) -> list[float]:
    """Recursively collect every float leaf out of a `model_dump()`-shaped structure."""
    out: list[float] = []
    if isinstance(value, float):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_all_floats(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_all_floats(v))
    return out


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


def test_empty_database_produces_an_all_zero_report(conn: sqlite3.Connection, cfg: Config) -> None:
    report = behavior_report(conn, cfg)

    assert report.total_intervals == 0
    assert report.closed_count == 0
    assert report.right_censored_count == 0
    assert report.right_censored_curve.n_observations == 0
    assert report.right_censored_curve.median_days is None
    assert report.interval_censored_curve.n_observations == 0
    assert report.interval_censored_curve.median_days is None
    assert report.coverage.company_count == 0
    assert report.reposts.closed_postings == 0
    assert report.reposts.repost_rate is None


def test_empty_database_report_still_writes_valid_markdown(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = behavior_report(conn, cfg)
    destination = write_behavior_report(tmp_path / "report.md", report)

    text = destination.read_text(encoding="utf-8")
    assert "## Limitations and interpretation" in text
    assert "# Posting-behavior report" in text


# ---------------------------------------------------------------------------
# Right-censored-only (nobody closed)
# ---------------------------------------------------------------------------


def test_right_censored_only_gives_none_median_and_does_not_crash(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_never_closing_cohort(conn)

    report = behavior_report(conn, cfg, now=at(20))

    assert report.closed_count == 0
    assert report.right_censored_count == 10
    assert report.right_censored_curve.n_events == 0
    assert report.right_censored_curve.median_days is None
    assert report.interval_censored_curve.n_events == 0
    assert report.interval_censored_curve.median_days is None
    # Still open -> no reposting is even possible; None, not 0.0.
    assert report.reposts.repost_rate is None


def test_right_censored_only_report_still_writes(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    _build_never_closing_cohort(conn)
    report = behavior_report(conn, cfg, now=at(20))

    destination = write_behavior_report(tmp_path / "out.md", report)
    assert destination.exists()
    assert "## Limitations and interpretation" in destination.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Known cohort: interval-censored median lands in the expected range
# ---------------------------------------------------------------------------


def test_staggered_cohort_interval_censored_median_in_expected_range(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Jobs close at days 10, 12, 14, ..., 48 (20 jobs). The TRUE median
    # closure day (job index 9/10, i.e. the 10th closer) is around day 28-30.
    _build_staggered_cohort(conn)

    sample = collect_survival_sample(conn, now=at(70))
    assert sample.total == 20
    assert sample.n_interval_censored == 20
    assert sample.n_right_censored == 0

    curve = fit_interval_censored(sample)
    assert curve.n_observations == 20
    assert curve.n_events == 20
    assert curve.median_days is not None
    # Wide, deliberately non-exact range: assert the shape, not a float.
    assert 15.0 <= curve.median_days <= 45.0

    # The right-censored arm's median must be >= the interval-censored
    # arm's median (documented upward-bias judgment call).
    right_curve = fit_right_censored(sample)
    assert right_curve.median_days is not None
    assert right_curve.median_days >= curve.median_days


def test_staggered_cohort_full_report_end_to_end(conn: sqlite3.Connection, cfg: Config) -> None:
    _build_staggered_cohort(conn)
    report = behavior_report(conn, cfg, now=at(70))

    assert report.total_intervals == 20
    assert report.closed_count == 20
    assert report.coverage.company_count == 1
    assert report.coverage.history_days_min is not None
    assert report.coverage.history_days_min > 0


# ---------------------------------------------------------------------------
# survival_at: horizons beyond/inside support, monotonicity
# ---------------------------------------------------------------------------


def test_survival_at_horizons_beyond_support_are_none_and_monotone(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # All jobs closed by day 48; the fit has no data past ~day 49, so a
    # horizon like 90 (and likely 60) must be None.
    _build_staggered_cohort(conn)
    sample = collect_survival_sample(conn, now=at(70))

    for curve in (fit_right_censored(sample), fit_interval_censored(sample)):
        assert set(curve.survival_at) == set(HORIZON_DAYS)
        assert curve.survival_at[90] is None

        previous: float | None = None
        for horizon in HORIZON_DAYS:
            value = curve.survival_at[horizon]
            if value is None:
                continue
            assert 0.0 <= value <= 1.0
            if previous is not None:
                assert value <= previous + 1e-9  # non-increasing
            previous = value


def test_survival_at_horizon_within_support_is_a_probability(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_staggered_cohort(conn)
    sample = collect_survival_sample(conn, now=at(70))
    curve = fit_interval_censored(sample)

    assert curve.survival_at[7] is not None
    assert 0.0 <= curve.survival_at[7] <= 1.0


# ---------------------------------------------------------------------------
# No inf/nan anywhere in the report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build", [lambda conn: None, _build_never_closing_cohort, _build_staggered_cohort]
)
def test_no_inf_or_nan_in_report_model_dump(
    conn: sqlite3.Connection, cfg: Config, build
) -> None:
    build(conn)
    report = behavior_report(conn, cfg, now=at(70))

    dumped = report.model_dump()
    floats = _all_floats(dumped)
    assert all(math.isfinite(f) for f in floats), floats


# ---------------------------------------------------------------------------
# Zero-width interval (degenerate bracket) does not crash
# ---------------------------------------------------------------------------


def test_zero_width_interval_from_tied_captures_does_not_crash(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Two captures at the SAME instant (an own capture and an archive
    # capture of the same moment, per rli.history.closures's documented
    # tie-break): the job is present in the first, absent in the second,
    # collapsing last_seen_open == closure_absent_at.
    add_posting(conn, job_id="tied", tenant="acme")
    add_capture(conn, at(0), [job("tied", title="Tied Role")], source="own")
    add_capture(conn, at(1), [job("tied", title="Tied Role")], source="own")
    add_capture(conn, at(1), [], source="archive")

    sample = collect_survival_sample(conn, now=at(5))
    assert sample.n_zero_width_exact == 1

    # Must not raise.
    right_curve = fit_right_censored(sample)
    interval_curve = fit_interval_censored(sample)
    assert right_curve.n_observations == 1
    assert interval_curve.n_observations == 1


# ---------------------------------------------------------------------------
# write_behavior_report: parent dirs, Limitations heading
# ---------------------------------------------------------------------------


def test_write_behavior_report_creates_parent_dirs(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    report = behavior_report(conn, cfg)
    nested = tmp_path / "a" / "b" / "c" / "behavior.md"

    result = write_behavior_report(nested, report)

    assert result == nested
    assert nested.exists()
    text = nested.read_text(encoding="utf-8")
    assert "## Limitations and interpretation" in text
    assert "Do not use posting survival probability as the v1 action engine" in text


# ---------------------------------------------------------------------------
# Repost rate: None with no closed postings, correct fraction with them
# ---------------------------------------------------------------------------


def test_repost_rate_none_with_no_closed_postings(conn: sqlite3.Connection, cfg: Config) -> None:
    _build_never_closing_cohort(conn, n=3, days=5)
    report = behavior_report(conn, cfg, now=at(10))

    assert report.reposts.closed_postings == 0
    assert report.reposts.repost_rate is None
    assert report.reposts.median_days_to_repost is None


def test_repost_rate_and_median_gap_with_linked_reposts(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # 4 postings, all close at day 2. Two of them get a repost_links row
    # pointing at a freshly created replacement posting.
    for i in range(4):
        add_posting(conn, job_id=f"old{i}", tenant="acme")
    for day in range(5):
        jobs = [job(f"old{i}", title=f"Old role {i}") for i in range(4) if day < 2]
        add_capture(conn, at(day), jobs)
    apply_to_postings(conn, now=at(10))

    new0 = add_posting(conn, job_id="new0", tenant="acme", first_observed=at(5))
    new1 = add_posting(conn, job_id="new1", tenant="acme", first_observed=at(7))
    old0 = "greenhouse:acme:old0"
    old1 = "greenhouse:acme:old1"

    for old_id, new_id in ((old0, new0), (old1, new1)):
        conn.execute(
            """
            INSERT INTO repost_links (
                company_id, old_posting_id, new_posting_id,
                combined_score, component_scores, matched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (COMPANY, old_id, new_id, 0.9, "{}", to_utc_z(at(3))),
        )
    conn.commit()

    report = behavior_report(conn, cfg, now=at(10))

    assert report.reposts.closed_postings == 4
    assert report.reposts.reposted_count == 2
    assert report.reposts.repost_rate == pytest.approx(0.5)
    assert report.reposts.repost_links_total == 2
    # first_seen_absent lands at day 2 for all four (present days 0, 1 only);
    # new0 first_observed=5 -> gap 3 days, new1 first_observed=7 -> gap 5 days.
    assert report.reposts.median_days_to_repost == pytest.approx(4.0)
    assert report.reposts.median_days_to_repost_n == 2


# ---------------------------------------------------------------------------
# company_id scoping
# ---------------------------------------------------------------------------


def test_company_scoped_report_excludes_other_companies(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_staggered_cohort(conn, n=5)
    add_capture(
        conn,
        at(0),
        [job("other-job", title="Other Co Role")],
        company_id="globex.com",
    )

    scoped = behavior_report(conn, cfg, now=at(70), company_id=COMPANY)
    assert scoped.company_id == COMPANY
    assert scoped.total_intervals == 5
    assert scoped.coverage.company_count == 1


# ---------------------------------------------------------------------------
# BehaviorReport.describe() smoke test
# ---------------------------------------------------------------------------


def test_describe_does_not_raise_for_any_scenario(conn: sqlite3.Connection, cfg: Config) -> None:
    _build_staggered_cohort(conn, n=3)
    report: BehaviorReport = behavior_report(conn, cfg, now=at(70))
    text = str(report)
    assert "behavior report" in text
