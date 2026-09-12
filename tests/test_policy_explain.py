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
from rli.policy.claim_families import CLAIM_FAMILIES, classify_reason
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
# The hiring-signal reason's two wordings (Amendment 2026-09-12)
# ---------------------------------------------------------------------------

_HIRING_FRAGMENT = "hiring activity was found for this team"
_SPECIFIC_TAIL = "while this role remained listed"


def _team_evidence() -> list[EvidenceItem]:
    """The `team_signal` probe's output, in the order that probe emits it."""
    return [
        ev("t1", "team_new_roles", probe="team_signal", source_quality="enrichment"),
        ev("t2", "team_closures", probe="team_signal", source_quality="enrichment"),
        ev("t3", CLAIM_TEAM_SIGNAL, probe="team_signal", source_quality="enrichment"),
    ]


def test_hiring_signal_specific_wording_cites_the_team_items_and_the_state_claim():
    """The observation behind the policy's P5b `apply_now` is a TWO-part
    statement, so spec.md §9 requires both halves cited in the one
    `ReasonItem` — every `team_signal` item, in evidence order, followed by
    the `posting_state` claim (exactly as `_material_event_reason` cites its
    "since" claim alongside the event)."""
    evidence = [ev("e1", CLAIM_POSTING_STATE, value="open"), *_team_evidence()]
    reasons = reasons_from_inputs(
        PolicyInputs(posting_state="open", corroborating_hiring_signal=True), evidence, now=NOW
    )
    assert_every_reason_is_cited(reasons, evidence)

    hiring = [r for r in reasons if _HIRING_FRAGMENT in r.text]
    assert len(hiring) == 1, "exactly ONE hiring-signal reason, whichever wording applies"
    assert _SPECIFIC_TAIL in hiring[0].text
    assert hiring[0].evidence_ids == ["t1", "t2", "t3", "e1"]


def test_hiring_signal_specific_wording_is_supported_by_the_claims_it_cites():
    """The wording is chosen against `rli.policy.claim_families`' crude
    classifier, not against taste: "listed" puts the sentence in the
    `posting_state` family, so without a `posting_state` claim among the
    cited ids spec.md §6's citation-support metric would count the reason
    UNSUPPORTED (and `rli.agent.explanation`'s guard would drop it). The
    real classifier output is asserted, not a guess."""
    evidence = [ev("e1", CLAIM_POSTING_STATE, value="open"), *_team_evidence()]
    reasons = reasons_from_inputs(
        PolicyInputs(posting_state="open", corroborating_hiring_signal=True), evidence, now=NOW
    )
    hiring = next(r for r in reasons if _SPECIFIC_TAIL in r.text)

    families = classify_reason(hiring.text)
    assert families == {"posting_state"}

    by_id = {item.id: item for item in evidence}
    cited_types = {by_id[eid].claim_type for eid in hiring.evidence_ids}
    supported = {family for family in families if cited_types & CLAIM_FAMILIES[family]}
    assert supported == families


def test_hiring_signal_falls_back_to_generic_without_a_posting_state_claim():
    """No `posting_state` claim, no id for the second half of the sentence —
    and an uncited half-sentence is worse than a coarser true one (the same
    judgment as `_material_event_reason`'s third fallback). The generic
    wording classifies as nothing at all, which the metric counts as
    `reasons_unclassified` rather than unsupported."""
    evidence = _team_evidence()
    reasons = reasons_from_inputs(
        PolicyInputs(posting_state="open", corroborating_hiring_signal=True), evidence, now=NOW
    )
    assert_every_reason_is_cited(reasons, evidence)

    hiring = [r for r in reasons if _HIRING_FRAGMENT in r.text]
    assert len(hiring) == 1
    assert _SPECIFIC_TAIL not in hiring[0].text
    assert hiring[0].evidence_ids == ["t1", "t2", "t3"]
    assert classify_reason(hiring[0].text) == set()


@pytest.mark.parametrize("posting_state", ["reposted", "closed", "unknown", UNKNOWN])
def test_hiring_signal_specific_wording_needs_an_OPEN_posting(posting_state):
    """ "...while this role remained listed" is only true of an `open`
    posting. A reposted, closed or unresolved one keeps the generic wording,
    which says nothing about the listing and cites only the team items."""
    state_value = "unknown" if posting_state is UNKNOWN else posting_state
    evidence = [ev("e1", CLAIM_POSTING_STATE, value=state_value), *_team_evidence()]
    reasons = reasons_from_inputs(
        PolicyInputs(posting_state=posting_state, corroborating_hiring_signal=True),
        evidence,
        now=NOW,
    )
    assert_every_reason_is_cited(reasons, evidence)

    hiring = [r for r in reasons if _HIRING_FRAGMENT in r.text]
    assert len(hiring) == 1
    assert _SPECIFIC_TAIL not in hiring[0].text
    assert hiring[0].evidence_ids == ["t1", "t2", "t3"]


def test_negative_hiring_signal_wording_is_unchanged():
    """The `False` case gets no second wording: "no hiring activity" says
    nothing about whether the role is listed, so there is no second half to
    cite — and P4, the branch that reads `is False`, is a `skip`, not the
    `apply_now` the specific wording exists to explain."""
    evidence = [ev("e1", CLAIM_POSTING_STATE, value="open"), *_team_evidence()]
    reasons = reasons_from_inputs(
        PolicyInputs(posting_state="open", corroborating_hiring_signal=False), evidence, now=NOW
    )
    assert_every_reason_is_cited(reasons, evidence)

    hiring = [r for r in reasons if _HIRING_FRAGMENT in r.text]
    assert len(hiring) == 1
    assert hiring[0].text == "No recent hiring activity was found for this team."
    assert hiring[0].evidence_ids == ["t1", "t2", "t3"]


def test_hiring_signal_reason_does_not_cite_the_publish_claim():
    """P5b fires REGARDLESS of publish recency, and `first_published` /
    `refreshed_at` are `publish`-family claims that support neither half of
    the sentence — citing one would be the unsupported-citation shape
    spec.md §9 forbids. The publish claim still earns its OWN reason."""
    evidence = [
        ev("e1", CLAIM_POSTING_STATE, value="open"),
        ev("e2", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=340)),
        ev("e3", CLAIM_REFRESHED_AT, source_event_at=NOW - timedelta(days=200)),
        *_team_evidence(),
    ]
    reasons = reasons_from_inputs(
        PolicyInputs(
            posting_state="open",
            publish_recency="not_recent",
            corroborating_hiring_signal=True,
        ),
        evidence,
        now=NOW,
    )
    assert_every_reason_is_cited(reasons, evidence)

    hiring = next(r for r in reasons if _SPECIFIC_TAIL in r.text)
    assert set(hiring.evidence_ids) == {"t1", "t2", "t3", "e1"}
    assert any(r.evidence_ids == ["e2"] for r in reasons)
    assert any(r.evidence_ids == ["e3"] for r in reasons)


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
