"""Tests for `rli.history.titles` — the shared job-title quality policy.

This module is the single source of truth used by BOTH the archive HTML
extractor (`rli.archive.backfill`, at persist time) and the repost matcher
(`rli.history.matching`, at match time), so its behaviour is pinned here
rather than in either caller's tests.
"""

from __future__ import annotations

import pytest

from rli.config import load_config
from rli.history.titles import (
    dominant_title_fraction,
    is_degenerate_page,
    is_junk_title,
    junk_title_reason,
    normalize_title,
)

# The exact strings observed in `board_snapshot_jobs` for the 8,174 archive
# rows that a link-text scrape produced instead of a job title.
OBSERVED_JUNK = ["Apply", "Apply now", "Apply Now", "View", "View job", "", "   "]


@pytest.mark.parametrize("value", OBSERVED_JUNK)
def test_observed_scrape_junk_is_rejected(value: str) -> None:
    assert is_junk_title(value) is True
    assert junk_title_reason(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "»",  # punctuation only
        "AI",  # shorter than the 3-character minimum
        "12345",  # digits only
        "Learn more",
        "See details",
        "Remote",  # describes a job without naming the role
        "Full time",
        "Careers",
        "Apply for this job",
    ],
)
def test_other_non_titles_are_rejected(value: str | None) -> None:
    assert is_junk_title(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "Senior Backend Engineer",
        "Staff Software Engineer, Payments",
        "Application Security Engineer",  # substring "appl" must NOT trigger
        "Head of Careers",  # substring "careers" must NOT trigger
        "Learning Experience Designer",  # "learn" prefix but a real, long title
        "Remote Site Reliability Engineer",  # "remote" only as a modifier
        "View Analytics Product Manager, Growth",  # >4 tokens, so not a CTA
        "SRE",  # exactly the 3-character minimum
        # Non-Latin scripts must survive normalization. This exact title is
        # in the live `board_snapshot_jobs` table and an ASCII-only
        # normalizer folded it to "" and rejected it as junk.
        "アカウント営業/リテール",
        "Ingénieur logiciel",
        "Разработчик",
    ],
)
def test_real_titles_are_accepted(value: str) -> None:
    assert is_junk_title(value) is False, junk_title_reason(value)


def test_normalization_folds_case_and_punctuation() -> None:
    assert normalize_title("Sr. Engineer (Remote)") == "sr engineer remote"
    assert normalize_title("APPLY!") == "apply"
    assert normalize_title(None) == ""


def test_normalization_is_unicode_aware() -> None:
    assert normalize_title("アカウント営業/リテール") == "アカウント営業 リテール"
    assert normalize_title("Café_Manager") == "café manager"


def test_the_matcher_and_the_title_policy_normalize_identically() -> None:
    """They must agree, or a title could be accepted here and score 0 there."""
    from rli.history.matching import normalize_text

    for value in ["Sr. Engineer (Remote)", "アカウント営業/リテール", "Café_Manager", "", "APPLY!"]:
        assert normalize_text(value) == normalize_title(value)


def test_min_chars_is_configurable() -> None:
    assert is_junk_title("SRE", min_chars=3) is False
    assert is_junk_title("SRE", min_chars=4) is True


def test_min_chars_default_matches_the_config_default() -> None:
    # The extractor and the matcher both pass `cfg.matching.junk_title_min_chars`;
    # the module default must not silently disagree with it.
    assert load_config().matching.junk_title_min_chars == 3


# ---------------------------------------------------------------------------
# Page-level degeneracy
# ---------------------------------------------------------------------------


def test_dominant_title_fraction_counts_the_most_common_normalized_title() -> None:
    assert dominant_title_fraction(["Apply", "apply", "Backend Engineer"]) == pytest.approx(2 / 3)
    assert dominant_title_fraction([]) == 0.0


def test_page_where_one_title_dominates_is_degenerate() -> None:
    titles = ["Software Engineer"] * 7 + ["Product Manager", "Designer"]
    assert dominant_title_fraction(titles) > 0.60
    assert is_degenerate_page(titles) is True


def test_a_varied_page_is_not_degenerate() -> None:
    titles = ["Backend Engineer", "Frontend Engineer", "Designer", "Backend Engineer"]
    assert is_degenerate_page(titles) is False


def test_the_fraction_rule_is_not_applied_below_the_minimum_job_count() -> None:
    # 1 of 1 and 2 of 2 are 100% by arithmetic and say nothing about the
    # scrape, so the rule must not fire there.
    assert is_degenerate_page(["Backend Engineer"]) is False
    assert is_degenerate_page(["Backend Engineer", "Backend Engineer"]) is False
    assert is_degenerate_page(["Backend Engineer"] * 3) is True
