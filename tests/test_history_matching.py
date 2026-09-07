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
from rli.history.closures import PostingInterval, apply_to_postings, build_intervals
from rli.history.matching import link_reposts, rank_matches, score_pair
from rli.history.sample import MATCH_SAMPLE_COLUMNS, export_match_sample

OLD_TITLE = "Senior Backend Engineer"
OLD_TEAM = "Infrastructure"
OLD_LOCATION = "San Francisco, CA"
OLD_HASH = "sha256:aaaa"

# A near-title that scores between `[matching].title_min` (0.85) and
# `title_only_min` (0.95), so it exercises the gap between the two bars.
SIMILAR_TITLE = "Senior Backend Engineer I"

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
    assert near.components.title < cfg.matching.title_min
    assert near.components.description == 0.0
    assert near.combined < cfg.matching.combined_min
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
    _build_history(conn, repost_day=cfg.matching.max_gap_days + 10)

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


# ---------------------------------------------------------------------------
# Hard gates added to stop the false-positive families seen on real data
# (data/match_sample.csv: 37,776 links, most of them junk-title pairings).
# ---------------------------------------------------------------------------


def _pair(**overrides):
    """Two hand-built intervals for gate tests, disjoint in time and captures.

    Building `PostingInterval`s directly (rather than through captures) is
    what makes it possible to test ONE gate at a time: several of the gates
    subsume each other when driven from realistic capture histories, so a
    capture-driven test could not tell which gate did the rejecting.
    """
    old = PostingInterval(
        company_id=COMPANY,
        job_id="old1",
        posting_id=OLD_POSTING,
        title=OLD_TITLE,
        team=OLD_TEAM,
        location=OLD_LOCATION,
        description_hash=OLD_HASH,
        first_observed=at(0),
        last_seen_open=at(1),
        first_seen_absent=at(2),
        closure_absent_at=at(2),
        censoring="interval",
        gap_days=1.0,
        present_snapshot_ids=(1, 2),
    )
    new = PostingInterval(
        company_id=COMPANY,
        job_id="twin1",
        posting_id=TWIN_POSTING,
        title=OLD_TITLE,
        team=OLD_TEAM,
        location=OLD_LOCATION,
        description_hash=OLD_HASH,
        first_observed=at(3),
        last_seen_open=at(4),
        censoring="right",
        present_snapshot_ids=(4, 5),
    )
    return old.model_copy(update=overrides.get("old", {})), new.model_copy(
        update=overrides.get("new", {})
    )


def test_the_hand_built_control_pair_matches(conn: sqlite3.Connection, cfg: Config) -> None:
    """Control for every gate test below: unmodified, this pair is a match."""
    old, new = _pair()
    scored = score_pair(old, new, cfg)
    assert scored is not None and scored.is_match and scored.combined == 1.0


# --- junk titles -----------------------------------------------------------


def test_junk_titles_are_never_matched(conn: sqlite3.Connection, cfg: Config) -> None:
    """The gohighlevel.com family: "Apply" -> "Apply" scored a perfect 1.0."""
    old, new = _pair(old={"title": "Apply"}, new={"title": "Apply"})
    assert score_pair(old, new, cfg) is None

    # A junk title on EITHER side is enough to reject the pair.
    old, new = _pair(new={"title": "View"})
    assert score_pair(old, new, cfg) is None
    old, new = _pair(old={"title": ""})
    assert score_pair(old, new, cfg) is None


def test_junk_titles_are_excluded_end_to_end(conn: sqlite3.Connection, cfg: Config) -> None:
    add_posting(conn, job_id="old1", title="Apply")
    add_posting(conn, job_id="twin1", title="Apply")
    add_capture(conn, at(0), [job("old1", title="Apply", url="https://x/old1")])
    add_capture(conn, at(1), [])
    add_capture(conn, at(2), [job("twin1", title="Apply", url="https://x/twin1")])
    apply_to_postings(conn)

    assert rank_matches(conn, cfg, COMPANY) == []
    summary = link_reposts(conn, cfg)
    assert summary.matches == 0
    assert conn.execute("SELECT COUNT(*) FROM repost_links").fetchone()[0] == 0


# --- temporal ordering -----------------------------------------------------


def test_candidate_first_observed_before_the_absence_is_bounded_by_the_tolerance(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    tolerance = cfg.matching.pre_absence_tolerance_days
    assert tolerance > 0, "the fixture config must exercise a real tolerance"

    # Inside the tolerance: a repost first seen just before we observed the
    # old posting gone is still admissible (captures are sparse).
    old, new = _pair(
        old={"last_seen_open": at(0), "first_seen_absent": at(20)},
        new={"first_observed": at(20 - tolerance / 2)},
    )
    scored = score_pair(old, new, cfg)
    assert scored is not None and scored.gap_days < 0

    # Beyond it: the candidate was already live far too long before the old
    # posting was ever observed absent.
    old, new = _pair(
        old={"last_seen_open": at(0), "first_seen_absent": at(20)},
        new={"first_observed": at(20 - tolerance - 1)},
    )
    assert score_pair(old, new, cfg) is None


def test_candidate_at_the_exact_absence_capture_is_admissible(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The canonical archive shape: one capture shows old gone and new present."""
    old, new = _pair(new={"first_observed": at(2)})
    scored = score_pair(old, new, cfg)
    assert scored is not None and scored.gap_days == 0.0 and scored.is_match


def test_candidate_not_after_last_seen_open_is_rejected(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    old, new = _pair(new={"first_observed": at(1)})  # == old.last_seen_open
    assert score_pair(old, new, cfg) is None


# --- coexistence -----------------------------------------------------------


def test_postings_seen_together_in_one_capture_are_never_a_repost_pair(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Two jobs listed side by side on one board are coexisting roles."""
    old, new = _pair(new={"present_snapshot_ids": (2, 4, 5)})  # shares capture 2
    assert old.coexists_with(new) is True
    assert score_pair(old, new, cfg) is None

    # Control: the identical pair with disjoint captures matches.
    old, new = _pair(new={"present_snapshot_ids": (4, 5)})
    assert score_pair(old, new, cfg) is not None


def test_present_snapshot_ids_are_derived_from_the_captures(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    first = add_capture(conn, at(0), [_old_job(), _repost_job()])
    second = add_capture(conn, at(1), [_old_job()])

    intervals = build_intervals(conn, COMPANY)
    assert interval_by_job(intervals, "old1").present_snapshot_ids == (first, second)
    assert interval_by_job(intervals, "twin1").present_snapshot_ids == (first,)


# --- one-to-one assignment -------------------------------------------------


def _build_many_to_many(conn: sqlite3.Connection) -> None:
    """Two identical roles close, then two identical roles appear.

    Every one of the four cross pairs scores a perfect 1.0, so without the
    one-to-one assignment this history produces four links from two
    closures.
    """
    for job_id in ("old1", "old2", "new1", "new2"):
        add_posting(conn, job_id=job_id, title=OLD_TITLE, team=OLD_TEAM, location=OLD_LOCATION)

    def listing(job_id: str):
        return job(
            job_id,
            title=OLD_TITLE,
            team=OLD_TEAM,
            location=OLD_LOCATION,
            description_hash=OLD_HASH,
        )

    add_capture(conn, at(0), [listing("old1"), listing("old2")])
    add_capture(conn, at(1), [])
    add_capture(conn, at(2), [listing("new1"), listing("new2")])
    apply_to_postings(conn)


def test_each_posting_participates_in_at_most_one_accepted_match(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _build_many_to_many(conn)

    ranked = rank_matches(conn, cfg, COMPANY)
    accepted = [c for c in ranked if c.is_match]

    # All four pairs pass the thresholds; only two survive the assignment.
    assert sum(1 for c in ranked if c.passes_thresholds) == 4
    assert len(accepted) == 2
    assert len({c.old_job_id for c in accepted}) == 2
    assert len({c.new_job_id for c in accepted}) == 2

    # The losers are flagged as assignment rejections, not threshold ones,
    # so the hand-check CSV can tell the two apart.
    losers = [c for c in ranked if c.passes_thresholds and not c.is_match]
    assert len(losers) == 2
    assert {c.reject_reason for c in losers} == {"assignment"}


def test_link_reposts_writes_one_link_per_posting(conn: sqlite3.Connection, cfg: Config) -> None:
    _build_many_to_many(conn)

    summary = link_reposts(conn, cfg)

    assert summary.matches == 2
    assert summary.links_written == 2
    assert summary.replacements_set == 2

    links = conn.execute("SELECT old_posting_id, new_posting_id FROM repost_links").fetchall()
    assert len({row["old_posting_id"] for row in links}) == 2
    assert len({row["new_posting_id"] for row in links}) == 2


def test_a_repost_already_claimed_by_another_posting_is_not_reused(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The cross-RUN half of the one-to-one invariant."""
    _build_many_to_many(conn)
    # old2 is pre-claimed against the repost the assignment would give old1.
    conn.execute(
        "UPDATE postings SET replacement_job_id = ? WHERE posting_id = ?",
        ("greenhouse:acme:new1", "greenhouse:acme:old2"),
    )
    conn.commit()

    link_reposts(conn, cfg)

    replacements = [
        row["replacement_job_id"]
        for row in conn.execute(
            "SELECT replacement_job_id FROM postings WHERE replacement_job_id IS NOT NULL"
        )
    ]
    assert len(replacements) == len(set(replacements)), "a repost was claimed twice"


# --- version, not repost ---------------------------------------------------


def test_a_version_change_under_a_stable_job_id_is_not_a_repost(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §4: a content change under the SAME ATS job id is a version.

    The job disappears and comes back under its own id with a new content
    hash. `reappeared_at` records that (it is the same job listed again),
    but nothing may be linked as a REPOST — a repost is a different job id.
    """
    posting_id = add_posting(conn, job_id="v1", title=OLD_TITLE)
    add_capture(conn, at(0), [job("v1", title=OLD_TITLE, description_hash="sha256:aaaa")])
    add_capture(conn, at(1), [])
    add_capture(conn, at(2), [job("v1", title=OLD_TITLE, description_hash="sha256:bbbb")])
    apply_to_postings(conn)

    assert rank_matches(conn, cfg, COMPANY) == []
    link_reposts(conn, cfg)

    row = posting_row(conn, posting_id)
    assert row["replacement_job_id"] is None
    assert row["reappeared_at"] is not None  # the version WAS seen again
    assert conn.execute("SELECT COUNT(*) FROM repost_links").fetchone()[0] == 0


def test_a_continuously_open_job_whose_content_hash_changes_never_closes(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    add_posting(conn, job_id="v1", title=OLD_TITLE)
    add_capture(conn, at(0), [job("v1", title=OLD_TITLE, description_hash="sha256:aaaa")])
    add_capture(conn, at(1), [job("v1", title=OLD_TITLE, description_hash="sha256:bbbb")])
    apply_to_postings(conn)

    interval = interval_by_job(build_intervals(conn, COMPANY), "v1")
    assert interval.first_seen_absent is None
    assert interval.censoring == "right"
    assert rank_matches(conn, cfg, COMPANY) == []


def test_two_job_ids_sharing_a_canonical_url_are_one_job(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    url = "https://boards.greenhouse.io/acme/jobs/1"
    old, new = _pair(old={"url": url}, new={"url": url + "/"})
    assert score_pair(old, new, cfg) is None


# --- corroboration ---------------------------------------------------------


def test_a_title_only_pair_must_clear_the_stricter_bar(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    bare = {"team": None, "location": None, "description_hash": None}

    # Identical titles: 1.0 >= title_only_min, so it still matches.
    old, new = _pair(old=bare, new=bare)
    scored = score_pair(old, new, cfg)
    assert scored is not None
    assert scored.title_only is True and scored.corroborated is False and scored.is_match

    # Merely SIMILAR titles clear `title_min` but not `title_only_min`, and
    # with nothing to corroborate them they must be rejected. (SIMILAR_TITLE
    # is chosen to land between the two bars — see the assertion below.)
    old, new = _pair(old=bare, new=dict(bare, title=SIMILAR_TITLE))
    scored = score_pair(old, new, cfg)
    assert scored is not None
    assert cfg.matching.title_min <= scored.components.title < cfg.matching.title_only_min
    assert scored.title_only is True
    assert scored.combined >= cfg.matching.combined_min  # only the title bar rejects it
    assert scored.passes_thresholds is False and scored.reject_reason == "thresholds"


def test_a_perfect_title_still_needs_a_corroborating_component(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Identical titles are not on their own evidence of a repost.

    Team and description are unknown on both sides here, so location is the
    only component that COULD corroborate; the combined score clears
    `combined_min` either way, which isolates the corroboration rule as the
    only thing that can reject the pair.
    """
    thin = {"team": None, "description_hash": None}

    old, new = _pair(old=thin, new=dict(thin, location="Berlin, Germany"))
    scored = score_pair(old, new, cfg)
    assert scored is not None
    assert scored.components.title == 1.0
    assert scored.components.location < cfg.matching.location_min
    assert scored.title_only is False  # location IS known, it just disagrees
    assert scored.combined >= cfg.matching.combined_min
    assert scored.corroborated is False
    assert scored.passes_thresholds is False

    # Control: the same pair with a matching location is corroborated.
    old, new = _pair(old=thin, new=thin)
    scored = score_pair(old, new, cfg)
    assert scored is not None
    assert scored.corroborated is True and scored.passes_thresholds is True
