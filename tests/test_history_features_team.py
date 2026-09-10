"""Tests for `rli.history.features.team_activity` (spec.md §5 "Amendment 2026-09-10")."""

from __future__ import annotations

import sqlite3

from test_history_helpers import COMPANY, add_capture, add_posting, at, job

from rli.config import Config
from rli.history.features import team_activity

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
