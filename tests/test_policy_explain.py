"""The deterministic explanation fallback (spec.md §1 `reason`, §9 citations).

The invariant under test throughout: every reason cites at least one evidence
id, and every cited id exists in the evidence handed in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rli.events.policy_signals import CLAIM_EVENTS_SEARCHED, format_event_claim_value
from rli.models.decision import Decision
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs
from rli.policy.explain_stub import _enforce_citations, reasons_from_inputs
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_DECLARED_EXPIRY,
    CLAIM_FIRST_PUBLISHED,
    CLAIM_POSTING_STATE,
    CLAIM_REFRESHED_AT,
    CLAIM_TEAM_SIGNAL,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def ev(
    eid: str,
    claim_type: str,
    *,
    probe: str = "resolve_posting",
    value: str = "x",
    source_quality: str = "ats_native",
    source_event_at: datetime | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        id=eid,
        probe=probe,
        claim_type=claim_type,
        value=value,
        source_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs/1",
        source_quality=source_quality,
        source_event_at=source_event_at,
        available_at=NOW,
        fetched_at=NOW,
    )


def assert_every_reason_is_cited(reasons, evidence):
    known = {item.id for item in evidence}
    assert reasons, "expected at least one reason"
    for reason in reasons:
        assert reason.evidence_ids, f"uncited reason: {reason.text!r}"
        assert set(reason.evidence_ids) <= known


def test_open_recent_posting_produces_cited_reasons():
    evidence = [
        ev("e1", CLAIM_POSTING_STATE, value="open"),
        ev("e2", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=11)),
    ]
    reasons = reasons_from_inputs(
        PolicyInputs(posting_state="open", publish_recency="recent"), evidence, now=NOW
    )
    assert_every_reason_is_cited(reasons, evidence)
    # spec.md §1's worked example: "Role was first published 11 days ago."
    assert any("11 days ago" in reason.text for reason in reasons)
    assert reasons[0].evidence_ids == ["e1"]


@pytest.mark.parametrize(
    ("days", "fragment"),
    [(0, "0 days ago"), (1, "1 day ago"), (2, "2 days ago")],
)
def test_days_ago_phrasing(days, fragment):
    evidence = [ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=days))]
    reasons = reasons_from_inputs(PolicyInputs(), evidence, now=NOW)
    assert fragment in reasons[0].text


@pytest.mark.parametrize("posting_state", ["open", "closed", "reposted", "unknown"])
def test_every_state_has_a_sentence(posting_state):
    evidence = [ev("e1", CLAIM_POSTING_STATE, value=posting_state)]
    reasons = reasons_from_inputs(PolicyInputs(posting_state=posting_state), evidence, now=NOW)
    assert_every_reason_is_cited(reasons, evidence)


def test_no_reason_is_emitted_without_supporting_evidence():
    """A known input with no evidence behind it stays unsaid (spec.md §9)."""
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=True,
        freeze_or_pause=True,
        repost_pattern="repeated_unchanged",
        corroborating_hiring_signal=False,
        declared_expiry=NOW + timedelta(days=5),
    )
    assert reasons_from_inputs(inputs, [], now=NOW) == []


def test_declared_expiry_reason_marks_a_passed_expiry():
    evidence = [
        ev("e1", CLAIM_DECLARED_EXPIRY, source_quality="page_structured"),
    ]
    future = reasons_from_inputs(
        PolicyInputs(declared_expiry=NOW + timedelta(days=5)), evidence, now=NOW
    )
    assert "has now passed" not in future[0].text

    past = reasons_from_inputs(
        PolicyInputs(declared_expiry=NOW - timedelta(days=5)), evidence, now=NOW
    )
    assert "has now passed" in past[0].text
    assert_every_reason_is_cited(past, evidence)


def test_event_reasons_are_only_stated_when_true():
    evidence = [ev("e1", "layoff", probe="company_events", source_quality="news")]

    stated = reasons_from_inputs(
        PolicyInputs(material_negative_event=True, freeze_or_pause=True), evidence, now=NOW
    )
    assert len(stated) == 2
    assert_every_reason_is_cited(stated, evidence)

    # "checked, none found" is not a user-facing reason.
    assert (
        reasons_from_inputs(
            PolicyInputs(material_negative_event=False, freeze_or_pause=False), evidence, now=NOW
        )
        == []
    )


@pytest.mark.parametrize(
    ("pattern", "expected_count"),
    [("repeated_unchanged", 1), ("changed", 1), ("none", 0), (UNKNOWN, 0)],
)
def test_repost_reason(pattern, expected_count):
    evidence = [ev("e1", "repost_link", probe="repost_history", source_quality="archive")]
    reasons = reasons_from_inputs(PolicyInputs(repost_pattern=pattern), evidence, now=NOW)
    assert len(reasons) == expected_count


@pytest.mark.parametrize(
    ("signal", "fragment"),
    [(True, "Recent hiring activity"), (False, "No recent hiring activity")],
)
def test_team_signal_reason(signal, fragment):
    evidence = [ev("e1", CLAIM_TEAM_SIGNAL, probe="team_signal", source_quality="enrichment")]
    reasons = reasons_from_inputs(
        PolicyInputs(corroborating_hiring_signal=signal), evidence, now=NOW
    )
    assert fragment in reasons[0].text
    assert_every_reason_is_cited(reasons, evidence)


# ---------------------------------------------------------------------------
# P3c: the material-event reason's two wordings (Amendment 2026-09-10)
# ---------------------------------------------------------------------------

_MATERIAL_EVENT_FRAGMENT = "layoff or shutdown"


def test_material_event_reason_cites_both_event_and_refresh_when_unanswered():
    """The P3c wording cites BOTH halves of the observation — the dated
    event and the "since" claim — in ONE `ReasonItem` (module docstring:
    two wordings, one reason, and both evidence ids on it)."""
    event_at = NOW - timedelta(days=10)
    refreshed_before_event = NOW - timedelta(days=30)
    evidence = [
        ev("e1", "layoff", probe="company_events", source_quality="news"),
        ev("e2", CLAIM_REFRESHED_AT, source_event_at=refreshed_before_event),
    ]
    inputs = PolicyInputs(material_negative_event=True, last_material_event_at=event_at)
    reasons = reasons_from_inputs(inputs, evidence, now=NOW)
    assert_every_reason_is_cited(reasons, evidence)

    material_event_reasons = [r for r in reasons if _MATERIAL_EVENT_FRAGMENT in r.text]
    assert len(material_event_reasons) == 1
    reason = material_event_reasons[0]
    assert "has not changed since" in reason.text
    assert set(reason.evidence_ids) == {"e1", "e2"}


def test_material_event_reason_falls_back_to_generic_without_refresh_or_publish_evidence():
    """No refresh/publish evidence to cite at all: still true that the
    posting was "not refreshed since", but there is no id for the absence —
    the generic wording (citing only the event) is emitted instead of an
    uncited half-sentence (module docstring's third fallback case)."""
    evidence = [ev("e1", "layoff", probe="company_events", source_quality="news")]
    inputs = PolicyInputs(
        material_negative_event=True, last_material_event_at=NOW - timedelta(days=10)
    )
    reasons = reasons_from_inputs(inputs, evidence, now=NOW)
    assert_every_reason_is_cited(reasons, evidence)

    material_event_reasons = [r for r in reasons if _MATERIAL_EVENT_FRAGMENT in r.text]
    assert len(material_event_reasons) == 1
    reason = material_event_reasons[0]
    assert "has not changed since" not in reason.text
    assert reason.evidence_ids == ["e1"]


def test_material_event_reason_uses_generic_wording_when_refreshed_after_the_event():
    """The posting WAS refreshed after the event: the specific wording would
    be false, so the generic wording is used — exactly one reason either way."""
    event_at = NOW - timedelta(days=10)
    refreshed_after_event = NOW - timedelta(days=2)
    evidence = [
        ev("e1", "layoff", probe="company_events", source_quality="news"),
        ev("e2", CLAIM_REFRESHED_AT, source_event_at=refreshed_after_event),
    ]
    inputs = PolicyInputs(material_negative_event=True, last_material_event_at=event_at)
    reasons = reasons_from_inputs(inputs, evidence, now=NOW)
    assert_every_reason_is_cited(reasons, evidence)

    material_event_reasons = [r for r in reasons if _MATERIAL_EVENT_FRAGMENT in r.text]
    assert len(material_event_reasons) == 1
    reason = material_event_reasons[0]
    assert "has not changed since" not in reason.text
    # The refresh claim proved the posting DID move since the event, so it
    # has nothing to do with the generic sentence — only the event is cited.
    assert reason.evidence_ids == ["e1"]


def test_refresh_reason_cites_the_refresh_claim_and_sits_beside_publication():
    """A `refreshed_at` claim produces its own reason citing that claim's
    id, and it appears ALONGSIDE the "first published N days ago" reason
    (module docstring's anti-misleading guarantee) — in the module
    docstring's fixed order (state, publication, refresh, expiry, ...)."""
    evidence = [
        ev("e1", CLAIM_POSTING_STATE, value="open"),
        ev("e2", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=340)),
        ev("e3", CLAIM_REFRESHED_AT, source_event_at=NOW - timedelta(days=2)),
    ]
    inputs = PolicyInputs(posting_state="open", publish_recency="recent")
    reasons = reasons_from_inputs(inputs, evidence, now=NOW)
    assert_every_reason_is_cited(reasons, evidence)

    # Fixed order: state (e1), publication (e2), refresh (e3) — adjacently,
    # so "first published 340 days ago" is never read without the refresh
    # that explains why the posting still counts as recent.
    assert [r.evidence_ids for r in reasons] == [["e1"], ["e2"], ["e3"]]
    assert any("first published 340 days ago" in r.text for r in reasons)
    assert any("updated on" in r.text for r in reasons)


def test_board_absence_is_surfaced_even_when_it_conflicts():
    evidence = [
        ev("e1", CLAIM_POSTING_STATE, value="open"),
        ev("e2", CLAIM_BOARD_ABSENT, probe="board_snapshot"),
    ]
    reasons = reasons_from_inputs(PolicyInputs(posting_state="open"), evidence, now=NOW)
    assert any(reason.evidence_ids == ["e2"] for reason in reasons)


def test_reasons_are_deterministic_and_ordered():
    evidence = [
        ev("e1", CLAIM_POSTING_STATE, value="open"),
        ev("e2", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=3)),
        ev("e3", CLAIM_DECLARED_EXPIRY, source_quality="page_structured"),
    ]
    inputs = PolicyInputs(posting_state="open", declared_expiry=NOW + timedelta(days=9))
    first = reasons_from_inputs(inputs, evidence, now=NOW)
    second = reasons_from_inputs(inputs, list(reversed(evidence)), now=NOW)
    assert [r.text for r in first] == [r.text for r in second]
    assert [r.evidence_ids for r in first] == [["e1"], ["e2"], ["e3"]]


def test_empty_evidence_yields_no_reasons():
    assert reasons_from_inputs(PolicyInputs(posting_state="open"), [], now=NOW) == []


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        reasons_from_inputs(PolicyInputs(), [], now=datetime(2026, 9, 7))


def test_a_dangling_citation_fails_loudly():
    """A reason citing an id that does not exist is a bug, not a warning."""
    from rli.models.decision import ReasonItem

    with pytest.raises(ValueError, match="unknown evidence id"):
        _enforce_citations([ReasonItem(text="x", evidence_ids=["nope"])], [])


def test_reasons_drop_in_to_the_spec_output_shape():
    evidence = [
        ev("e1", CLAIM_POSTING_STATE, value="open"),
        ev("e2", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=11)),
    ]
    decision = Decision(
        posting_state="open",
        recommended_action="apply_now",
        recheck_after_days=None,
        evidence_quality="strong",
        reason=reasons_from_inputs(
            PolicyInputs(posting_state="open", publish_recency="recent"), evidence, now=NOW
        ),
        evidence=evidence,
    )
    payload = decision.model_dump()
    cited = {eid for reason in payload["reason"] for eid in reason["evidence_ids"]}
    assert cited <= {item["id"] for item in payload["evidence"]}


# ---------------------------------------------------------------------------
# Company-event reasons cite the qualifying claims, not "everything the probe
# produced" (spec.md §9)
# ---------------------------------------------------------------------------


def _event_ev(
    eid: str, claim_type: str, event_at: datetime, *, materiality: str = "material"
) -> EvidenceItem:
    """A `company_events` claim shaped exactly as that probe emits it."""
    return ev(
        eid,
        claim_type,
        probe="company_events",
        value=format_event_claim_value(materiality, f"headline for {claim_type}"),
        source_quality="news",
        source_event_at=event_at,
    )


def test_company_event_reasons_cite_only_the_qualifying_claims():
    """A layoff reason must not be supported by a funding headline.

    `company_events` emits a claim per dated event of ANY type, plus the
    `company_events_searched` collection-status claim. Citing the whole probe
    output would attach a funding round and a bookkeeping stamp to the
    sentence "a dated layoff or shutdown was recorded", which resolves to
    real ids and still says nothing that supports the claim — the exact shape
    of unsupported citation spec.md §9 forbids (and which
    `rli.eval.metrics`' citation-support figure counts).
    """
    event_at = NOW - timedelta(days=10)
    evidence = [
        ev(
            "e0",
            CLAIM_EVENTS_SEARCHED,
            probe="company_events",
            source_quality="enrichment",
            source_event_at=NOW - timedelta(days=1),
        ),
        _event_ev("e1", "funding", event_at, materiality="minor"),
        _event_ev("e2", "layoff", event_at),
        _event_ev("e3", "hiring_freeze", event_at),
    ]
    inputs = PolicyInputs(
        material_negative_event=True,
        freeze_or_pause=True,
        last_material_event_at=event_at,
    )

    reasons = reasons_from_inputs(inputs, evidence, now=NOW)
    assert_every_reason_is_cited(reasons, evidence)

    material = next(r for r in reasons if _MATERIAL_EVENT_FRAGMENT in r.text)
    assert set(material.evidence_ids) == {"e2"}

    freeze = next(r for r in reasons if "hiring freeze or pause" in r.text)
    assert freeze.evidence_ids == ["e3"]


def test_company_event_reason_falls_back_to_the_whole_probe_output():
    """Defensive fallback: an undatable claim cannot be selected precisely.

    A `company_events` claim with no `source_event_at` cannot be windowed, so
    `material_event_claims` drops it and the precise set comes back empty
    while the boolean is still `True` (here, because the caller asserted it).
    The reason must still be emitted and cited — silently dropping the
    explanation of a `wait` is worse than a coarser citation.
    """
    evidence = [ev("e1", "layoff", probe="company_events", source_quality="news")]
    inputs = PolicyInputs(material_negative_event=True)

    reasons = reasons_from_inputs(inputs, evidence, now=NOW)

    material = next(r for r in reasons if _MATERIAL_EVENT_FRAGMENT in r.text)
    assert material.evidence_ids == ["e1"]
