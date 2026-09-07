"""`evidence_quality` (spec.md §1) — every rule and every contradiction kind."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs
from rli.models.probe import ProbeResult
from rli.policy.action import PolicyThresholds
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_FIRST_PUBLISHED,
    CLAIM_POSTING_STATE,
)
from rli.policy.quality import (
    evidence_quality,
    evidence_quality_detail,
    find_contradictions,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

THRESHOLDS = PolicyThresholds(
    recent_publish_days=14,
    long_lived_days=180,
    contradiction_days=3,
    recheck_default_days=14,
    recheck_cap_days=14,
)

ATS_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"
PAGE_URL = "https://acme.com/careers/1"


def ev(
    eid: str,
    claim_type: str,
    *,
    probe: str = "resolve_posting",
    value: str = "x",
    source_quality: str = "ats_native",
    source_event_at: datetime | None = None,
    available_at: datetime | None = None,
    source_url: str = ATS_URL,
) -> EvidenceItem:
    stamp = available_at or NOW
    return EvidenceItem(
        id=eid,
        probe=probe,
        claim_type=claim_type,
        value=value,
        source_url=source_url,
        source_quality=source_quality,
        source_event_at=source_event_at,
        available_at=stamp,
        fetched_at=stamp,
    )


def publish(eid: str, *, days_ago: int, quality: str = "ats_native", url: str = ATS_URL):
    return ev(
        eid,
        CLAIM_FIRST_PUBLISHED,
        source_quality=quality,
        source_event_at=NOW - timedelta(days=days_ago),
        source_url=url,
    )


def state(eid: str = "e-state", value: str = "open", *, available_at: datetime | None = None):
    return ev(eid, CLAIM_POSTING_STATE, value=value, available_at=available_at)


OPEN_INPUTS = PolicyInputs(posting_state="open")


def quality_of(evidence, inputs=OPEN_INPUTS, failures=()):
    return evidence_quality(evidence, inputs, failures, THRESHOLDS)


# ---------------------------------------------------------------------------
# Q5 — strong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_quality", ["ats_native", "page_structured"])
def test_strong_needs_primary_publish_evidence_and_a_known_state(source_quality):
    evidence = [state(), publish("e1", days_ago=5, quality=source_quality)]
    verdict = evidence_quality_detail(evidence, OPEN_INPUTS, (), THRESHOLDS)
    assert verdict.quality == "strong"
    assert verdict.rule == "strong"
    assert verdict.contradictions == ()


@pytest.mark.parametrize("posting_state", ["open", "closed", "reposted"])
def test_strong_is_reachable_for_every_decisive_state(posting_state):
    evidence = [state(value=posting_state), publish("e1", days_ago=5)]
    inputs = PolicyInputs(posting_state=posting_state)
    assert quality_of(evidence, inputs) == "strong"


# ---------------------------------------------------------------------------
# Q1 — failures
# ---------------------------------------------------------------------------


def test_a_failed_always_run_probe_makes_the_case_weak():
    evidence = [state(), publish("e1", days_ago=5)]
    failure = ProbeResult(ok=False, error="connect timeout", retryable=True)
    verdict = evidence_quality_detail(evidence, OPEN_INPUTS, [failure], THRESHOLDS)
    assert verdict.quality == "weak"
    assert verdict.rule == "probe_failure"
    assert "connect timeout" in verdict.detail


def test_successful_probe_results_are_ignored():
    evidence = [state(), publish("e1", days_ago=5)]
    assert quality_of(evidence, failures=[ProbeResult(ok=True)]) == "strong"


def test_failure_outranks_a_contradiction():
    """Both weak rules beat `mixed` — the weakest verdict wins (documented)."""
    evidence = [
        state(),
        publish("e1", days_ago=5),
        publish("e2", days_ago=90, quality="page_structured", url=PAGE_URL),
    ]
    assert quality_of(evidence) == "mixed"
    assert quality_of(evidence, failures=[ProbeResult(ok=False, error="boom")]) == "weak"


# ---------------------------------------------------------------------------
# Q2 — missing / archive-only publish evidence
# ---------------------------------------------------------------------------


def test_no_publish_evidence_is_weak():
    verdict = evidence_quality_detail([state()], OPEN_INPUTS, (), THRESHOLDS)
    assert verdict.quality == "weak"
    assert verdict.rule == "no_primary_publish_evidence"


def test_archive_only_publish_evidence_is_weak():
    evidence = [state(), publish("e1", days_ago=5, quality="archive")]
    verdict = evidence_quality_detail(evidence, OPEN_INPUTS, (), THRESHOLDS)
    assert verdict.quality == "weak"
    assert verdict.rule == "no_primary_publish_evidence"
    assert "archive" in verdict.detail


def test_undated_publish_claim_does_not_count():
    evidence = [state(), ev("e1", CLAIM_FIRST_PUBLISHED, value="spring", source_event_at=None)]
    assert quality_of(evidence) == "weak"


# ---------------------------------------------------------------------------
# Q3 — unknown posting state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("posting_state", [UNKNOWN, "unknown"])
def test_unestablished_state_is_weak(posting_state):
    evidence = [publish("e1", days_ago=5)]
    inputs = PolicyInputs(posting_state=posting_state)
    verdict = evidence_quality_detail(evidence, inputs, (), THRESHOLDS)
    assert verdict.quality == "weak"
    assert verdict.rule == "posting_state_unknown"


# ---------------------------------------------------------------------------
# Q4 / C1 — conflicting publish dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gap_days", "expected"),
    [(0, "strong"), (3, "strong"), (4, "mixed"), (365, "mixed")],
)
def test_publish_date_conflict_threshold(gap_days, expected):
    evidence = [
        state(),
        publish("e1", days_ago=5),
        publish("e2", days_ago=5 + gap_days, quality="page_structured", url=PAGE_URL),
    ]
    assert quality_of(evidence) == expected


def test_publish_date_conflict_names_both_sources():
    evidence = [
        state(),
        publish("e1", days_ago=5),
        publish("e2", days_ago=90, quality="page_structured", url=PAGE_URL),
    ]
    contradictions = find_contradictions(evidence, THRESHOLDS)
    assert len(contradictions) == 1
    assert contradictions[0].kind == "publish_date_conflict"
    assert set(contradictions[0].evidence_ids) == {"e1", "e2"}


def test_repeated_claims_from_one_source_never_contradict_themselves():
    """Two fetches of the same URL are one source, even if the date drifted."""
    evidence = [
        state(),
        publish("e1", days_ago=5),
        ev(
            "e2",
            CLAIM_FIRST_PUBLISHED,
            source_event_at=NOW - timedelta(days=200),
            source_url=ATS_URL,
            available_at=NOW - timedelta(days=1),
        ),
    ]
    assert find_contradictions(evidence, THRESHOLDS) == []
    assert quality_of(evidence) == "strong"


def test_archive_publish_evidence_participates_in_the_conflict_check():
    evidence = [
        state(),
        publish("e1", days_ago=5),
        publish("e2", days_ago=200, quality="archive", url="https://web.archive.org/x"),
    ]
    assert quality_of(evidence) == "mixed"


def test_a_single_dated_source_cannot_contradict_anything():
    assert find_contradictions([publish("e1", days_ago=5)], THRESHOLDS) == []


def test_contradiction_threshold_is_configuration():
    evidence = [
        state(),
        publish("e1", days_ago=5),
        publish("e2", days_ago=15, quality="page_structured", url=PAGE_URL),
    ]
    assert quality_of(evidence) == "mixed"
    lenient = THRESHOLDS.model_copy(update={"contradiction_days": 30})
    assert evidence_quality(evidence, OPEN_INPUTS, (), lenient) == "strong"


# ---------------------------------------------------------------------------
# Q4 / C2 — open, but absent from a fresher board snapshot
# ---------------------------------------------------------------------------


def test_open_but_absent_from_a_fresher_board_snapshot_is_mixed():
    evidence = [
        state(available_at=NOW - timedelta(hours=2)),
        publish("e1", days_ago=5),
        ev(
            "e-absent",
            CLAIM_BOARD_ABSENT,
            probe="board_snapshot",
            value="job 1 not listed",
            available_at=NOW,
        ),
    ]
    verdict = evidence_quality_detail(evidence, OPEN_INPUTS, (), THRESHOLDS)
    assert verdict.quality == "mixed"
    assert verdict.contradictions[0].kind == "open_but_absent_from_board"
    assert set(verdict.contradictions[0].evidence_ids) == {"e-state", "e-absent"}


def test_a_stale_absence_is_history_not_a_contradiction():
    """Absence, then a fresher "open": the posting came back."""
    evidence = [
        state(available_at=NOW),
        publish("e1", days_ago=5),
        ev(
            "e-absent",
            CLAIM_BOARD_ABSENT,
            probe="board_snapshot",
            available_at=NOW - timedelta(days=3),
        ),
    ]
    assert find_contradictions(evidence, THRESHOLDS) == []
    assert quality_of(evidence) == "strong"


def test_absence_only_contradicts_an_open_state():
    evidence = [
        state(value="closed", available_at=NOW - timedelta(hours=2)),
        publish("e1", days_ago=5),
        ev("e-absent", CLAIM_BOARD_ABSENT, probe="board_snapshot", available_at=NOW),
    ]
    assert find_contradictions(evidence, THRESHOLDS) == []


def test_both_contradiction_kinds_are_reported_together():
    evidence = [
        state(available_at=NOW - timedelta(hours=2)),
        publish("e1", days_ago=5),
        publish("e2", days_ago=200, quality="page_structured", url=PAGE_URL),
        ev("e-absent", CLAIM_BOARD_ABSENT, probe="board_snapshot", available_at=NOW),
    ]
    kinds = {c.kind for c in find_contradictions(evidence, THRESHOLDS)}
    assert kinds == {"publish_date_conflict", "open_but_absent_from_board"}


def test_contradictions_are_reported_even_on_a_weak_verdict():
    """The trace keeps the conflict, even when a failure decided the verdict."""
    evidence = [
        state(),
        publish("e1", days_ago=5),
        publish("e2", days_ago=200, quality="page_structured", url=PAGE_URL),
    ]
    verdict = evidence_quality_detail(
        evidence, OPEN_INPUTS, [ProbeResult(ok=False, error="boom")], THRESHOLDS
    )
    assert verdict.quality == "weak"
    assert len(verdict.contradictions) == 1


def test_empty_evidence_is_weak():
    assert evidence_quality([], PolicyInputs(), (), THRESHOLDS) == "weak"


def test_evidence_quality_is_deterministic():
    evidence = [state(), publish("e1", days_ago=5)]
    assert quality_of(evidence) == quality_of(list(reversed(evidence)))
