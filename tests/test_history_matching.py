"""Tests for `rli.history.matching` (+ the hand-check CSV in `rli.history.sample`)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

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

from rli.config import Config
from rli.history.closures import apply_to_postings, build_intervals
from rli.history.matching import link_reposts, rank_matches, score_pair
from rli.history.sample import MATCH_SAMPLE_COLUMNS, export_match_sample

OLD_TITLE = "Senior Backend Engineer"
OLD_TEAM = "Infrastructure"
OLD_LOCATION = "San Francisco, CA"
OLD_HASH = "sha256:aaaa"

OLD_POSTING = "greenhouse:acme:old1"
TWIN_POSTING = "greenhouse:acme:twin1"
NEAR_POSTING = "greenhouse:acme:near1"


def _old_job(job_id: str = "old1"):
    return job(
        job_id,
        title=OLD_TITLE,
        team=OLD_TEAM,
        location=OLD_LOCATION,
        description_hash=OLD_HASH,
    )


def _repost_job(job_id: str = "twin1"):
    """A textbook repost: identical title, team, location and content hash."""
    return job(
        job_id,
        title=OLD_TITLE,
        team=OLD_TEAM,
        location=OLD_LOCATION,
        description_hash=OLD_HASH,
    )


def _near_miss_job(job_id: str = "near1"):
    """Similar-sounding title, but a different team, location and content."""
    return job(
        job_id,
        title="Senior Backend Engineer, Payments Platform",
        team="Payments",
        location="New York, NY",
        description_hash="sha256:bbbb",
    )


def _build_history(conn: sqlite3.Connection, *, repost_day: int = 3) -> None:
    """old1 open on days 0-1, gone on day 2, reposted as twin1 on `repost_day`."""
    add_posting(conn, job_id="old1", title=OLD_TITLE, team=OLD_TEAM, location=OLD_LOCATION)
    add_posting(conn, job_id="twin1", title=OLD_TITLE, team=OLD_TEAM, location=OLD_LOCATION)
    add_posting(conn, job_id="near1", title="Senior Backend Engineer, Payments Platform")

    add_capture(conn, at(0), [_old_job()])
    add_capture(conn, at(1), [_old_job()])
    add_capture(conn, at(2), [])
    add_capture(conn, at(repost_day), [_repost_job(), _near_miss_job()])

    apply_to_postings(conn)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_true_positive_scores_above_threshold(conn: sqlite3.Connection, cfg: Config) -> None:
    _build_history(conn)

    ranked = rank_matches(conn, cfg, COMPANY)
    best = ranked[0]

    assert (best.old_job_id, best.new_job_id) == ("old1", "twin1")
    assert best.is_match is True
    assert best.combined == 1.0
    assert best.components.title == 1.0
    assert best.components.description == 1.0
    assert best.passed == {"title": True, "team": True, "location": True, "description": True}
    assert best.gap_days == 1.0


def test_near_miss_scores_below_threshold(conn: sqlite3.Connection, cfg: Config) -> None:
    _build_history(conn)

    ranked = rank_matches(conn, cfg, COMPANY)
    near = next(c for c in ranked if (c.old_job_id, c.new_job_id) == ("old1", "near1"))

    assert near.is_match is False
    assert near.components.title < cfg.thresholds.repost_title_similarity
    assert near.components.description == 0.0
    assert near.combined < cfg.thresholds.repost_combined_min
    # It is still SCORED and returned, so the hand-check sample can show how
    # close the threshold came to firing.
    assert near in ranked


def test_wrong_company_candidate_is_excluded_regardless_of_score(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_history(conn)
    # A different company posts a byte-identical role the very next day.
    add_capture(conn, at(3), [_repost_job("clone1")], company_id=OTHER_COMPANY)
    apply_to_postings(conn)

    ranked = rank_matches(conn, cfg)

    assert all(c.new_job_id != "clone1" for c in ranked)
    assert all(c.company_id == COMPANY for c in ranked)

    # Directly: the pair scores perfectly on every component and is STILL
    # rejected, because the company gate runs before any scoring.
    old = interval_by_job(build_intervals(conn, COMPANY), "old1")
    clone = interval_by_job(build_intervals(conn, OTHER_COMPANY), "clone1")
    assert score_pair(old, clone, cfg) is None
    # Control: the very same candidate, with ONLY its company_id changed to
    # match, is a perfect-scoring match — so the exclusion above is the
    # company gate and nothing else.
    same_company_clone = clone.model_copy(update={"company_id": COMPANY})
    control = score_pair(old, same_company_clone, cfg)
    assert control is not None and control.is_match and control.combined == 1.0


def test_candidate_beyond_max_gap_days_is_not_considered(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_history(conn, repost_day=cfg.thresholds.repost_max_gap_days + 10)

    ranked = rank_matches(conn, cfg, COMPANY)

    assert all(c.new_job_id != "twin1" for c in ranked)


def test_candidate_predating_the_old_postings_last_open_sighting_is_rejected(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="old1", title=OLD_TITLE)
    add_posting(conn, job_id="twin1", title=OLD_TITLE)
    add_capture(conn, at(0), [_old_job(), _repost_job()])
    add_capture(conn, at(1), [_repost_job()])
    add_capture(conn, at(2), [])
    apply_to_postings(conn)

    ranked = rank_matches(conn, cfg, COMPANY)

    # twin1 was already open while old1 was still listed, so it cannot be
    # old1's replacement.
    assert all((c.old_job_id, c.new_job_id) != ("old1", "twin1") for c in ranked)


def test_missing_component_is_excluded_rather_than_scored_zero(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="old1", title=OLD_TITLE)
    add_posting(conn, job_id="twin1", title=OLD_TITLE)
    # No team, no location, no description hash on either side.
    add_capture(conn, at(0), [job("old1", title=OLD_TITLE)])
    add_capture(conn, at(1), [])
    add_capture(conn, at(2), [job("twin1", title=OLD_TITLE)])
    apply_to_postings(conn)

    best = rank_matches(conn, cfg, COMPANY)[0]

    assert best.components.team is None
    assert best.components.location is None
    assert best.components.description is None
    assert best.passed == {"title": True}
    # Title alone carries the whole (renormalized) weight — a missing field
    # must not be scored as a mismatch.
    assert best.combined == 1.0
    assert best.is_match is True


# ---------------------------------------------------------------------------
# link_reposts
# ---------------------------------------------------------------------------


def test_link_reposts_sets_replacement_and_writes_the_audit_row(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_history(conn)

    summary = link_reposts(conn, cfg)

    assert summary.matches == 1
    assert summary.links_written == 1
    assert summary.replacements_set == 1

    old = posting_row(conn, OLD_POSTING)
    assert old["replacement_job_id"] == TWIN_POSTING
    assert old["reappeared_at"] == "2026-01-04T12:00:00.000000Z"

    link = conn.execute("SELECT * FROM repost_links").fetchone()
    assert link["company_id"] == COMPANY
    assert link["old_posting_id"] == OLD_POSTING
    assert link["new_posting_id"] == TWIN_POSTING
    assert link["combined_score"] == 1.0
    payload = json.loads(link["component_scores"])
    assert payload["scores"] == {
        "title": 1.0,
        "team": 1.0,
        "location": 1.0,
        "description": 1.0,
    }
    assert payload["passed"]["description"] is True
    assert link["matched_at"].endswith("Z")


def test_link_reposts_never_links_the_near_miss(conn: sqlite3.Connection, cfg: Config) -> None:
    _build_history(conn)

    link_reposts(conn, cfg)

    linked = {row["new_posting_id"] for row in conn.execute("SELECT * FROM repost_links")}
    assert NEAR_POSTING not in linked
    assert posting_row(conn, NEAR_POSTING)["replacement_job_id"] is None


def test_link_reposts_is_idempotent_and_keeps_an_existing_replacement(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_history(conn)
    link_reposts(conn, cfg)

    conn.execute(
        "UPDATE postings SET replacement_job_id = ? WHERE posting_id = ?",
        (NEAR_POSTING, OLD_POSTING),
    )
    conn.commit()

    second = link_reposts(conn, cfg)

    assert second.links_written == 0
    assert second.links_already_present == 1
    assert second.replacements_set == 0
    assert second.replacements_kept == 1
    # The pre-existing (manually set) link is preserved, not overwritten.
    assert posting_row(conn, OLD_POSTING)["replacement_job_id"] == NEAR_POSTING
    assert conn.execute("SELECT COUNT(*) FROM repost_links").fetchone()[0] == 1


def test_link_reposts_skips_candidates_without_posting_rows(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    # Captures only, and apply_to_postings deliberately NOT run, so no
    # postings row exists for either side of the pair.
    add_capture(conn, at(0), [_old_job()])
    add_capture(conn, at(1), [])
    add_capture(conn, at(2), [_repost_job()])

    summary = link_reposts(conn, cfg)

    assert summary.matches == 1
    assert summary.unresolved_postings == 1
    assert summary.links_written == 0
    assert conn.execute("SELECT COUNT(*) FROM repost_links").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# sample.export_match_sample
# ---------------------------------------------------------------------------


def test_export_match_sample_writes_hand_check_columns(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    _build_history(conn)
    destination = tmp_path / "sample" / "match_precision.csv"

    written = export_match_sample(conn, cfg, destination, n=50)

    rows = list(csv.DictReader(destination.open(encoding="utf-8")))
    assert written == len(rows) >= 2
    assert list(rows[0]) == MATCH_SAMPLE_COLUMNS

    top = rows[0]
    assert top["company_id"] == COMPANY
    assert top["old_posting_id"] == OLD_POSTING
    assert top["new_posting_id"] == TWIN_POSTING
    assert top["is_match"] == "true"
    assert top["human_verdict"] == ""
    # Near-misses are exported too, flagged, so threshold sensitivity is
    # visible in the same file.
    assert any(row["is_match"] == "false" for row in rows)


def test_export_match_sample_honours_n_and_matches_only(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    _build_history(conn)

    only_one = tmp_path / "one.csv"
    assert export_match_sample(conn, cfg, only_one, n=1) == 1

    strict = tmp_path / "strict.csv"
    export_match_sample(conn, cfg, strict, n=50, matches_only=True)
    rows = list(csv.DictReader(strict.open(encoding="utf-8")))
    assert rows and all(row["is_match"] == "true" for row in rows)


def test_export_match_sample_writes_a_header_only_file_for_empty_history(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    destination = tmp_path / "empty.csv"

    assert export_match_sample(conn, cfg, destination, n=50) == 0
    assert destination.read_text(encoding="utf-8").strip() == ",".join(MATCH_SAMPLE_COLUMNS)
