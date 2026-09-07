"""`rli.replay.leakage`: spec.md §6's "future-leakage violations (0 target)".

The clean case is a real end-to-end replay (build with respx, replay without
it). Each violation kind is then PLANTED into that same trace, one at a time,
so the checker is shown to catch a thing that is genuinely there rather than
to agree with itself.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import respx
from test_replay_helpers import (
    NOW,
    OPEN_JOB,
    OPEN_URL,
    TENANT,
    dev_everything_cutoff,
    mock_ats,
    seed_corpus,
)

from rli.config import Config
from rli.models.time import to_utc_z
from rli.replay.build import build_dataset
from rli.replay.leakage import check_dataset, net_call_count
from rli.replay.mode import ReplayNetPool, ReplayViolation
from rli.replay.run import run_replay

DATASET = "ds-leak"
OPEN_POSTING = f"greenhouse:{TENANT}:{OPEN_JOB}"


@respx.mock
def _build(conn: sqlite3.Connection, cfg: Config) -> None:
    seed_corpus(conn)
    mock_ats()
    summary = build_dataset(
        conn,
        cfg,
        dataset_id=DATASET,
        split="dev",
        split_kind="temporal",
        grid_step_days=30,
        now=NOW,
        cutoff=dev_everything_cutoff(),
        use_tool_cache=False,
        collection_status_csv="/nonexistent/collection_status.csv",
    )
    assert summary.postings_failed == 0, summary.failures


def _replay_both(conn: sqlite3.Connection, cfg: Config) -> None:
    for system in ("A", "B"):
        summary = run_replay(conn, cfg, dataset_id=DATASET, system=system)
        assert summary.errors == 0, summary.describe()


def _a_replay_run(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM runs WHERE mode = 'replay' ORDER BY replay_at, id LIMIT 1"
    ).fetchone()
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# The clean case
# ---------------------------------------------------------------------------


def test_a_clean_replay_reports_zero_violations(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)

    report = check_dataset(conn, DATASET)
    assert report.clean, report.describe()
    assert report.total == 0
    assert report.counts == {}
    assert report.runs_checked > 0
    assert report.evidence_checked > 0
    assert report.steps_checked > 0
    assert report.failed_runs == 0
    assert report.systems == ("A", "B")
    assert "CLEAN (0 violations)" in report.describe()


def test_an_unbuilt_dataset_is_vacuously_clean(conn: sqlite3.Connection) -> None:
    report = check_dataset(conn, "never-built")
    assert report.clean
    assert report.runs_checked == 0


def test_the_checker_ignores_live_runs_and_other_datasets(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    # The build wrote live System A runs with evidence stamped at the build
    # instant; none of it may be counted against a replay dataset.
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'live'").fetchone()[0] > 0
    assert check_dataset(conn, DATASET).clean
    assert check_dataset(conn, "some-other-dataset").runs_checked == 0


# ---------------------------------------------------------------------------
# One planted violation per kind
# ---------------------------------------------------------------------------


def test_evidence_dated_after_t_is_caught(conn: sqlite3.Connection, cfg: Config) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    leaked_at = to_utc_z(NOW + timedelta(days=365))
    conn.execute(
        """
        INSERT INTO evidence
            (id, run_id, posting_id, probe, claim_type, value, source_url,
             source_quality, available_at, fetched_at)
        VALUES ('e999', ?, NULL, 'resolve_posting', 'first_published', '2027-01-01',
                ?, 'ats_native', ?, ?)
        """,
        (run["id"], OPEN_URL, leaked_at, leaked_at),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert not report.clean
    assert report.counts == {"evidence_after_t": 1}
    assert report.violations[0].kind == "evidence_after_t"
    assert report.violations[0].run_id == run["id"]
    assert "e999" in report.violations[0].detail


def test_a_cache_miss_in_a_replay_run_is_caught(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`'miss'` means "at least one call reached the network" (rli.eval.runner)."""
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    conn.execute(
        """
        UPDATE run_steps SET cache_status = 'miss'
        WHERE run_id = ? AND probe_name = 'resolve_posting'
        """,
        (run["id"],),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("cache_miss") == 1
    assert not report.clean


def test_a_recorded_net_call_attempt_is_caught(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    conn.execute(
        """
        INSERT INTO run_steps
            (run_id, step_index, component, decision_type, probe_name, error, created_at)
        VALUES (?, 999, 'controller', 'replay_violation:net_call', 'board_snapshot',
                'forbidden live tool call', ?)
        """,
        (run["id"], to_utc_z(NOW)),
    )
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("net_call") == 1


def test_a_dataset_gap_is_reported_as_a_missing_probe_result(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    conn.execute(
        """
        DELETE FROM replay_probe_results
        WHERE dataset_id = ? AND posting_id = ? AND probe_name = 'board_snapshot'
        """,
        (DATASET, OPEN_POSTING),
    )
    conn.commit()
    summary = run_replay(conn, cfg, dataset_id=DATASET, system="A")
    assert summary.violations > 0

    report = check_dataset(conn, DATASET)
    assert report.counts.get("missing_probe_result", 0) > 0
    assert report.failed_runs > 0
    assert not report.clean


def test_a_replay_run_without_a_t_cannot_be_audited_and_says_so(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)
    conn.execute("UPDATE runs SET replay_at = NULL WHERE id = ?", (run["id"],))
    conn.commit()

    report = check_dataset(conn, DATASET)
    assert report.counts.get("missing_replay_at") == 1


def test_the_itemized_list_is_capped_but_the_counts_are_not(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, cfg)
    _replay_both(conn, cfg)
    run = _a_replay_run(conn)

    leaked_at = to_utc_z(NOW + timedelta(days=365))
    for index in range(6):
        conn.execute(
            """
            INSERT INTO evidence
                (id, run_id, posting_id, probe, claim_type, value, source_url,
                 source_quality, available_at, fetched_at)
            VALUES (?, ?, NULL, 'resolve_posting', 'first_published', 'x',
                    ?, 'ats_native', ?, ?)
            """,
            (f"z{index}", run["id"], OPEN_URL, leaked_at, leaked_at),
        )
    conn.commit()

    report = check_dataset(conn, DATASET, max_items=2)
    assert report.counts["evidence_after_t"] == 6
    assert len(report.violations) == 2
    assert report.truncated == 4
    assert "and 4 more" in report.describe()


# ---------------------------------------------------------------------------
# The in-process counterpart
# ---------------------------------------------------------------------------


def test_net_call_count_counts_refused_attempts(cfg: Config) -> None:
    pool = ReplayNetPool(cfg=cfg)
    try:
        assert net_call_count(pool) == 0
        client = pool.client_for("board_snapshot")
        for _ in range(2):
            try:
                client.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
            except ReplayViolation:
                pass
        assert net_call_count(pool) == 2
    finally:
        pool.close()
