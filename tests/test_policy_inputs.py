"""Policy-input derivation and the controller's deterministic stop rule.

PLAN.md M3: "`policy_inputs`: derive 8 inputs; distinguish unknown from
false; expose unpopulated inputs and which could still change the action."
The `could_change_action` tests below are the ones that matter most: spec.md
§4's hard stop and the controller's probe-eligibility rule are both defined
in terms of that set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rli.history.features import PostingHistoryFeatures
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs
from rli.policy.action import PolicyThresholds
from rli.policy.inputs import (
    CLAIM_DECLARED_EXPIRY,
    CLAIM_FIRST_PUBLISHED,
    CLAIM_POSTING_STATE,
    CLAIM_TEAM_SIGNAL,
    could_change_action,
    derive_policy_inputs,
    unpopulated,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

THRESHOLDS = PolicyThresholds(
    recent_publish_days=14,
    long_lived_days=180,
    contradiction_days=3,
    recheck_default_days=14,
    recheck_cap_days=14,
)


def ev(
    eid: str,
    claim_type: str,
    *,
    probe: str = "resolve_posting",
    value: str = "x",
    source_quality: str = "ats_native",
    source_event_at: datetime | None = None,
    available_at: datetime | None = None,
    source_url: str = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1",
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


def state_ev(value: str = "open", **kwargs) -> EvidenceItem:
    return ev(kwargs.pop("eid", "e-state"), CLAIM_POSTING_STATE, value=value, **kwargs)


def features(**overrides) -> PostingHistoryFeatures:
    base = {
        "posting_id": "p1",
        "company_id": "acme.com",
        "as_of": NOW,
        "history_days": 200.0,
        "history_coverage": 0.9,
    }
    return PostingHistoryFeatures(**{**base, **overrides})


def derive(evidence, *, feats=None, signals=None, **kwargs) -> PolicyInputs:
    return derive_policy_inputs(evidence, feats, signals, NOW, cfg=THRESHOLDS, **kwargs)


# ---------------------------------------------------------------------------
# posting_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["open", "closed", "reposted", "unknown"])
def test_posting_state_comes_from_resolver_evidence(value):
    assert derive([state_ev(value)]).posting_state == value


def test_posting_state_is_unknown_sentinel_without_evidence():
    """No evidence is "not investigated", which is NOT the literal "unknown"."""
    inputs = derive([])
    assert inputs.posting_state is UNKNOWN
    assert "posting_state" in unpopulated(inputs)


def test_literal_unknown_state_is_populated_not_unresolved():
    inputs = derive([state_ev("unknown")])
    assert inputs.posting_state == "unknown"
    assert "posting_state" not in unpopulated(inputs)


def test_freshest_state_claim_wins():
    stale = state_ev("open", eid="e1", available_at=NOW - timedelta(days=2))
    fresh = state_ev("closed", eid="e2", available_at=NOW)
    assert derive([fresh, stale]).posting_state == "closed"


def test_unparseable_state_value_is_unknown_sentinel():
    assert derive([state_ev("evergreen")]).posting_state is UNKNOWN


def test_resolver_state_claim_beats_another_probes_claim():
    other = ev("e1", CLAIM_POSTING_STATE, probe="repost_history", value="closed")
    resolver = state_ev("open", eid="e2", available_at=NOW - timedelta(days=1))
    assert derive([other, resolver]).posting_state == "open"


# ---------------------------------------------------------------------------
# publish_recency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [(0, "recent"), (13, "recent"), (14, "recent"), (15, "not_recent"), (400, "not_recent")],
)
def test_publish_recency_threshold(age_days, expected):
    claim = ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=age_days))
    assert derive([claim]).publish_recency == expected


def test_publish_recency_unknown_without_publish_evidence():
    inputs = derive([state_ev("open")])
    assert inputs.publish_recency is UNKNOWN


def test_archive_only_publish_evidence_is_not_primary():
    """Documented: archive-only leaves the question OPEN, it does not answer it."""
    claim = ev(
        "e1",
        CLAIM_FIRST_PUBLISHED,
        probe="repost_history",
        source_quality="archive",
        source_event_at=NOW - timedelta(days=2),
    )
    assert derive([claim]).publish_recency is UNKNOWN


def test_ats_native_beats_page_structured():
    ats = ev(
        "e1",
        CLAIM_FIRST_PUBLISHED,
        source_quality="ats_native",
        source_event_at=NOW - timedelta(days=2),
    )
    page = ev(
        "e2",
        CLAIM_FIRST_PUBLISHED,
        source_quality="page_structured",
        source_event_at=NOW - timedelta(days=400),
        source_url="https://acme.com/careers/1",
    )
    assert derive([page, ats]).publish_recency == "recent"


def test_earliest_date_wins_within_a_source_tier():
    early = ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=100))
    late = ev(
        "e2",
        CLAIM_FIRST_PUBLISHED,
        source_event_at=NOW - timedelta(days=1),
        source_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs/2",
    )
    assert derive([late, early]).publish_recency == "not_recent"


def test_publish_claim_without_a_parsed_date_is_ignored():
    claim = ev("e1", CLAIM_FIRST_PUBLISHED, value="sometime in spring", source_event_at=None)
    assert derive([claim]).publish_recency is UNKNOWN


# ---------------------------------------------------------------------------
# declared_expiry: unknown vs known-absent
# ---------------------------------------------------------------------------


def test_declared_expiry_from_claim():
    expiry = NOW + timedelta(days=20)
    claim = ev(
        "e1",
        CLAIM_DECLARED_EXPIRY,
        source_quality="page_structured",
        source_event_at=expiry,
    )
    assert derive([state_ev("open"), claim]).declared_expiry == expiry


def test_declared_expiry_known_absent_when_resolver_was_decisive():
    inputs = derive([state_ev("open")])
    assert inputs.declared_expiry is None
    assert "declared_expiry" not in unpopulated(inputs)


def test_declared_expiry_unknown_when_resolver_failed():
    inputs = derive([state_ev("open")], resolver_ok=False)
    assert inputs.declared_expiry is UNKNOWN


def test_declared_expiry_unknown_when_state_was_indeterminate():
    """A resolver that could not decide the state may also have failed JSON-LD."""
    assert derive([state_ev("unknown")]).declared_expiry is UNKNOWN


def test_declared_expiry_unknown_without_any_resolver_evidence():
    claim = ev("e1", CLAIM_POSTING_STATE, probe="repost_history", value="open")
    assert derive([claim]).declared_expiry is UNKNOWN


# ---------------------------------------------------------------------------
# events, history, team signal
# ---------------------------------------------------------------------------


def test_event_signals_are_passed_through():
    inputs = derive([], signals=(True, False))
    assert inputs.material_negative_event is True
    assert inputs.freeze_or_pause is False


def test_missing_event_signals_are_unknown_not_false():
    inputs = derive([], signals=None)
    assert inputs.material_negative_event is UNKNOWN
    assert inputs.freeze_or_pause is UNKNOWN
    assert {"material_negative_event", "freeze_or_pause"} <= unpopulated(inputs)


def test_unknown_event_signals_survive_the_round_trip():
    inputs = derive([], signals=(UNKNOWN, True))
    assert inputs.material_negative_event is UNKNOWN
    assert inputs.freeze_or_pause is True


@pytest.mark.parametrize("pattern", ["repeated_unchanged", "changed", "none", UNKNOWN])
def test_repost_pattern_comes_from_history_features(pattern):
    inputs = derive([], feats=features(repost_pattern=pattern))
    assert inputs.repost_pattern == pattern


def test_repost_pattern_unknown_without_features():
    assert derive([], feats=None).repost_pattern is UNKNOWN


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_corroborating_hiring_signal_from_team_signal_evidence(value, expected):
    claim = ev(
        "e1",
        CLAIM_TEAM_SIGNAL,
        probe="team_signal",
        value=value,
        source_quality="enrichment",
        source_url="https://enrichment.example/acme",
    )
    assert derive([claim]).corroborating_hiring_signal is expected


def test_corroborating_hiring_signal_unknown_by_default():
    assert derive([state_ev("open")]).corroborating_hiring_signal is UNKNOWN


def test_corroborating_hiring_signal_ignores_other_probes():
    """spec.md §4: team_signal is the ONLY source for this input."""
    claim = ev(
        "e1",
        CLAIM_TEAM_SIGNAL,
        probe="board_snapshot",
        value="true",
        source_quality="ats_native",
    )
    assert derive([claim]).corroborating_hiring_signal is UNKNOWN


def test_corroborating_hiring_signal_keyword_injection():
    assert derive([], team_signal=False).corroborating_hiring_signal is False


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        derive_policy_inputs([], None, None, datetime(2026, 9, 7), cfg=THRESHOLDS)


def test_unpopulated_is_empty_for_a_fully_populated_case():
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=False,
        freeze_or_pause=False,
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
    )
    assert unpopulated(inputs) == set()


# ---------------------------------------------------------------------------
# could_change_action — spec.md §4's deterministic stop rule
# ---------------------------------------------------------------------------


def ccan(inputs: PolicyInputs, action: str, **kwargs) -> set[str]:
    return could_change_action(inputs, action, now=NOW, cfg=THRESHOLDS, **kwargs)


def test_closed_posting_has_no_open_questions():
    """Nothing can rescue a closed posting: P1 fires first, unconditionally."""
    inputs = PolicyInputs(posting_state="closed")
    assert unpopulated(inputs)  # six inputs are still unpopulated...
    assert ccan(inputs, "skip") == set()  # ...and not one of them matters


def test_fully_populated_inputs_have_no_open_questions():
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=False,
        freeze_or_pause=False,
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
    )
    assert ccan(inputs, "apply_now") == set()


def test_result_is_always_a_subset_of_unpopulated():
    inputs = PolicyInputs(posting_state="open")
    assert ccan(inputs, "quick_apply", quality="weak") <= unpopulated(inputs)


def test_unknown_state_keeps_the_state_question_open():
    assert "posting_state" in ccan(PolicyInputs(), "wait")


def test_publish_recency_matters_only_when_apply_now_is_reachable():
    open_strong = PolicyInputs(posting_state="open", material_negative_event=False)
    assert "publish_recency" in ccan(open_strong, "quick_apply", quality="strong")

    # With the evidence pinned weak, no publish date can reach apply_now...
    assert "publish_recency" not in ccan(open_strong, "quick_apply", quality="weak")
    # ...but leaving quality free (a probe may also improve it) keeps it open.
    assert "publish_recency" in ccan(open_strong, "quick_apply")


def test_repost_and_team_signal_are_relevant_only_together():
    """The joint test: neither input alone changes the action, both do.

    This is the case a naive one-variable-at-a-time stop rule gets wrong —
    it would stop the loop and make spec.md §5's repost `skip` row
    unreachable.
    """
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=False,
        freeze_or_pause=False,
        declared_expiry=None,
    )
    relevant = ccan(inputs, "apply_now", quality="strong", long_lived=True)
    assert {"repost_pattern", "corroborating_hiring_signal"} <= relevant

    # Varying either one alone leaves the action untouched, which is exactly
    # why the joint test has to exist.
    for field, value in [
        ("repost_pattern", "repeated_unchanged"),
        ("corroborating_hiring_signal", False),
    ]:
        from rli.policy.action import decide

        alone = inputs.model_copy(update={field: value})
        assert decide(alone, "strong", NOW, THRESHOLDS, long_lived=True).recommended_action == (
            "apply_now"
        )


def test_team_signal_is_pointless_when_the_repost_branch_is_unreachable():
    """spec.md §4: team_signal is eligible only when that branch is reachable."""
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=False,
        freeze_or_pause=False,
        declared_expiry=None,
        repost_pattern="none",
    )
    assert ccan(inputs, "apply_now", quality="strong", long_lived=True) == set()

    # Same case, but the posting is not long-lived: still unreachable.
    inputs = inputs.model_copy(update={"repost_pattern": UNKNOWN})
    assert ccan(inputs, "apply_now", quality="strong", long_lived=False) == set()


def test_freeze_question_stays_open_for_a_strong_open_posting():
    """A freeze is decisive on its own, so company_events is always worth running."""
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=False,
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
    )
    assert ccan(inputs, "apply_now", quality="strong") == {"freeze_or_pause"}


def test_declared_expiry_question_closes_once_the_case_is_strong_and_open():
    """spec.md §3: validThrough is not proof, so it cannot move a strong open case."""
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=False,
        freeze_or_pause=False,
        repost_pattern="none",
        corroborating_hiring_signal=True,
    )
    assert ccan(inputs, "apply_now", quality="strong") == set()
    # ...but on weak evidence the same question does change the action.
    assert ccan(inputs, "quick_apply", quality="weak") == {"declared_expiry"}


def test_could_change_action_is_deterministic():
    inputs = PolicyInputs(posting_state="open")
    assert ccan(inputs, "quick_apply", quality="mixed") == ccan(
        inputs, "quick_apply", quality="mixed"
    )


def test_enumeration_domains_cover_every_policy_input():
    """A new `PolicyInputs` field must come with an enumeration domain.

    Without this guard, adding a field to the model would make
    `could_change_action` raise `KeyError` the first time that field is
    unpopulated — i.e. in production, on the very first controller step.
    """
    from rli.policy.inputs import _INPUT_DOMAINS

    assert set(_INPUT_DOMAINS) == set(PolicyInputs.model_fields)


def test_every_enumerated_domain_contains_the_unknown_sentinel():
    """"Stays unresolved" must be one of the outcomes the enumeration considers."""
    from rli.policy.inputs import _INPUT_DOMAINS

    for name, domain in _INPUT_DOMAINS.items():
        assert UNKNOWN in domain, name
