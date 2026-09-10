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

from rli.events.policy_signals import (
    CLAIM_EVENTS_SEARCHED,
    COLLECTION_STATUS_SOURCE_URL,
    COMPANY_EVENTS_PROBE,
    format_event_claim_value,
)
from rli.history.features import PostingHistoryFeatures
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs
from rli.policy.action import PolicyThresholds
from rli.policy.inputs import (
    CLAIM_DECLARED_EXPIRY,
    CLAIM_FIRST_PUBLISHED,
    CLAIM_POSTING_STATE,
    CLAIM_REFRESHED_AT,
    CLAIM_TEAM_SIGNAL,
    could_change_action,
    derive_policy_inputs,
    freeze_event_claims,
    last_publish_or_refresh,
    material_event_claims,
    newest_refresh_claim,
    unpopulated,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
# The third element of the `EventSignals` triple: the date of the most recent
# material negative event inside the window (midnight UTC, day-granular).
EVENT_AT = datetime(2026, 8, 20, tzinfo=UTC)

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


def refresh_ev(source_event_at: datetime, **kwargs) -> EvidenceItem:
    """A `refreshed_at` claim, as `rli.eval.case` synthesizes it: attributed
    to its own non-network probe rather than to `resolve_posting`."""
    kwargs.setdefault("eid", "e-refresh")
    kwargs.setdefault("probe", "refresh_match")
    return ev(
        kwargs.pop("eid"),
        CLAIM_REFRESHED_AT,
        source_event_at=source_event_at,
        **kwargs,
    )


def features(**overrides) -> PostingHistoryFeatures:
    base = {
        "posting_id": "p1",
        "company_id": "acme.com",
        "as_of": NOW,
        "history_days": 200.0,
        "history_coverage": 0.9,
    }
    return PostingHistoryFeatures(**{**base, **overrides})


def searched_ev(searched_at: datetime = NOW, eid: str = "e-searched") -> EvidenceItem:
    """`company_events`' collection-status claim.

    Its presence is what makes a `False` event signal an evidence-backed
    "checked, none found" instead of an inference from silence; its absence
    is what keeps the three inputs UNKNOWN.
    """
    return ev(
        eid,
        CLAIM_EVENTS_SEARCHED,
        probe=COMPANY_EVENTS_PROBE,
        value=searched_at.isoformat(),
        source_quality="enrichment",
        source_event_at=searched_at,
        available_at=searched_at,
        source_url=COLLECTION_STATUS_SOURCE_URL,
    )


def event_ev(
    event_type: str,
    event_at: datetime,
    *,
    eid: str = "e-event",
    materiality: str = "material",
    headline: str = "something happened",
    value: str | None = None,
) -> EvidenceItem:
    """One dated `company_events` claim, shaped exactly as the probe emits it."""
    return ev(
        eid,
        event_type,
        probe=COMPANY_EVENTS_PROBE,
        value=value if value is not None else format_event_claim_value(materiality, headline),
        source_quality="news",
        source_event_at=event_at,
        source_url="https://news.example/acme-layoff",
    )


def derive(evidence, *, feats=None, **kwargs) -> PolicyInputs:
    return derive_policy_inputs(evidence, feats, NOW, cfg=THRESHOLDS, **kwargs)


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


def test_corroborated_refresh_makes_a_stale_publish_date_recent():
    """spec.md §5 Amendment 2026-09-10: `recent` reads the LATEST of first
    publish or a corroborated refresh. A `first_published` alone, this stale,
    would be `not_recent` (see `test_publish_recency_threshold`) — the
    refresh is what flips the verdict."""
    stale_publish = ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=340))
    fresh_refresh = refresh_ev(NOW - timedelta(days=2))
    assert derive([stale_publish, fresh_refresh]).publish_recency == "recent"
    # The publish claim alone (no refresh) proves the baseline this corrects.
    assert derive([stale_publish]).publish_recency == "not_recent"


def test_publish_recency_from_first_published_alone_is_unchanged():
    """No refresh claim at all: behaviour predating the amendment holds."""
    claim = ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=5))
    assert derive([claim]).publish_recency == "recent"


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [(14, "recent"), (15, "not_recent")],
)
def test_publish_recency_threshold_via_refresh(age_days, expected):
    """The same boundary as `test_publish_recency_threshold`, but driven by
    a `refreshed_at` claim instead of `first_published` — `_publish_recency`
    must apply `recent_publish_days` identically to whichever date wins."""
    claim = refresh_ev(NOW - timedelta(days=age_days))
    assert derive([claim]).publish_recency == expected


def test_bare_updated_at_claim_does_not_make_it_recent():
    """The whole point of the corroboration rule: an ATS `updated_at` that
    was NOT matched to an observed content-hash change never becomes a
    `refreshed_at` claim, so it must not move `publish_recency` at all —
    otherwise every ATS's routine re-crawl timestamp would count as a
    refresh."""
    stale_publish = ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=340))
    bare_updated_at = ev("e2", "updated_at", source_event_at=NOW - timedelta(days=1))
    assert derive([stale_publish, bare_updated_at]).publish_recency == "not_recent"


def test_last_publish_or_refresh_takes_the_later_of_the_two():
    publish = ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=100))
    refresh = refresh_ev(NOW - timedelta(days=2))
    assert last_publish_or_refresh([publish, refresh]) == refresh.source_event_at

    # Reversed dates: publish is the later one this time.
    later_publish = ev(
        "e3",
        CLAIM_FIRST_PUBLISHED,
        source_event_at=NOW - timedelta(days=1),
        source_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs/2",
    )
    earlier_refresh = refresh_ev(NOW - timedelta(days=50), eid="e4")
    assert (
        last_publish_or_refresh([later_publish, earlier_refresh]) == later_publish.source_event_at
    )


def test_last_publish_or_refresh_works_with_only_one_side():
    publish = ev("e1", CLAIM_FIRST_PUBLISHED, source_event_at=NOW - timedelta(days=10))
    assert last_publish_or_refresh([publish]) == publish.source_event_at

    refresh = refresh_ev(NOW - timedelta(days=3))
    assert last_publish_or_refresh([refresh]) == refresh.source_event_at


def test_last_publish_or_refresh_is_none_without_either():
    assert last_publish_or_refresh([]) is None
    assert last_publish_or_refresh([state_ev("open")]) is None


def test_newest_refresh_claim_picks_the_freshest_and_is_none_otherwise():
    assert newest_refresh_claim([]) is None
    older = refresh_ev(NOW - timedelta(days=10), eid="e1", available_at=NOW - timedelta(days=1))
    newer = refresh_ev(NOW - timedelta(days=2), eid="e2", available_at=NOW)
    assert newest_refresh_claim([older, newer]) is newer


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


def test_event_signals_come_from_company_events_claims():
    """The three event inputs are read out of the probe's CLAIMS (spec.md §5/§9)."""
    inputs = derive([searched_ev(), event_ev("layoff", EVENT_AT)])
    assert inputs.material_negative_event is True
    assert inputs.freeze_or_pause is False
    assert inputs.last_material_event_at == EVENT_AT


def test_no_company_events_evidence_is_unknown_not_false():
    """No `company_events` evidence at all (the probe has not run) leaves all
    THREE members of the triple UNKNOWN, not just the first two."""
    inputs = derive([])
    assert inputs.material_negative_event is UNKNOWN
    assert inputs.freeze_or_pause is UNKNOWN
    assert inputs.last_material_event_at is UNKNOWN
    assert {
        "material_negative_event",
        "freeze_or_pause",
        "last_material_event_at",
    } <= unpopulated(inputs)


def test_searched_claim_alone_is_a_known_negative():
    """ "We looked and found nothing" is `False`/`None`, and it is evidence-backed."""
    inputs = derive([searched_ev()])
    assert inputs.material_negative_event is False
    assert inputs.freeze_or_pause is False
    assert inputs.last_material_event_at is None
    assert not {
        "material_negative_event",
        "freeze_or_pause",
        "last_material_event_at",
    } & unpopulated(inputs)


def test_event_claims_without_the_searched_claim_stay_unknown():
    """A dated event with no collection-status claim cannot be reported as a
    known negative for the OTHER signals: the search claim is what says the
    company was checked at all. (In replay the point-in-time gate can drop
    the searched claim while events remain visible — that must read as
    "not yet investigated", per `signals_from_facts`.)"""
    inputs = derive([event_ev("layoff", EVENT_AT)])
    assert inputs.material_negative_event is UNKNOWN
    assert inputs.freeze_or_pause is UNKNOWN
    assert inputs.last_material_event_at is UNKNOWN


def test_freeze_claim_sets_only_the_freeze_signal():
    inputs = derive([searched_ev(), event_ev("hiring_freeze", EVENT_AT, materiality="material")])
    assert inputs.freeze_or_pause is True
    assert inputs.material_negative_event is False
    assert inputs.last_material_event_at is None


def test_minor_layoff_does_not_set_material_negative_event():
    inputs = derive([searched_ev(), event_ev("layoff", EVENT_AT, materiality="minor")])
    assert inputs.material_negative_event is False
    assert inputs.last_material_event_at is None


def test_unparseable_materiality_on_a_layoff_fails_safe_to_material():
    """A `value` this probe version did not write must not silently downgrade a
    layoff to `minor` — that would drop a `wait` on real bad news."""
    inputs = derive([searched_ev(), event_ev("layoff", EVENT_AT, value="Acme lays off 200")])
    assert inputs.material_negative_event is True
    assert inputs.last_material_event_at == EVENT_AT


def test_event_outside_the_window_is_ignored():
    stale = NOW - timedelta(days=THRESHOLDS.negative_event_window_days + 5)
    inputs = derive([searched_ev(), event_ev("layoff", stale)])
    assert inputs.material_negative_event is False
    assert inputs.last_material_event_at is None


def test_last_material_event_at_is_the_most_recent_qualifying_event():
    # Midnight UTC: the signal is day-granular (`company_events` stores a
    # calendar date), so a claim's time-of-day never survives into the input.
    older = EVENT_AT - timedelta(days=40)
    newer = EVENT_AT - timedelta(days=5)
    inputs = derive(
        [
            searched_ev(),
            event_ev("layoff", older, eid="e-old"),
            event_ev("shutdown", newer, eid="e-new"),
        ]
    )
    assert inputs.last_material_event_at == newer


def test_event_claims_from_another_probe_are_ignored():
    """`claim_type` alone must not feed the rule — the probe has to be the one
    spec.md §5 names as the source."""
    impostor = ev(
        "e-x",
        "layoff",
        probe="board_snapshot",
        value=format_event_claim_value("material", "not from company_events"),
        source_event_at=EVENT_AT,
    )
    assert derive([searched_ev(), impostor]).material_negative_event is False


def test_material_event_claims_cites_only_the_qualifying_claims():
    """The explanation helper selects exactly the claims that set the boolean."""
    evidence = [
        searched_ev(),
        event_ev("funding", EVENT_AT, eid="e-fund", materiality="minor"),
        event_ev("layoff", EVENT_AT, eid="e-layoff"),
        event_ev("hiring_freeze", EVENT_AT, eid="e-freeze"),
    ]
    assert [c.id for c in material_event_claims(evidence, NOW, cfg=THRESHOLDS)] == ["e-layoff"]
    assert [c.id for c in freeze_event_claims(evidence, NOW, cfg=THRESHOLDS)] == ["e-freeze"]


def test_event_citation_helpers_are_empty_when_the_signal_is_false():
    evidence = [searched_ev(), event_ev("funding", EVENT_AT, materiality="minor")]
    assert material_event_claims(evidence, NOW, cfg=THRESHOLDS) == []
    assert freeze_event_claims(evidence, NOW, cfg=THRESHOLDS) == []


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
        derive_policy_inputs([], None, datetime(2026, 9, 7), cfg=THRESHOLDS)


def test_unpopulated_is_empty_for_a_fully_populated_case():
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        material_negative_event=False,
        freeze_or_pause=False,
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
        last_material_event_at=None,
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
        last_material_event_at=None,
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


def test_last_material_event_at_matters_when_it_can_still_trigger_p3c():
    """A posting reachable to `apply_now` is not safe from the amendment's
    new row: an unresolved event date is exactly the case the module
    docstring's `_materialize_event_date` samples on both sides of the
    (fixed, UNKNOWN) refresh anchor, so it must show up as relevant."""
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        freeze_or_pause=False,
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
    )
    assert {"material_negative_event", "last_material_event_at"} <= inputs.unpopulated()
    relevant = ccan(inputs, "apply_now", quality="strong")
    assert "last_material_event_at" in relevant
    assert "material_negative_event" in relevant


def test_last_refreshed_at_is_never_a_member_of_the_result():
    """`last_refreshed_at` is a `decide`/`could_change_action` KEYWORD, not a
    `PolicyInputs` field (module docstring's "two keyword inputs" section) —
    it structurally cannot appear in a set the controller maps to probes."""
    assert "last_refreshed_at" not in PolicyInputs.model_fields
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        freeze_or_pause=False,
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
    )
    relevant = ccan(inputs, "apply_now", quality="strong", last_refreshed_at=NOW)
    assert "last_refreshed_at" not in relevant


def test_a_known_refresh_after_a_known_event_can_close_the_material_event_question():
    """The direction that CAN make `material_negative_event` stop mattering:
    with `last_material_event_at` already resolved to a real date (so it is
    no longer part of the enumeration) and `apply_now` unreachable for an
    unrelated reason (`quality="weak"`, so P5 cannot fire and P3c is the
    ONLY branch through which `material_negative_event` could still act),
    a `last_refreshed_at` known to be after that date blocks P3c for every
    value of `material_negative_event` — closing the question entirely.

    Checked against the actual implementation before writing this
    assertion: with `last_refreshed_at` left UNKNOWN (the default) the
    question stays open, because `_materialize_event_date` samples an event
    date on EITHER side of whatever anchor it is given, so a concrete-but-
    unrelated `last_refreshed_at` does NOT shrink the set (see
    `test_last_material_event_at_matters_when_it_can_still_trigger_p3c`) —
    only a `last_refreshed_at` that is provably after the ALREADY-RESOLVED
    event date can. That is a real, narrow direction, not the general one;
    the assertion below is the one the real behaviour supports.
    """
    event_at = NOW - timedelta(days=30)
    inputs = PolicyInputs(
        posting_state="open",
        publish_recency="recent",
        freeze_or_pause=False,
        declared_expiry=None,
        repost_pattern="none",
        corroborating_hiring_signal=True,
        last_material_event_at=event_at,
    )
    assert inputs.unpopulated() == {"material_negative_event"}

    open_question = ccan(inputs, "quick_apply", quality="weak")
    assert "material_negative_event" in open_question

    refreshed_after = event_at + timedelta(days=5)
    closed_question = ccan(inputs, "quick_apply", quality="weak", last_refreshed_at=refreshed_after)
    assert closed_question <= open_question
    assert "material_negative_event" not in closed_question


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
    """ "Stays unresolved" must be one of the outcomes the enumeration considers."""
    from rli.policy.inputs import _INPUT_DOMAINS

    for name, domain in _INPUT_DOMAINS.items():
        assert UNKNOWN in domain, name
