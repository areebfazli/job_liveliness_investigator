"""`rli.replay.pit`: the point-in-time view of the collection corpus (spec.md §6).

The rule under test is the one the evidence gate cannot enforce: a system
replayed at `T` must not READ a corpus containing observations made after
`T`. Every assertion here goes through the ordinary corpus readers
(`rli.history.features`, `rli.probes.lookups`) rather than through this
module's own SQL, because the whole design depends on those readers — which
know nothing about replay — seeing the restricted corpus.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from test_eval_helpers import COMPANY, add_capture, add_posting, job
from test_replay_helpers import CLOSED_JOB, NOW, OPEN_JOB, TENANT, seed_corpus

from rli.config import Config
from rli.history.features import coverage_window, posting_features
from rli.models.policy_inputs import UNKNOWN
from rli.models.time import to_utc_z
from rli.probes.lookups import has_usable_history, posting_row
from rli.replay.pit import (
    PIT_SHADOWED_TABLES,
    PointInTimeError,
    installed_shadows,
    point_in_time,
)

OPEN_POSTING = f"greenhouse:{TENANT}:{OPEN_JOB}"
CLOSED_POSTING = f"greenhouse:{TENANT}:{CLOSED_JOB}"


def _replacement(conn: sqlite3.Connection, posting_id: str) -> str | None:
    """`postings.replacement_job_id` — not a column `posting_row` selects."""
    row = conn.execute(
        "SELECT replacement_job_id FROM postings WHERE posting_id = ?", (posting_id,)
    ).fetchone()
    return None if row is None else row[0]


def test_captures_after_t_are_invisible(conn: sqlite3.Connection) -> None:
    seed_corpus(conn)
    total = conn.execute("SELECT COUNT(*) FROM board_snapshots").fetchone()[0]

    with point_in_time(conn, NOW - timedelta(days=40), company_ids=[COMPANY]):
        visible = conn.execute("SELECT COUNT(*) FROM board_snapshots").fetchone()[0]
        # The -60 and -45 captures only.
        assert visible == 4
        jobs = conn.execute("SELECT COUNT(*) FROM board_snapshot_jobs").fetchone()[0]
        # acme listed two jobs in each of its two visible captures; globex one.
        assert jobs == 6

    assert conn.execute("SELECT COUNT(*) FROM board_snapshots").fetchone()[0] == total


def test_a_future_closure_is_hidden_and_reappears_afterwards(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The single most important leak this module exists to stop."""
    seed_corpus(conn)
    stored = posting_features(conn, cfg, CLOSED_POSTING, now=NOW)
    assert stored.first_seen_absent is not None

    with point_in_time(conn, NOW - timedelta(days=45), company_ids=[COMPANY]):
        at_t = posting_features(conn, cfg, CLOSED_POSTING, now=NOW - timedelta(days=45))
        assert at_t.first_seen_absent is None
        assert at_t.censoring == "right"

    with point_in_time(conn, NOW - timedelta(days=30), company_ids=[COMPANY]):
        at_t = posting_features(conn, cfg, CLOSED_POSTING, now=NOW - timedelta(days=30))
        assert at_t.first_seen_absent == NOW - timedelta(days=30)


def test_history_depth_shrinks_with_t_so_eligibility_does_too(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §4: history probes are ineligible without usable history."""
    seed_corpus(conn)
    assert has_usable_history(conn, cfg, COMPANY)

    # 15 days after the first capture, the company has 15 days of history,
    # which is under the configured `min_history_days` (30).
    with point_in_time(conn, NOW - timedelta(days=45), company_ids=[COMPANY]):
        window = coverage_window(conn, COMPANY)
        assert window.history_days == pytest.approx(15.0)
        assert not has_usable_history(conn, cfg, COMPANY)


def test_a_posting_not_yet_observed_at_t_has_no_lifecycle(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    seed_corpus(conn)
    with point_in_time(conn, NOW - timedelta(days=90), company_ids=[COMPANY]):
        # The identity row survives — it is a registry entry, not an
        # observation — but it makes no claim about having been seen.
        row = posting_row(conn, OPEN_POSTING)
        assert row is not None
        features = posting_features(conn, cfg, OPEN_POSTING, now=NOW - timedelta(days=90))
        assert features.first_observed is None
        assert features.age_days is UNKNOWN
        assert features.long_lived is UNKNOWN


def test_a_repost_link_whose_replacement_is_not_yet_visible_is_dropped(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    # A replacement posting first seen 10 days ago, linked to the job that
    # disappeared 30 days ago.
    replacement = add_posting(
        conn, job_id="6009", tenant=TENANT, first_observed=NOW - timedelta(days=10)
    )
    add_capture(conn, NOW - timedelta(days=10), [job("6009")], company_id=COMPANY)
    conn.execute(
        """
        INSERT INTO repost_links
            (company_id, old_posting_id, new_posting_id, combined_score,
             component_scores, matched_at)
        VALUES (?, ?, ?, 0.95, '{"scores": {"title": 1.0, "description": 1.0}}', ?)
        """,
        (COMPANY, CLOSED_POSTING, replacement, to_utc_z(NOW)),
    )
    conn.commit()

    with point_in_time(conn, NOW - timedelta(days=20), company_ids=[COMPANY]):
        # The replacement had not been seen yet, so the link could not have
        # been computed at T.
        assert conn.execute("SELECT COUNT(*) FROM repost_links").fetchone()[0] == 0
        assert _replacement(conn, CLOSED_POSTING) is None

    with point_in_time(conn, NOW, company_ids=[COMPANY]):
        assert conn.execute("SELECT COUNT(*) FROM repost_links").fetchone()[0] == 1
        assert _replacement(conn, CLOSED_POSTING) == replacement


def test_the_context_never_mutates_the_real_corpus(conn: sqlite3.Connection) -> None:
    seed_corpus(conn)
    before = {
        row["posting_id"]: (
            row["first_observed"],
            row["last_seen_open"],
            row["first_seen_absent"],
        )
        for row in conn.execute(
            "SELECT posting_id, first_observed, last_seen_open, first_seen_absent FROM postings"
        )
    }
    with point_in_time(conn, NOW - timedelta(days=45)):
        pass
    after = {
        row["posting_id"]: (
            row["first_observed"],
            row["last_seen_open"],
            row["first_seen_absent"],
        )
        for row in conn.execute(
            "SELECT posting_id, first_observed, last_seen_open, first_seen_absent FROM postings"
        )
    }
    assert before == after


def test_the_shadows_are_removed_even_when_the_block_raises(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    with pytest.raises(RuntimeError, match="boom"), point_in_time(conn, NOW):
        assert installed_shadows(conn) == list(PIT_SHADOWED_TABLES)
        raise RuntimeError("boom")
    assert installed_shadows(conn) == []


def test_nesting_is_refused_rather_than_double_filtering(
    conn: sqlite3.Connection,
) -> None:
    seed_corpus(conn)
    with point_in_time(conn, NOW):  # noqa: SIM117 - the nesting IS the test
        with pytest.raises(PointInTimeError, match="already installed"):
            with point_in_time(conn, NOW - timedelta(days=1)):
                pass


def test_the_temp_copy_never_contains_a_posting_the_real_table_lacks(
    conn: sqlite3.Connection,
) -> None:
    """Otherwise `runs.posting_id`'s foreign key would fail on a replay run."""
    seed_corpus(conn)
    # A job that appears in captures but has no `postings` row: the live
    # pipeline would create an `archive:` row for it, and the temp copy must
    # not.
    add_capture(conn, NOW - timedelta(days=20), [job("8888")], company_id=COMPANY)
    with point_in_time(conn, NOW, company_ids=[COMPANY]):
        temp_ids = {row[0] for row in conn.execute("SELECT posting_id FROM postings")}
        real_ids = {row[0] for row in conn.execute("SELECT posting_id FROM main.postings")}
        assert temp_ids <= real_ids


def test_runs_and_evidence_are_not_shadowed(conn: sqlite3.Connection) -> None:
    """A replay run recorded inside the context must land in the real database."""
    seed_corpus(conn)
    with point_in_time(conn, NOW):
        names = set(installed_shadows(conn))
        assert "runs" not in names
        assert "evidence" not in names
        assert "replay_probe_results" not in names


def test_a_naive_t_is_rejected(conn: sqlite3.Connection) -> None:
    from datetime import datetime

    with (
        pytest.raises(ValueError, match="timezone-aware"),
        point_in_time(
            conn,
            datetime(2026, 1, 1),  # noqa: DTZ001
        ),
    ):
        pass


def test_a_timestamp_that_is_not_a_to_utc_z_value_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """The `T` literal is inlined into a view definition; that must be checked."""
    from rli.replay.pit import _sql_timestamp_literal

    assert _sql_timestamp_literal(to_utc_z(NOW)) == f"'{to_utc_z(NOW)}'"
    with pytest.raises(PointInTimeError, match="refusing to inline"):
        _sql_timestamp_literal("2026-09-07' OR '1'='1")
