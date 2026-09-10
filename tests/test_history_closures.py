"""Tests for `rli.history.closures` — interval-censored closure derivation."""

from __future__ import annotations

import sqlite3

from test_history_helpers import (
    COMPANY,
    OTHER_COMPANY,
    add_capture,
    add_posting,
    at,
    interval_by_job,
    job,
    posting_row,
)

from rli.history.closures import apply_to_postings, build_intervals

# ---------------------------------------------------------------------------
# Censoring
# ---------------------------------------------------------------------------


def test_right_censored_posting_present_in_latest_capture(conn: sqlite3.Connection) -> None:
    for day in (0, 1, 2):
        add_capture(conn, at(day), [job("j1", title="Backend Engineer")])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.censoring == "right"
    assert interval.first_seen_absent is None
    assert interval.closure_absent_at is None
    assert interval.gap_days is None
    assert interval.first_observed == at(0)
    assert interval.last_seen_open == at(2)
    assert interval.capture_count == 3


def test_interval_censored_closure_bounds_and_gap_days(conn: sqlite3.Connection) -> None:
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(3), [job("j2", title="Designer")])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.censoring == "interval"
    assert interval.last_seen_open == at(1)
    assert interval.first_seen_absent == at(3)
    assert interval.closure_absent_at == at(3)
    # The true closure time lies in (day 1, day 3]; the bracket is 2 days wide.
    assert interval.gap_days == 2.0
    assert interval.reappeared_at is None


def test_empty_complete_capture_is_a_real_absence(conn: sqlite3.Connection) -> None:
    # A complete capture that legitimately listed zero jobs (the board really
    # is empty) IS evidence of absence, unlike a 'partial' zero-job capture.
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(2), [])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.censoring == "interval"
    assert interval.first_seen_absent == at(2)


# ---------------------------------------------------------------------------
# Coverage gaps are never absences (spec.md §4)
# ---------------------------------------------------------------------------


def test_gap_capture_between_complete_captures_is_not_an_absence(
    conn: sqlite3.Connection,
) -> None:
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [], coverage_status="gap")
    add_capture(conn, at(2), [job("j1", title="Backend Engineer")])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.censoring == "right"
    assert interval.first_seen_absent is None
    assert interval.last_seen_open == at(2)


def test_partial_capture_missing_the_job_is_not_an_absence(conn: sqlite3.Connection) -> None:
    # A JS-rendered board scraped to zero job links is recorded 'partial' by
    # rli.archive.backfill; it must not close every posting on that board.
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [], source="archive", coverage_status="partial")
    add_capture(conn, at(2), [job("j1", title="Backend Engineer")])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.censoring == "right"
    assert interval.first_seen_absent is None


def test_absence_is_bracketed_by_the_next_complete_capture_not_the_gap(
    conn: sqlite3.Connection,
) -> None:
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [], coverage_status="gap")
    add_capture(conn, at(2), [], source="archive", coverage_status="partial")
    add_capture(conn, at(3), [job("j2", title="Designer")])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.censoring == "interval"
    assert interval.last_seen_open == at(0)
    # Bracketed by the day-3 COMPLETE capture, not by the day-1 gap or the
    # day-2 partial: the interval is 3 days wide, not 1.
    assert interval.first_seen_absent == at(3)
    assert interval.gap_days == 3.0


def test_presence_in_a_partial_capture_still_counts_as_seen_open(
    conn: sqlite3.Connection,
) -> None:
    # Presence is readable from ANY coverage_status: if the job is listed, we
    # saw it. Only ABSENCE requires a 'complete' capture.
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [job("j1", title="Backend Engineer")], coverage_status="partial")
    add_capture(conn, at(2), [], coverage_status="gap")

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.last_seen_open == at(1)
    assert interval.censoring == "right"


# ---------------------------------------------------------------------------
# Reappearance
# ---------------------------------------------------------------------------


def test_reappearance_sets_reappeared_at_and_keeps_first_absence(
    conn: sqlite3.Connection,
) -> None:
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [])  # complete capture without j1 -> first absence
    add_capture(conn, at(4), [job("j1", title="Backend Engineer")])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.first_seen_absent == at(1)
    assert interval.reappeared_at == at(4)
    assert interval.last_seen_open == at(4)
    # Still present in the latest capture, so the closure is right-censored
    # even though an earlier absence exists.
    assert interval.censoring == "right"
    assert interval.gap_days is None


def test_second_disappearance_brackets_the_closure_not_the_first_absence(
    conn: sqlite3.Connection,
) -> None:
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [])
    add_capture(conn, at(4), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(6), [])

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.first_seen_absent == at(1)
    assert interval.reappeared_at == at(4)
    assert interval.last_seen_open == at(4)
    assert interval.closure_absent_at == at(6)
    assert interval.censoring == "interval"
    # gap_days brackets the LAST disappearance (day 4 -> day 6), not the first.
    assert interval.gap_days == 2.0


# ---------------------------------------------------------------------------
# Sources and scoping
# ---------------------------------------------------------------------------


def test_own_and_archive_captures_are_merged_into_one_lifecycle(
    conn: sqlite3.Connection,
) -> None:
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(5), [job("j1", title="Backend Engineer")], source="own")

    interval = interval_by_job(build_intervals(conn, COMPANY), "j1")

    assert interval.sources == ("archive", "own")
    assert interval.archive_only is False
    assert interval.first_observed == at(0)
    assert interval.last_seen_open == at(5)


def test_intervals_never_cross_companies(conn: sqlite3.Connection) -> None:
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")], company_id=COMPANY)
    add_capture(conn, at(1), [job("j1", title="Backend Engineer")], company_id=OTHER_COMPANY)

    intervals = build_intervals(conn)

    assert {(i.company_id, i.job_id) for i in intervals} == {
        (COMPANY, "j1"),
        (OTHER_COMPANY, "j1"),
    }
    acme = [i for i in intervals if i.company_id == COMPANY]
    assert interval_by_job(acme, "j1").capture_count == 1


# ---------------------------------------------------------------------------
# apply_to_postings: archive-only creation
# ---------------------------------------------------------------------------


def test_archive_only_posting_is_created_with_borrowed_ats(conn: sqlite3.Connection) -> None:
    # The company already runs Ashby (one existing posting says so), so the
    # archive-only posting borrows 'ashby' rather than falling back to 'other'.
    add_posting(conn, job_id="known", ats="ashby", tenant="acme-tenant")
    add_capture(
        conn,
        at(0),
        [job("arch1", title="Archive Only Role", url="https://jobs.ashbyhq.com/acme/arch1")],
        source="archive",
    )

    summary = apply_to_postings(conn)

    assert summary.postings_created == 1
    row = posting_row(conn, f"archive:{COMPANY}:arch1")
    assert row["ats"] == "ashby"
    assert row["ats_tenant_id"] is None
    assert row["ats_job_id"] == "arch1"
    assert row["canonical_url"] == "https://jobs.ashbyhq.com/acme/arch1"
    assert row["first_observed"] is not None


def test_archive_only_posting_falls_back_to_other_and_placeholder_url(
    conn: sqlite3.Connection,
) -> None:
    add_capture(conn, at(0), [job("arch1", title="Archive Only Role")], source="archive")

    apply_to_postings(conn)

    row = posting_row(conn, f"archive:{COMPANY}:arch1")
    assert row["ats"] == "other"
    assert row["ats_tenant_id"] is None
    # Never a plausible-but-wrong ATS URL.
    assert row["canonical_url"] == f"archive-only:{COMPANY}/arch1"
    assert "https://" not in row["canonical_url"]


def test_archive_only_posting_carries_the_derived_interval(conn: sqlite3.Connection) -> None:
    add_capture(conn, at(0), [job("arch1", title="Archive Only Role")], source="archive")
    add_capture(conn, at(2), [], source="archive")

    apply_to_postings(conn)

    row = posting_row(conn, f"archive:{COMPANY}:arch1")
    assert row["first_observed"].startswith("2026-01-01")
    assert row["last_seen_open"].startswith("2026-01-01")
    assert row["first_seen_absent"].startswith("2026-01-03")


# ---------------------------------------------------------------------------
# apply_to_postings: the merge rule
# ---------------------------------------------------------------------------


def test_first_observed_moves_earlier_and_last_seen_open_moves_later(
    conn: sqlite3.Connection,
) -> None:
    posting_id = add_posting(conn, job_id="j1", first_observed=at(5), last_seen_open=at(6))
    # Archive discovers the posting existed earlier; own capture is newer.
    add_capture(conn, at(1), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(9), [job("j1", title="Backend Engineer")], source="own")

    summary = apply_to_postings(conn)

    row = posting_row(conn, posting_id)
    assert row["first_observed"] == "2026-01-02T12:00:00.000000Z"
    assert row["last_seen_open"] == "2026-01-10T12:00:00.000000Z"
    assert summary.first_observed_tightened == 1
    assert summary.last_seen_open_advanced == 1
    assert summary.postings_created == 0


def test_later_derived_values_never_widen_a_stored_lifecycle(conn: sqlite3.Connection) -> None:
    posting_id = add_posting(conn, job_id="j1", first_observed=at(0), last_seen_open=at(20))
    # Archive history is strictly weaker: it starts later and ends earlier.
    add_capture(conn, at(3), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(5), [job("j1", title="Backend Engineer")], source="archive")

    apply_to_postings(conn)

    row = posting_row(conn, posting_id)
    assert row["first_observed"] == "2026-01-01T12:00:00.000000Z"
    assert row["last_seen_open"] == "2026-01-21T12:00:00.000000Z"


def test_archive_absence_never_regresses_an_own_confirmed_open_posting(
    conn: sqlite3.Connection,
) -> None:
    # Own daily snapshots saw this posting open on day 10. A sparse archive
    # capture on day 5 did not list it — that is the archive being a weaker
    # observer, NOT the posting closing.
    posting_id = add_posting(conn, job_id="j1", first_observed=at(0), last_seen_open=at(10))
    add_capture(conn, at(2), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(5), [job("j2", title="Designer")], source="archive")

    summary = apply_to_postings(conn)

    row = posting_row(conn, posting_id)
    assert row["first_seen_absent"] is None, "archive data must not close an own-open posting"
    assert row["last_seen_open"] == "2026-01-11T12:00:00.000000Z"
    assert summary.first_seen_absent_blocked == 1
    assert summary.first_seen_absent_tightened == 0


def test_first_seen_absent_is_written_when_own_data_does_not_contradict_it(
    conn: sqlite3.Connection,
) -> None:
    posting_id = add_posting(conn, job_id="j1", first_observed=at(0), last_seen_open=at(2))
    add_capture(conn, at(2), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(6), [], source="archive")

    summary = apply_to_postings(conn)

    row = posting_row(conn, posting_id)
    assert row["first_seen_absent"] == "2026-01-07T12:00:00.000000Z"
    assert summary.first_seen_absent_tightened == 1
    assert summary.first_seen_absent_blocked == 0


def test_first_seen_absent_only_ever_moves_earlier(conn: sqlite3.Connection) -> None:
    posting_id = add_posting(
        conn,
        job_id="j1",
        first_observed=at(0),
        last_seen_open=at(1),
        first_seen_absent=at(10),
    )
    # Archive brackets the disappearance more tightly (day 4, not day 10).
    add_capture(conn, at(1), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(4), [], source="archive")

    apply_to_postings(conn)
    assert posting_row(conn, posting_id)["first_seen_absent"] == "2026-01-05T12:00:00.000000Z"


def test_a_later_derived_absence_never_replaces_a_stored_one(conn: sqlite3.Connection) -> None:
    posting_id = add_posting(
        conn,
        job_id="j1",
        first_observed=at(0),
        last_seen_open=at(1),
        first_seen_absent=at(2),
    )
    add_capture(conn, at(1), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(8), [], source="archive")

    summary = apply_to_postings(conn)

    assert posting_row(conn, posting_id)["first_seen_absent"] == "2026-01-03T12:00:00.000000Z"
    assert summary.first_seen_absent_tightened == 0


def test_reappeared_at_is_set_only_when_currently_null(conn: sqlite3.Connection) -> None:
    posting_id = add_posting(
        conn,
        job_id="j1",
        first_observed=at(0),
        last_seen_open=at(0),
        first_seen_absent=at(1),
    )
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")])
    add_capture(conn, at(1), [])
    add_capture(conn, at(3), [job("j1", title="Backend Engineer")])

    summary = apply_to_postings(conn)
    row = posting_row(conn, posting_id)
    assert row["reappeared_at"] == "2026-01-04T12:00:00.000000Z"
    assert summary.reappeared_at_set == 1

    # Second run must not touch it again.
    summary_2 = apply_to_postings(conn)
    assert summary_2.reappeared_at_set == 0
    assert posting_row(conn, posting_id)["reappeared_at"] == "2026-01-04T12:00:00.000000Z"


def test_null_title_is_backfilled_but_an_existing_one_is_not_overwritten(
    conn: sqlite3.Connection,
) -> None:
    with_title = add_posting(
        conn, job_id="j1", title="Own Title", first_observed=at(0), last_seen_open=at(0)
    )
    without_title = add_posting(
        conn, job_id="j2", title=None, first_observed=at(0), last_seen_open=at(0)
    )
    add_capture(
        conn,
        at(0),
        [job("j1", title="Archive Title"), job("j2", title="Archive Title 2")],
        source="archive",
    )

    apply_to_postings(conn)

    assert posting_row(conn, with_title)["title"] == "Own Title"
    assert posting_row(conn, without_title)["title"] == "Archive Title 2"


def test_apply_to_postings_is_idempotent(conn: sqlite3.Connection) -> None:
    add_posting(conn, job_id="j1", first_observed=at(1), last_seen_open=at(1))
    add_capture(conn, at(0), [job("j1", title="Backend Engineer")], source="archive")
    add_capture(conn, at(3), [], source="archive")
    add_capture(conn, at(3), [job("arch1", title="Archive Only")], source="archive")

    first = apply_to_postings(conn)
    before = [tuple(row) for row in conn.execute("SELECT * FROM postings ORDER BY posting_id")]
    second = apply_to_postings(conn)
    after = [tuple(row) for row in conn.execute("SELECT * FROM postings ORDER BY posting_id")]

    assert first.postings_created == 1
    assert second.postings_created == 0
    assert before == after


def test_ambiguous_job_ids_are_counted_not_silently_merged(conn: sqlite3.Connection) -> None:
    # Two tenants under one company_id reusing the same bare job id — the
    # documented limitation of the (company_id, ats_job_id) correlation key.
    add_posting(conn, job_id="j1", tenant="acme-one", first_observed=at(0), last_seen_open=at(0))
    add_posting(conn, job_id="j1", tenant="acme-two", first_observed=at(0), last_seen_open=at(0))
    add_capture(conn, at(1), [job("j1", title="Backend Engineer")])

    summary = apply_to_postings(conn)

    assert summary.ambiguous_job_ids == 1
    # The lowest posting_id wins, deterministically.
    assert posting_row(conn, "greenhouse:acme-one:j1")["last_seen_open"] is not None
