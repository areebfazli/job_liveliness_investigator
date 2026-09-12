"""Tests for `rli.history.features.team_activity` (spec.md §5 "Amendment 2026-09-10")."""

from __future__ import annotations

import sqlite3

from test_history_helpers import COMPANY, add_attempt, add_capture, add_posting, at, job

from rli.config import Config
from rli.history.closures import apply_to_postings
from rli.history.features import team_activity
from rli.replay.pit import point_in_time

# A fixed "now" far enough past the postings used below that every window
# offset below is an unambiguous, exact number of days.
NOW = at(100)


# ---------------------------------------------------------------------------
# New-roles / closures window boundaries
# ---------------------------------------------------------------------------


def test_new_role_at_the_window_boundary_is_counted(conn: sqlite3.Connection, cfg: Config) -> None:
    """`new_roles_from <= first_observed <= now` is a CLOSED interval."""
    window = cfg.team_signal.new_roles_window_days
    add_posting(conn, job_id="inside", team="Engineering", first_observed=at(100 - window))
    add_posting(conn, job_id="outside", team="Engineering", first_observed=at(100 - window - 1))

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.new_roles_30d == 1
    assert [e.posting_id for e in activity.new_roles] == ["greenhouse:acme:inside"]


def test_closure_at_the_window_boundary_is_counted(conn: sqlite3.Connection, cfg: Config) -> None:
    window = cfg.team_signal.closures_window_days
    add_posting(
        conn,
        job_id="inside",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(100 - window),
    )
    add_posting(
        conn,
        job_id="outside",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(100 - window - 1),
    )

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.closures_60d == 1
    assert [e.posting_id for e in activity.closures] == ["greenhouse:acme:inside"]


# ---------------------------------------------------------------------------
# The as-of bound on the posting SET, not just on the event windows
# ---------------------------------------------------------------------------


def test_a_posting_first_observed_after_now_contributes_to_nothing(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A posting we had not seen by `now` was not on the board we had seen.

    So it is out of scope entirely, not merely outside the two event
    windows: the counts that describe the WATCHED SET (`in_scope_postings`,
    `archive_only_postings`, `open_roles_now`) must not see it either.
    """
    add_posting(conn, job_id="present", team="Engineering", first_observed=at(90))
    add_posting(conn, job_id="future", team="Engineering", first_observed=at(110))
    add_posting(
        conn,
        job_id="future_archive",
        posting_id="archive:acme.com:future_archive",
        team="Engineering",
        first_observed=at(110),
    )

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.in_scope_postings == 1
    assert activity.archive_only_postings == 0
    assert activity.open_roles_now == 1
    assert activity.new_roles_30d == 1
    assert [e.posting_id for e in activity.new_roles] == ["greenhouse:acme:present"]


def test_a_posting_with_no_first_observed_is_excluded_for_the_same_reason(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """NULL `first_observed` says we have no evidence we ever saw it.

    It is also exactly what `rli.replay.pit._install_postings` leaves behind
    for a posting unseen at `T`, so counting it here would make the two
    corpora disagree.
    """
    add_posting(conn, job_id="seen", team="Engineering", first_observed=at(90))
    add_posting(conn, job_id="never_seen", team="Engineering")

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.in_scope_postings == 1
    assert activity.open_roles_now == 1
    assert [e.posting_id for e in activity.new_roles] == ["greenhouse:acme:seen"]


# ---------------------------------------------------------------------------
# open_roles_now
# ---------------------------------------------------------------------------


def test_open_roles_now_counts_only_in_scope_postings_with_no_closure(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="open1", team="Engineering", first_observed=at(0))
    add_posting(
        conn, job_id="closed1", team="Engineering", first_observed=at(0), first_seen_absent=at(50)
    )
    add_posting(conn, job_id="other_team", team="Sales", first_observed=at(0))

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.open_roles_now == 1
    assert activity.in_scope_postings == 2


def test_open_roles_now_means_open_as_of_now_not_open_today(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A disappearance first observed AFTER `now` had not happened yet at `now`.

    Reading `first_seen_absent IS NULL` instead would answer "is it open
    today?", which at an archive-era `now` is a fact from the future.
    """
    add_posting(
        conn,
        job_id="closes_after_now",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(110),
    )
    add_posting(
        conn,
        job_id="closes_at_now",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(100),
    )
    add_posting(
        conn,
        job_id="closes_before_now",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(90),
    )

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.in_scope_postings == 3
    # Only the one whose absence is still in the future at `now`.
    assert activity.open_roles_now == 1
    # ...and an absence AT `now` is an observed closure, not an open role.
    assert [e.posting_id for e in activity.closures] == [
        "greenhouse:acme:closes_at_now",
        "greenhouse:acme:closes_before_now",
    ]


# ---------------------------------------------------------------------------
# Company-wide fallback when the posting carries no team
# ---------------------------------------------------------------------------


def test_team_none_pools_the_whole_company(conn: sqlite3.Connection, cfg: Config) -> None:
    add_posting(conn, job_id="eng", team="Engineering", first_observed=at(90))
    add_posting(conn, job_id="sales", team="Sales", first_observed=at(95))

    activity = team_activity(conn, COMPANY, None, NOW, cfg)

    assert activity.scope == "company"
    assert activity.team is None
    assert activity.new_roles_30d == 2


def test_blank_or_whitespace_team_also_pools_the_whole_company(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="eng", team="Engineering", first_observed=at(90))

    for blank in ("", "   "):
        activity = team_activity(conn, COMPANY, blank, NOW, cfg)
        assert activity.scope == "company"
        assert activity.team is None
        assert activity.new_roles_30d == 1


# ---------------------------------------------------------------------------
# Team normalization
# ---------------------------------------------------------------------------


def test_team_matching_is_case_and_whitespace_insensitive(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="a", team="Engineering", first_observed=at(90))
    add_posting(conn, job_id="b", team=" engineering ", first_observed=at(92))
    add_posting(conn, job_id="c", team="ENGINEERING", first_observed=at(94))

    activity = team_activity(conn, COMPANY, "engineering", NOW, cfg)

    assert activity.new_roles_30d == 3
    assert activity.team == "engineering"
    assert activity.scope == "team"


# ---------------------------------------------------------------------------
# archive_only flagging and whole-in-scope-set provenance
# ---------------------------------------------------------------------------


def test_archive_only_posting_is_flagged_via_the_posting_id_prefix(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(
        conn,
        job_id="archived1",
        posting_id="archive:acme.com:archived1",
        team="Engineering",
        first_observed=at(90),
    )
    add_posting(conn, job_id="native1", team="Engineering", first_observed=at(0))

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.in_scope_postings == 2
    assert activity.archive_only_postings == 1
    new_role = next(e for e in activity.new_roles if e.posting_id == "archive:acme.com:archived1")
    assert new_role.archive_only is True


def test_in_scope_and_archive_only_counts_include_postings_outside_every_window(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Provenance of the WHOLE in-scope set, not just the windowed events."""
    add_posting(
        conn,
        job_id="ancient",
        posting_id="archive:acme.com:ancient",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(1),
    )
    add_posting(conn, job_id="native1", team="Engineering", first_observed=at(0))

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.new_roles_30d == 0
    assert activity.closures_60d == 0
    assert activity.in_scope_postings == 2
    assert activity.archive_only_postings == 1


# ---------------------------------------------------------------------------
# history_days / history_coverage are ALWAYS populated
# ---------------------------------------------------------------------------


def test_history_days_and_coverage_are_populated_even_with_zero_team_matches(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """'Never treat missing history as no activity' (module docstring)."""
    add_capture(conn, at(0), [job("j1")])
    add_capture(conn, at(50), [job("j1")])
    add_posting(conn, job_id="j1", team="Sales", first_observed=at(0))

    activity = team_activity(conn, COMPANY, "Nonexistent Team", NOW, cfg)

    assert activity.new_roles_30d == 0
    assert activity.in_scope_postings == 0
    assert activity.history_days > 0
    assert activity.history_coverage > 0


# ---------------------------------------------------------------------------
# last_capture_at
# ---------------------------------------------------------------------------


def test_last_capture_at_is_the_newest_capture_at_or_before_now(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """It is the observation a NEGATIVE answer rests on, so it may not be later.

    `rli.probes.team_signal` dates a `false` hiring-signal claim from this
    value; a capture taken after `now` would stamp the claim with an
    observation that had not been made yet.
    """
    add_capture(conn, at(10), [job("j1")])
    add_capture(conn, at(50), [job("j1")])
    add_capture(conn, at(150), [job("j1")])
    add_posting(conn, job_id="j1", team="Engineering", first_observed=at(10))

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.last_capture_at == at(50)


def test_last_capture_at_is_none_when_the_company_has_no_capture_that_early(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Nothing had been watched at `now`, which is not the same as a quiet board."""
    add_capture(conn, at(150), [job("j1")])
    add_posting(conn, job_id="j1", team="Engineering", first_observed=at(10))

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert activity.last_capture_at is None
    assert activity.history_days == 0.0
    assert activity.history_coverage == 0.0


# ---------------------------------------------------------------------------
# Captures after `now` are invisible to the coverage numbers
# ---------------------------------------------------------------------------


def test_history_days_and_coverage_ignore_captures_after_now(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`coverage_window(..., as_of=now)`: the window we had actually watched by then.

    Unbounded, a company we have kept capturing ever since would report a
    deeper and denser history at an archive-era `now` than it had — the
    denominator that "quietly flatters a dataset" (module docstring).
    """
    add_capture(conn, at(0), [job("j1")])
    add_capture(conn, at(50), [job("j1")])
    add_posting(conn, job_id="j1", team="Engineering", first_observed=at(0))

    before = team_activity(conn, COMPANY, "Engineering", NOW, cfg)
    assert before.history_days == 50.0
    assert before.history_coverage == 1.0

    add_capture(conn, at(120), [job("j1")])
    add_capture(conn, at(150), [job("j1")], coverage_status="partial")

    after = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert after.history_days == before.history_days
    assert after.history_coverage == before.history_coverage
    assert after.last_capture_at == before.last_capture_at


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_new_roles_and_closures_are_ordered_newest_first(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="early", team="Engineering", first_observed=at(70))
    add_posting(conn, job_id="late", team="Engineering", first_observed=at(95))
    add_posting(
        conn,
        job_id="closed_early",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(45),
    )
    add_posting(
        conn,
        job_id="closed_late",
        team="Engineering",
        first_observed=at(0),
        first_seen_absent=at(90),
    )

    activity = team_activity(conn, COMPANY, "Engineering", NOW, cfg)

    assert [e.posting_id for e in activity.new_roles] == [
        "greenhouse:acme:late",
        "greenhouse:acme:early",
    ]
    assert [e.posting_id for e in activity.closures] == [
        "greenhouse:acme:closed_late",
        "greenhouse:acme:closed_early",
    ]


# ---------------------------------------------------------------------------
# Point-in-time equivalence (the load-bearing claim of the whole arrangement)
# ---------------------------------------------------------------------------


def test_team_activity_agrees_with_the_point_in_time_corpus(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The explicit `as_of` bounds and a restricted corpus must give one answer.

    The two calls compute the same quantity by genuinely different routes:
    (a) reads the FULL corpus and bounds it arithmetically, while (b) runs
    inside `rli.replay.pit.point_in_time`, which hides every capture after
    `T` and RE-DERIVES the `postings` lifecycle columns from the ones that
    survive. So any field where they disagree is a real bug in one of them —
    either a bound this function is missing or a derivation that is not
    monotone in the capture set — and not a test to adjust.

    The corpus is built from captures and then derived with
    `rli.history.closures.apply_to_postings`, exactly as the live pipeline
    does, so the lifecycle columns are genuinely capture-derived rather than
    asserted; hand-written ones would make the equality a tautology about
    this file instead of a check on the two corpora. It straddles `T` in
    every dimension that matters: a closure and a first sighting on each
    side, an own capture attempt on each side, a complete capture after `T`
    and a partial one, plus an archive-only posting and an out-of-scope
    team.
    """
    t = at(60)
    # Job facts are constant across captures, so `postings.title` / `.team`
    # cannot differ between the corpora for a reason this test is not about
    # (`_merge_into_posting` takes them from the most recent capture).
    teams = {"sales": "Sales"}

    def board(*job_ids: str) -> list:
        return [job(j, title=j, team=teams.get(j, "Engineering")) for j in job_ids]

    for day, job_ids in (
        (10, ("keeps", "closes_before_t", "closes_after_t", "archive_only", "sales")),
        (20, ("keeps", "closes_before_t", "closes_after_t", "archive_only", "sales")),
        (30, ("keeps", "closes_after_t", "archive_only", "sales")),
        (40, ("keeps", "closes_after_t", "sales")),
        (55, ("keeps", "closes_after_t", "new_at_55", "sales")),
        (58, ("keeps", "closes_after_t", "new_at_55", "sales")),
        # Everything from here on is AFTER `t` and must be invisible at `t`:
        # `closes_after_t` disappears and `appears_after_t` shows up.
        (70, ("keeps", "new_at_55", "appears_after_t", "sales")),
        (80, ("keeps", "new_at_55", "appears_after_t", "sales")),
    ):
        add_capture(conn, at(day), board(*job_ids))
    add_capture(conn, at(75), board("keeps"), coverage_status="partial")

    # One failed own attempt inside the window (so history_coverage < 1) and
    # one after `t`, which neither corpus may count.
    add_attempt(conn, at(35))
    add_attempt(conn, at(75))

    # Identity rows only: every lifecycle column is left NULL and derived
    # below. `archive_only` deliberately gets no row, so `apply_to_postings`
    # creates the `archive:` one the way the live pipeline does.
    for job_id, team in (
        ("keeps", "Engineering"),
        ("closes_before_t", "Engineering"),
        ("closes_after_t", "Engineering"),
        ("new_at_55", "Engineering"),
        ("appears_after_t", "Engineering"),
        ("sales", "Sales"),
    ):
        add_posting(conn, job_id=job_id, team=team)
    apply_to_postings(conn, COMPANY, now=at(120))

    plain = team_activity(conn, COMPANY, "Engineering", t, cfg)

    # Pinned so the equality below cannot pass by both sides being empty.
    assert plain.in_scope_postings == 5  # `appears_after_t` is not one of them
    assert plain.archive_only_postings == 1
    assert plain.open_roles_now == 3  # keeps, new_at_55, and closes_after_t
    assert [e.posting_id for e in plain.new_roles] == ["greenhouse:acme:new_at_55"]
    assert [e.posting_id for e in plain.closures] == [
        "archive:acme.com:archive_only",
        "greenhouse:acme:closes_before_t",
    ]
    assert plain.last_capture_at == at(58)
    assert plain.history_days == 48.0
    assert plain.history_coverage == 6 / 7  # six complete days, plus the at(35) attempt

    with point_in_time(conn, t, company_ids=[COMPANY]):
        replayed = team_activity(conn, COMPANY, "Engineering", t, cfg)

    assert replayed == plain
