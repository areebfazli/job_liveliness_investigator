"""Tests for `rli.history.features` — coverage, trends, and UNKNOWN discipline."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from test_history_helpers import (
    COMPANY,
    add_attempt,
    add_capture,
    add_company,
    add_posting,
    at,
    job,
)

from rli.config import Config
from rli.history.closures import apply_to_postings
from rli.history.features import company_features, coverage_window, posting_features
from rli.history.matching import link_reposts
from rli.models.policy_inputs import UNKNOWN, Unknown
from rli.models.time import to_utc_z

TITLE = "Senior Backend Engineer"
OLD_POSTING = "greenhouse:acme:old1"
TWIN_POSTING = "greenhouse:acme:twin1"


def _posting_snapshot(
    conn: sqlite3.Connection,
    posting_id: str,
    captured_at: datetime,
    *,
    source: str = "archive",
    status: str = "open",
) -> None:
    """Raw-SQL `posting_snapshots` row — the per-posting capture table.

    No helper for this table exists in `test_history_helpers` (only the
    board-capture route, via `add_capture`, is covered there); mirrors
    `tests/test_probes_requirements_drift.py`'s `_archive_snapshot`, which
    inserts into the same table the same way.
    """
    conn.execute(
        "INSERT INTO posting_snapshots (posting_id, captured_at, source, status) "
        "VALUES (?, ?, ?, ?)",
        (posting_id, to_utc_z(captured_at), source, status),
    )
    conn.commit()


def _long_history(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    old_hash: str | None,
    twin_hash: str | None,
) -> None:
    """40 days of history: old1 open to day 20, gone day 21, twin1 from day 22."""
    add_posting(conn, job_id="old1", title=TITLE, team="Infrastructure", location="Remote")
    add_posting(conn, job_id="twin1", title=TITLE, team="Infrastructure", location="Remote")

    old = job(
        "old1", title=TITLE, team="Infrastructure", location="Remote", description_hash=old_hash
    )
    twin = job(
        "twin1", title=TITLE, team="Infrastructure", location="Remote", description_hash=twin_hash
    )

    for day in (0, 10, 20):
        add_capture(conn, at(day), [old])
    add_capture(conn, at(21), [])
    for day in (22, 30, 40):
        add_capture(conn, at(day), [twin])

    apply_to_postings(conn)
    link_reposts(conn, cfg)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_coverage_counts_complete_days_over_attempted_days(conn: sqlite3.Connection) -> None:
    add_capture(conn, at(0), [job("j1")])
    add_capture(conn, at(1), [], coverage_status="gap")
    add_capture(conn, at(2), [job("j1")])
    add_attempt(conn, at(3), ok=False)  # tried, produced no snapshot at all
    add_capture(conn, at(4), [job("j1")])
    # day 5: never attempted — the collector was not running
    add_capture(conn, at(6), [job("j1")])

    coverage = coverage_window(conn, COMPANY)

    assert coverage.history_days == 6.0
    assert coverage.calendar_days == 7
    assert coverage.complete_capture_days == 4  # days 0, 2, 4, 6
    assert coverage.attempted_days == 6  # days 0, 1, 2, 3, 4, 6
    assert coverage.incomplete_capture_days == 2  # days 1 (gap) and 3 (attempt only)
    assert coverage.never_attempted_days == 1  # day 5
    assert coverage.history_coverage == pytest.approx(4 / 6)
    assert coverage.calendar_coverage == pytest.approx(4 / 7)


def test_no_history_is_zero_coverage_not_full_coverage(conn: sqlite3.Connection) -> None:
    add_company(conn)

    coverage = coverage_window(conn, COMPANY)

    assert coverage.history_days == 0.0
    assert coverage.history_coverage == 0.0
    assert coverage.calendar_coverage == 0.0
    assert coverage.first_capture_at is None


def test_archive_capture_attempts_do_not_inflate_the_denominator(
    conn: sqlite3.Connection,
) -> None:
    add_capture(conn, at(0), [job("j1")])
    add_capture(conn, at(2), [job("j1")])
    baseline = coverage_window(conn, COMPANY)

    # rli.archive.backfill stamps its attempts with the RUN time, not the
    # historical capture date, so they must not count as expected days.
    add_attempt(conn, at(1), source="archive", ok=True)
    add_attempt(conn, at(1), source="archive", ok=False)

    assert coverage_window(conn, COMPANY) == baseline


def test_partial_capture_day_is_attempted_but_not_covered(conn: sqlite3.Connection) -> None:
    add_capture(conn, at(0), [job("j1")])
    add_capture(conn, at(1), [], source="archive", coverage_status="partial")
    add_capture(conn, at(2), [job("j1")])

    coverage = coverage_window(conn, COMPANY)

    assert coverage.attempted_days == 3
    assert coverage.complete_capture_days == 2
    assert coverage.history_coverage == pytest.approx(2 / 3)


def test_as_of_excludes_later_captures_but_none_still_sees_everything(
    conn: sqlite3.Connection,
) -> None:
    """`as_of` bounds `board_snapshots` by `captured_at <= as_of`; default is unbounded.

    Day 100 is "in the future" relative to day 50: with `as_of=at(50)` it must
    not exist as far as the window is concerned (last capture is day 10, and
    the day-100 capture contributes to neither `history_days` nor the
    complete-day count), while the default `as_of=None` must still see it —
    proving the new parameter changed nothing about today's behaviour.
    """
    add_capture(conn, at(0), [job("j1")])
    add_capture(conn, at(10), [job("j1")])
    add_capture(conn, at(100), [job("j1")])

    unbounded = coverage_window(conn, COMPANY)
    assert unbounded.last_capture_at == at(100)
    assert unbounded.history_days == 100.0
    assert unbounded.complete_capture_days == 3

    bounded = coverage_window(conn, COMPANY, as_of=at(50))
    assert bounded.last_capture_at == at(10)
    assert bounded.history_days == 10.0
    assert bounded.complete_capture_days == 2
    assert bounded.calendar_days == 11


# ---------------------------------------------------------------------------
# Thin history must never become a guess (spec.md §4)
# ---------------------------------------------------------------------------


def test_thin_history_yields_unknown_repost_pattern(conn: sqlite3.Connection, cfg: Config) -> None:
    # Five days of history, well under thresholds.min_history_days.
    add_posting(conn, job_id="old1", title=TITLE)
    add_capture(conn, at(0), [job("old1", title=TITLE)])
    add_capture(conn, at(5), [])
    apply_to_postings(conn)

    features = posting_features(conn, cfg, OLD_POSTING)

    assert features.history_days == 5.0
    assert features.history_days < cfg.thresholds.min_history_days
    assert features.repost_pattern is UNKNOWN
    assert isinstance(features.repost_pattern, Unknown)
    assert features.repost_pattern != "none"
    assert "min_history_days" in features.repost_pattern_reason
    # The closure itself is still observed and reported — only the
    # history-dependent JUDGEMENT degrades to UNKNOWN.
    assert features.first_seen_absent is not None
    assert features.censoring == "interval"


def test_no_history_at_all_yields_unknown_not_none(conn: sqlite3.Connection, cfg: Config) -> None:
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(0), first_seen_absent=at(1))

    features = posting_features(conn, cfg, OLD_POSTING)

    assert features.history_days == 0.0
    assert features.history_coverage == 0.0
    assert features.repost_pattern is UNKNOWN


def test_sufficient_history_with_no_link_yields_none(conn: sqlite3.Connection, cfg: Config) -> None:
    add_posting(conn, job_id="old1", title=TITLE)
    add_capture(conn, at(0), [job("old1", title=TITLE)])
    add_capture(conn, at(40), [])
    apply_to_postings(conn)

    features = posting_features(conn, cfg, OLD_POSTING)

    assert features.history_days == 40.0
    assert features.repost_pattern == "none"
    assert "no repost link" in features.repost_pattern_reason


def test_open_posting_with_sufficient_history_is_none(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="old1", title=TITLE)
    add_capture(conn, at(0), [job("old1", title=TITLE)])
    add_capture(conn, at(40), [job("old1", title=TITLE)])
    apply_to_postings(conn)

    features = posting_features(conn, cfg, OLD_POSTING)

    assert features.censoring == "right"
    assert features.repost_pattern == "none"


# ---------------------------------------------------------------------------
# repost_pattern classification
# ---------------------------------------------------------------------------


def test_matching_description_hash_is_repeated_unchanged(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _long_history(conn, cfg, old_hash="sha256:same", twin_hash="sha256:same")

    features = posting_features(conn, cfg, OLD_POSTING)

    assert features.repost_pattern == "repeated_unchanged"
    assert features.replacement_job_id == TWIN_POSTING


def test_differing_description_hash_is_changed(conn: sqlite3.Connection, cfg: Config) -> None:
    _long_history(conn, cfg, old_hash="sha256:before", twin_hash="sha256:after")

    features = posting_features(conn, cfg, OLD_POSTING)

    assert features.replacement_job_id == TWIN_POSTING
    assert features.repost_pattern == "changed"


def test_missing_description_hash_cannot_claim_unchanged(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _long_history(conn, cfg, old_hash=None, twin_hash=None)

    features = posting_features(conn, cfg, OLD_POSTING)

    assert features.replacement_job_id == TWIN_POSTING
    # A repost link exists, but "repeated UNCHANGED" is a positive claim
    # about content that a missing hash cannot support.
    assert features.repost_pattern is UNKNOWN
    assert "description_hash" in features.repost_pattern_reason


# ---------------------------------------------------------------------------
# age / long_lived
# ---------------------------------------------------------------------------


def test_long_lived_is_true_once_the_age_lower_bound_passes_the_threshold(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="old1", title=TITLE)
    add_capture(conn, at(0), [job("old1", title=TITLE)])
    add_capture(conn, at(10), [job("old1", title=TITLE)])
    apply_to_postings(conn)

    as_of = at(cfg.thresholds.long_lived_days + 5)
    features = posting_features(conn, cfg, OLD_POSTING, now=as_of)

    assert features.age_days == pytest.approx(cfg.thresholds.long_lived_days + 5)
    # A LOWER bound above the threshold is conclusive even on thin history.
    assert features.long_lived is True


def test_long_lived_is_false_on_thin_history_now_that_the_hedge_is_gone(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §5's Amendment 2026-09-10 removed the coverage hedge.

    A 40-day-old posting used to report `long_lived=UNKNOWN` whenever the
    company's own history span was shorter than `long_lived_days`, on the
    grounds that the posting might predate our first capture. The amendment
    replaces that: `age_days` now starts from the earliest of the ATS publish
    date, the earliest archive capture and `first_observed`, and `long_lived`
    is the plain comparison against the threshold. See
    `rli.history.features`' module docstring for why a wrong `False` is safe
    — it can only BLOCK the policy's P4 `skip`, never enable it.
    """
    add_posting(conn, job_id="old1", title=TITLE)
    add_capture(conn, at(0), [job("old1", title=TITLE)])
    add_capture(conn, at(40), [job("old1", title=TITLE)])
    apply_to_postings(conn)

    features = posting_features(conn, cfg, OLD_POSTING, now=at(40))

    assert features.age_days == 40.0
    assert features.long_lived is False


def test_long_lived_is_unknown_only_when_every_origin_is_absent(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """No publish date, no archive capture, no `first_observed` -> UNKNOWN.

    The one remaining UNKNOWN case: with nothing at all to date the posting
    from, `age_days` is not a number and `long_lived` must not be `False`
    (spec.md §4: "missing history never means flat hiring").
    """
    add_posting(conn, job_id="old1", title=TITLE, first_observed=None)

    features = posting_features(conn, cfg, OLD_POSTING, now=at(40))

    assert features.age_days is UNKNOWN
    assert features.long_lived is UNKNOWN


def test_the_ats_publish_date_can_make_a_young_looking_posting_long_lived(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The amendment's point: `long_lived` no longer depends on OUR start date.

    We first observed this posting 10 days ago, but the ATS says it was
    published well beyond the threshold — so it is long-lived, and the
    caller's `first_published` is what says so.
    """
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(30))
    add_capture(conn, at(30), [job("old1", title=TITLE)])
    apply_to_postings(conn)

    features = posting_features(
        conn,
        cfg,
        OLD_POSTING,
        now=at(40),
        first_published=at(40 - cfg.thresholds.long_lived_days - 5),
    )

    assert features.age_days == pytest.approx(cfg.thresholds.long_lived_days + 5)
    assert features.long_lived is True


def test_long_lived_is_false_when_the_earliest_origin_is_inside_the_threshold(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    span = cfg.thresholds.long_lived_days + 20
    add_capture(conn, at(0), [job("other")])
    add_capture(conn, at(span), [job("old1", title=TITLE)])
    add_posting(
        conn, job_id="old1", title=TITLE, first_observed=at(span - 10), last_seen_open=at(span)
    )

    features = posting_features(conn, cfg, OLD_POSTING, now=at(span))

    assert features.history_days == float(span)
    assert features.age_days == 10.0
    assert features.long_lived is False


def test_long_lived_from_an_archive_posting_snapshot_predating_first_observed(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The `posting_snapshots` archive route: an early archive capture wins over `first_observed`.

    `first_observed` (our own first sighting) is day 100; an archived open
    capture from day 10 predates it, so `age_days` must run from day 10, not
    day 100 — the whole point of the amendment's origin set.
    """
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(100))
    _posting_snapshot(conn, OLD_POSTING, at(10), source="archive", status="open")

    features = posting_features(conn, cfg, OLD_POSTING, now=at(200))

    assert features.age_days == pytest.approx(190.0)  # 200 - 10, not 200 - 100
    assert features.long_lived is True  # 190 >= long_lived_days (180)


def test_long_lived_from_an_archive_board_snapshot_predating_first_observed(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The `board_snapshot_jobs` archive route, joined on `(company_id, ats_job_id)`.

    Same shape as the `posting_snapshots` archive test, but through a
    company-wide board capture instead of a per-posting one — `add_posting`
    stores `job_id` as `ats_job_id`, which is what the join key requires.
    """
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(100))
    add_capture(conn, at(10), [job("old1", title=TITLE)], source="archive")

    features = posting_features(conn, cfg, OLD_POSTING, now=at(200))

    assert features.age_days == pytest.approx(190.0)
    assert features.long_lived is True


def test_own_capture_does_not_count_as_an_archive_origin(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """An `'own'` capture must NOT widen `age_days` the way an `'archive'` one does.

    Both archive queries in `posting_features` filter `source = 'archive'`
    explicitly; this pins that an early `'own' posting_snapshots` row (own
    captures cannot predate `first_observed`, which is derived from exactly
    those captures — module docstring) is correctly excluded rather than
    silently treated as an archive origin.
    """
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(100))
    _posting_snapshot(conn, OLD_POSTING, at(10), source="own", status="open")

    features = posting_features(conn, cfg, OLD_POSTING, now=at(200))

    # If the 'own' row were wrongly counted, age_days would be 190, not 100.
    assert features.age_days == pytest.approx(100.0)
    assert features.long_lived is False  # 100 < long_lived_days (180)


def test_age_days_takes_the_earliest_of_all_three_origins_when_several_are_present(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """publish date, archive capture, and `first_observed` all present: the EARLIEST wins."""
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(80))
    _posting_snapshot(conn, OLD_POSTING, at(30), source="archive", status="open")

    features = posting_features(conn, cfg, OLD_POSTING, now=at(200), first_published=at(50))

    # Earliest of {50, 30, 80} is 30 — neither of the other two origins.
    assert features.age_days == pytest.approx(170.0)


def test_long_lived_boundary_exactly_at_the_threshold_is_true(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(0))

    features = posting_features(conn, cfg, OLD_POSTING, now=at(cfg.thresholds.long_lived_days))

    assert features.age_days == pytest.approx(float(cfg.thresholds.long_lived_days))
    assert features.long_lived is True  # >= is inclusive


def test_long_lived_boundary_one_day_under_the_threshold_is_false(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="old1", title=TITLE, first_observed=at(0))

    features = posting_features(conn, cfg, OLD_POSTING, now=at(cfg.thresholds.long_lived_days - 1))

    assert features.age_days == pytest.approx(float(cfg.thresholds.long_lived_days - 1))
    assert features.long_lived is False


def test_posting_features_rejects_an_unknown_posting_id(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    with pytest.raises(KeyError):
        posting_features(conn, cfg, "greenhouse:acme:nope")


# ---------------------------------------------------------------------------
# Company features
# ---------------------------------------------------------------------------


def test_company_features_always_carry_coverage(conn: sqlite3.Connection, cfg: Config) -> None:
    add_company(conn)

    features = company_features(conn, cfg, COMPANY)

    assert features.history_days == 0.0
    assert features.history_coverage == 0.0
    assert features.open_count_slope_per_day is UNKNOWN
    assert features.open_count_direction is UNKNOWN
    assert features.median_lifetime_days_range is None
    assert features.repost_rate is None


def test_open_count_trend_is_inspectable(conn: sqlite3.Connection, cfg: Config) -> None:
    add_capture(conn, at(0), [job(f"j{i}") for i in range(5)])
    add_capture(conn, at(10), [job("j0")])

    features = company_features(conn, cfg, COMPANY)

    assert features.open_count_points == 2
    assert (features.open_count_first, features.open_count_last) == (5, 1)
    assert features.open_count_slope_per_day == pytest.approx(-0.4)
    assert features.open_count_direction == "down"


def test_open_count_trend_is_flat_when_the_fitted_change_is_under_one_job(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_capture(conn, at(0), [job("j0"), job("j1"), job("j2")])
    add_capture(conn, at(10), [job("j0"), job("j1"), job("j2")])

    features = company_features(conn, cfg, COMPANY)

    assert features.open_count_slope_per_day == pytest.approx(0.0)
    assert features.open_count_direction == "flat"


def test_a_single_capture_is_a_level_not_a_trend(conn: sqlite3.Connection, cfg: Config) -> None:
    add_capture(conn, at(0), [job("j0")])

    features = company_features(conn, cfg, COMPANY)

    assert features.open_count_points == 1
    assert features.open_count_slope_per_day is UNKNOWN
    assert features.open_count_direction is UNKNOWN


def test_median_lifetime_is_reported_as_a_censored_range(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(
        conn, job_id="a", first_observed=at(0), last_seen_open=at(10), first_seen_absent=at(12)
    )
    add_posting(
        conn, job_id="b", first_observed=at(0), last_seen_open=at(20), first_seen_absent=at(24)
    )
    add_posting(conn, job_id="c", first_observed=at(0), last_seen_open=at(30))

    features = company_features(conn, cfg, COMPANY)

    assert features.closed_count == 2
    assert features.right_censored_count == 1
    lower, upper = features.median_lifetime_days_range
    assert (lower, upper) == (15.0, 18.0)
    assert lower <= upper


def test_closure_counts_use_the_30_and_90_day_windows(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="a", first_observed=at(0), first_seen_absent=at(100))
    add_posting(conn, job_id="b", first_observed=at(0), first_seen_absent=at(60))
    add_posting(conn, job_id="c", first_observed=at(0), first_seen_absent=at(10))

    features = company_features(conn, cfg, COMPANY, now=at(101))

    assert features.closures_last_30_days == 1  # only the day-100 closure
    assert features.closures_last_90_days == 2  # days 100 and 60
    assert features.closed_count == 3


def test_repost_rate_is_the_linked_fraction_of_closed_postings(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _long_history(conn, cfg, old_hash="sha256:same", twin_hash="sha256:same")
    # A second posting that closed and was never reposted.
    add_posting(conn, job_id="lonely", first_observed=at(0), first_seen_absent=at(30))

    features = company_features(conn, cfg, COMPANY, now=at(40))

    assert features.closed_count == 2
    assert features.reposted_count == 1
    assert features.repost_rate == pytest.approx(0.5)
