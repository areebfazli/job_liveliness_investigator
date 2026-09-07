"""Tests for `rli.history.cli` — the `rebuild` / `sample` sub-app.

`rebuild` exists to undo write-once derived state so a threshold change can
take effect. Its correctness property is that it is a fixed point: running
it twice over unchanged captures leaves the database byte-identical in the
columns it owns.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from test_history_helpers import (
    COMPANY,
    OTHER_COMPANY,
    add_capture,
    add_posting,
    at,
    job,
    posting_row,
)
from typer.testing import CliRunner

from rli.config import Config
from rli.history.cli import app, rebuild
from rli.history.closures import apply_to_postings
from rli.history.matching import link_reposts
from rli.history.sample import MATCH_SAMPLE_COLUMNS

TITLE = "Senior Backend Engineer"
TEAM = "Infrastructure"
LOCATION = "San Francisco, CA"
HASH = "sha256:aaaa"

OLD_POSTING = "greenhouse:acme:old1"
TWIN_POSTING = "greenhouse:acme:twin1"


def _listing(job_id: str):
    return job(job_id, title=TITLE, team=TEAM, location=LOCATION, description_hash=HASH)


def _build(conn: sqlite3.Connection, company_id: str = COMPANY, suffix: str = "") -> None:
    """old closes, then an identical role appears under a new id."""
    add_posting(conn, job_id=f"old1{suffix}", company_id=company_id, title=TITLE)
    add_posting(conn, job_id=f"twin1{suffix}", company_id=company_id, title=TITLE)
    add_capture(conn, at(0), [_listing(f"old1{suffix}")], company_id=company_id)
    add_capture(conn, at(1), [], company_id=company_id)
    add_capture(conn, at(2), [_listing(f"twin1{suffix}")], company_id=company_id)


def _derived_state(conn: sqlite3.Connection) -> tuple[list[tuple], list[tuple]]:
    postings = conn.execute(
        "SELECT posting_id, replacement_job_id, reappeared_at FROM postings ORDER BY posting_id"
    ).fetchall()
    links = conn.execute(
        """
        SELECT company_id, old_posting_id, new_posting_id, combined_score, component_scores
        FROM repost_links ORDER BY old_posting_id, new_posting_id
        """
    ).fetchall()
    return [tuple(r) for r in postings], [tuple(r) for r in links]


def test_rebuild_reproduces_the_state_a_plain_run_produces(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn)
    apply_to_postings(conn)
    link_reposts(conn, cfg)
    baseline = _derived_state(conn)

    rebuild(conn)

    assert _derived_state(conn) == baseline


def test_rebuild_is_idempotent(conn: sqlite3.Connection) -> None:
    _build(conn)

    first_cleared, first_deleted, _, first_reposts = rebuild(conn)
    after_first = _derived_state(conn)

    second_cleared, second_deleted, _, second_reposts = rebuild(conn)

    # The first run had nothing of its own to clear; the second cleared
    # exactly what the first wrote.
    assert (first_cleared, first_deleted) == (0, 0)
    assert (second_cleared, second_deleted) == (1, 1)
    # And the derived state itself is unchanged: rebuild is a fixed point.
    assert _derived_state(conn) == after_first
    assert second_reposts.matches == first_reposts.matches == 1
    assert second_reposts.links_written == first_reposts.links_written == 1
    assert second_reposts.replacements_set == first_reposts.replacements_set == 1


def test_rebuild_picks_up_new_captures(conn: sqlite3.Connection) -> None:
    _build(conn)
    rebuild(conn)
    assert posting_row(conn, OLD_POSTING)["replacement_job_id"] == TWIN_POSTING

    # A later, better-scoring capture history must be re-derivable even
    # though `replacement_job_id` is write-once for `link_reposts`.
    conn.execute("DELETE FROM board_snapshot_jobs")
    conn.execute("DELETE FROM board_snapshots")
    conn.commit()
    add_posting(conn, job_id="new2", title=TITLE)
    add_capture(conn, at(0), [_listing("old1")])
    add_capture(conn, at(1), [])
    add_capture(conn, at(2), [_listing("new2")])

    rebuild(conn)

    assert posting_row(conn, OLD_POSTING)["replacement_job_id"] == "greenhouse:acme:new2"
    assert conn.execute("SELECT COUNT(*) FROM repost_links").fetchone()[0] == 1


def test_rebuild_keeps_a_closure_derived_reappeared_at(conn: sqlite3.Connection) -> None:
    """A posting that reappeared under its OWN id was never repost-linked.

    Its `reappeared_at` comes from `rli.history.closures`, so the clear step
    must not be able to lose it — `apply_to_postings` re-derives it before
    `link_reposts` runs.
    """
    posting_id = add_posting(conn, job_id="v1", title=TITLE)
    add_capture(conn, at(0), [_listing("v1")])
    add_capture(conn, at(1), [])
    add_capture(conn, at(2), [_listing("v1")])
    apply_to_postings(conn)
    expected = posting_row(conn, posting_id)["reappeared_at"]
    assert expected is not None

    rebuild(conn)

    assert posting_row(conn, posting_id)["reappeared_at"] == expected
    assert posting_row(conn, posting_id)["replacement_job_id"] is None


def test_rebuild_scoped_to_one_company_leaves_the_others_alone(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build(conn, COMPANY)
    _build(conn, OTHER_COMPANY, suffix="b")
    apply_to_postings(conn)
    link_reposts(conn, cfg)

    other_links = conn.execute(
        "SELECT COUNT(*) FROM repost_links WHERE company_id = ?", (OTHER_COMPANY,)
    ).fetchone()[0]
    assert other_links == 1

    cleared, deleted, _, _ = rebuild(conn, COMPANY)

    assert (cleared, deleted) == (1, 1)
    # The other company's derived rows were never touched.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM repost_links WHERE company_id = ?", (OTHER_COMPANY,)
        ).fetchone()[0]
        == 1
    )
    assert posting_row(conn, "greenhouse:acme:old1b")["replacement_job_id"] is not None


# ---------------------------------------------------------------------------
# Typer surface
# ---------------------------------------------------------------------------


def test_rebuild_command_runs_against_a_database_file(tmp_path: Path) -> None:
    from rli.db import connect, init_db

    db_path = tmp_path / "rli.sqlite3"
    init_db(db_path)
    seeded = connect(db_path)
    try:
        _build(seeded)
    finally:
        seeded.close()

    result = CliRunner().invoke(app, ["rebuild", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "closures:" in result.output
    assert "reposts:" in result.output

    check = connect(db_path)
    try:
        assert posting_row(check, OLD_POSTING)["replacement_job_id"] == TWIN_POSTING
    finally:
        check.close()


def test_sample_command_writes_the_hand_check_csv(tmp_path: Path) -> None:
    from rli.db import connect, init_db

    db_path = tmp_path / "rli.sqlite3"
    out_path = tmp_path / "out" / "match_sample.csv"
    init_db(db_path)
    seeded = connect(db_path)
    try:
        _build(seeded)
        apply_to_postings(seeded)
    finally:
        seeded.close()

    result = CliRunner().invoke(
        app, ["sample", "--db", str(db_path), "--out", str(out_path), "--n", "5"]
    )

    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(out_path.open(encoding="utf-8")))
    assert list(rows[0]) == MATCH_SAMPLE_COLUMNS
    assert rows[0]["is_match"] == "true"
