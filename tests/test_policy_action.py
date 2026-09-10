"""The frozen action policy: every branch, every precedence conflict.

PLAN.md M3: "Before freezing, document overlapping policy branches and their
precedence; test freeze/expiry against weak evidence and recent publication"
and "Tests for every policy branch and unknown-input case". The parametrized
tables below are the executable form of the precedence documentation in
`rli.policy.action`'s module docstring — if the code and the docstring ever
disagree, `test_precedence_table_is_exhaustive_and_ordered` fails.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rli.config import load_config
from rli.models.policy_inputs import UNKNOWN, PolicyInputs
from rli.policy.action import (
    PRECEDENCE,
    PolicyThresholds,
    decide,
    policy_version,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

THRESHOLDS = PolicyThresholds(
    recent_publish_days=14,
    long_lived_days=180,
    contradiction_days=3,
    recheck_default_days=14,
    recheck_cap_days=14,
)


def inputs(**overrides) -> PolicyInputs:
    return PolicyInputs(**overrides)


# ---------------------------------------------------------------------------
# One case per branch of the precedence table
# ---------------------------------------------------------------------------

# (branch id, PolicyInputs kwargs, evidence_quality, long_lived, expected action)
BRANCH_CASES = [
    (
        "P1_closed",
        {"posting_state": "closed"},
        "strong",
        UNKNOWN,
        "skip",
    ),
    (
        "P2_state_unresolved",
        {},
        "weak",
        UNKNOWN,
        "wait",
    ),
    (
        "P2_state_unresolved",
        {"posting_state": "unknown"},
        "weak",
        UNKNOWN,
        "wait",
    ),
    (
        "P3a_freeze_or_pause",
        {"posting_state": "open", "freeze_or_pause": True},
        "strong",
        UNKNOWN,
        "wait",
    ),
    (
        "P3b_declared_expiry_unresolved",
        {"posting_state": "open", "declared_expiry": NOW + timedelta(days=10)},
        "weak",
        UNKNOWN,
        "wait",
    ),
    (
        # spec.md §5 Amendment 2026-09-10's new row: a dated material event
        # and no publish/refresh date at all, which counts as "not refreshed
        # since" (see `rli.policy.action`'s Unknown-handling section).
        "P3c_material_event_unrefreshed",
        {
            "posting_state": "open",
            "material_negative_event": True,
            "last_material_event_at": NOW - timedelta(days=5),
        },
        "strong",
        UNKNOWN,
        "wait",
    ),
    (
        "P4_repeated_repost",
        {
            "posting_state": "open",
            "repost_pattern": "repeated_unchanged",
            "corroborating_hiring_signal": False,
        },
        "strong",
        True,
        "skip",
    ),
    (
        "P5_open_recent_strong",
        {
            "posting_state": "open",
            "publish_recency": "recent",
            "material_negative_event": False,
        },
        "strong",
        UNKNOWN,
        "apply_now",
    ),
    (
        "P6_active_mixed_or_weak",
        {"posting_state": "open", "publish_recency": "recent"},
        "mixed",
        UNKNOWN,
        "quick_apply",
    ),
    (
        "P6_active_mixed_or_weak",
        {"posting_state": "reposted"},
        "weak",
        UNKNOWN,
        "quick_apply",
    ),
    (
        "P7_default",
        {"posting_state": "open", "publish_recency": "not_recent"},
        "strong",
        UNKNOWN,
        "quick_apply",
    ),
]


@pytest.mark.parametrize(
    ("branch", "kwargs", "quality", "long_lived", "action"),
    BRANCH_CASES,
    ids=[f"{case[0]}-{index}" for index, case in enumerate(BRANCH_CASES)],
)
def test_every_branch(branch, kwargs, quality, long_lived, action):
    outcome = decide(inputs(**kwargs), quality, NOW, THRESHOLDS, long_lived=long_lived)
    assert outcome.branch == branch
    assert outcome.recommended_action == action


def test_precedence_table_is_exhaustive_and_ordered():
    """`PRECEDENCE` documents exactly the branches the code can return."""
    exercised = {
        decide(inputs(**kwargs), quality, NOW, THRESHOLDS, long_lived=long_lived).branch
        for _, kwargs, quality, long_lived, _ in BRANCH_CASES
    }
    assert exercised == {branch for branch, _ in PRECEDENCE}

    documented = dict(PRECEDENCE)
    for branch, kwargs, quality, long_lived, action in BRANCH_CASES:
        assert documented[branch] == action

    # First match wins, so the table order is load-bearing, not cosmetic.
    assert [branch for branch, _ in PRECEDENCE] == [
        "P1_closed",
        "P2_state_unresolved",
        "P3a_freeze_or_pause",
        "P3b_declared_expiry_unresolved",
        "P3c_material_event_unrefreshed",
        "P4_repeated_repost",
        "P5_open_recent_strong",
        "P6_active_mixed_or_weak",
        "P7_default",
    ]


# ---------------------------------------------------------------------------
# Precedence conflicts: cases where several §5 rows match at once
# ---------------------------------------------------------------------------

CONFLICT_CASES = [
    pytest.param(
        # closed beats absolutely everything, including a fresh publish date
        # and a strong evidence set.
        {
            "posting_state": "closed",
            "publish_recency": "recent",
            "freeze_or_pause": True,
            "declared_expiry": NOW + timedelta(days=2),
            "repost_pattern": "repeated_unchanged",
            "corroborating_hiring_signal": False,
            "material_negative_event": True,
        },
        "strong",
        True,
        "P1_closed",
        "skip",
        id="closed-beats-everything",
    ),
    pytest.param(
        # An unresolved state beats a freeze: we cannot even confirm the
        # posting, so "wait" is reached by the earlier rule.
        {"freeze_or_pause": True, "publish_recency": "recent"},
        "weak",
        UNKNOWN,
        "P2_state_unresolved",
        "wait",
        id="unknown-state-beats-freeze",
    ),
    pytest.param(
        # Freeze (wait, recoverable) beats the repost skip (terminal).
        {
            "posting_state": "open",
            "freeze_or_pause": True,
            "repost_pattern": "repeated_unchanged",
            "corroborating_hiring_signal": False,
        },
        "strong",
        True,
        "P3a_freeze_or_pause",
        "wait",
        id="freeze-beats-repost-skip",
    ),
    pytest.param(
        # PLAN.md M3 bullet 5, case 1: freeze + recent publication + strong
        # evidence is still a wait, never apply_now.
        {
            "posting_state": "open",
            "freeze_or_pause": True,
            "publish_recency": "recent",
            "material_negative_event": False,
        },
        "strong",
        UNKNOWN,
        "P3a_freeze_or_pause",
        "wait",
        id="freeze-beats-apply-now",
    ),
    pytest.param(
        # PLAN.md M3 bullet 5, case 2: declared expiry + WEAK evidence +
        # recent publication is a wait, not the quick_apply of P6.
        {
            "posting_state": "open",
            "declared_expiry": NOW + timedelta(days=5),
            "publish_recency": "recent",
        },
        "weak",
        UNKNOWN,
        "P3b_declared_expiry_unresolved",
        "wait",
        id="expiry-plus-weak-beats-quick-apply",
    ),
    pytest.param(
        # ...but a declared expiry alone does NOT block apply_now when the
        # posting is observably open on strong evidence (spec.md §3:
        # validThrough is not proof of a ghost job).
        {
            "posting_state": "open",
            "declared_expiry": NOW + timedelta(days=5),
            "publish_recency": "recent",
            "material_negative_event": False,
        },
        "strong",
        UNKNOWN,
        "P5_open_recent_strong",
        "apply_now",
        id="expiry-does-not-beat-strong-open",
    ),
    pytest.param(
        # An expiry on a REPOSTED posting still waits: "current status
        # resolved" requires posting_state == "open".
        {"posting_state": "reposted", "declared_expiry": NOW + timedelta(days=5)},
        "strong",
        UNKNOWN,
        "P3b_declared_expiry_unresolved",
        "wait",
        id="expiry-on-reposted-waits",
    ),
    pytest.param(
        # The repost skip beats apply_now — otherwise the repost row could
        # never fire, since a repost always looks freshly published.
        {
            "posting_state": "open",
            "publish_recency": "recent",
            "material_negative_event": False,
            "repost_pattern": "repeated_unchanged",
            "corroborating_hiring_signal": False,
        },
        "strong",
        True,
        "P4_repeated_repost",
        "skip",
        id="repost-skip-beats-apply-now",
    ),
    pytest.param(
        # A known material negative event blocks apply_now; it does not by
        # itself produce wait or skip (spec.md §5 uses it only as a conjunct).
        {
            "posting_state": "open",
            "publish_recency": "recent",
            "material_negative_event": True,
        },
        "strong",
        UNKNOWN,
        "P7_default",
        "quick_apply",
        id="material-negative-blocks-apply-now",
    ),
    pytest.param(
        # `reposted` + recent + strong is deliberately NOT apply_now: P5
        # requires the literal "open" state.
        {
            "posting_state": "reposted",
            "publish_recency": "recent",
            "material_negative_event": False,
        },
        "strong",
        UNKNOWN,
        "P7_default",
        "quick_apply",
        id="reposted-is-not-apply-now",
    ),
    pytest.param(
        # P3a (explicit freeze) beats P3c (material event): both are `wait`,
        # but the trace must record the DECISIVE reason, not merely "a wait
        # row matched" — an operator reading the trace needs to know a freeze
        # is in play even when an unrelated event also matched.
        {
            "posting_state": "open",
            "freeze_or_pause": True,
            "material_negative_event": True,
            "last_material_event_at": NOW - timedelta(days=5),
        },
        "strong",
        UNKNOWN,
        "P3a_freeze_or_pause",
        "wait",
        id="freeze-beats-material-event",
    ),
    pytest.param(
        # P3b (declared expiry, status unresolved) beats P3c for the same
        # reason: `quality="weak"` keeps the status unresolved, so P3b's row
        # is reached before P3c's is even considered.
        {
            "posting_state": "open",
            "declared_expiry": NOW + timedelta(days=5),
            "material_negative_event": True,
            "last_material_event_at": NOW - timedelta(days=5),
        },
        "weak",
        UNKNOWN,
        "P3b_declared_expiry_unresolved",
        "wait",
        id="expiry-beats-material-event",
    ),
    pytest.param(
        # P3c beats P4: the recoverable `wait` is reached before the terminal
        # `skip`, same argument as freeze-beats-repost-skip above.
        {
            "posting_state": "open",
            "material_negative_event": True,
            "last_material_event_at": NOW - timedelta(days=5),
            "repost_pattern": "repeated_unchanged",
            "corroborating_hiring_signal": False,
        },
        "strong",
        True,
        "P3c_material_event_unrefreshed",
        "wait",
        id="material-event-beats-repost-skip",
    ),
    pytest.param(
        # P3c beats P5: a fresh publish date and strong evidence do not
        # override an unanswered material event (PLAN.md M3 bullet 5's
        # argument, extended to the amendment's new row).
        {
            "posting_state": "open",
            "publish_recency": "recent",
            "material_negative_event": True,
            "last_material_event_at": NOW - timedelta(days=5),
        },
        "strong",
        UNKNOWN,
        "P3c_material_event_unrefreshed",
        "wait",
        id="material-event-beats-apply-now",
    ),
]


@pytest.mark.parametrize(("kwargs", "quality", "long_lived", "branch", "action"), CONFLICT_CASES)
def test_precedence_conflicts(kwargs, quality, long_lived, branch, action):
    outcome = decide(inputs(**kwargs), quality, NOW, THRESHOLDS, long_lived=long_lived)
    assert (outcome.branch, outcome.recommended_action) == (branch, action)


# ---------------------------------------------------------------------------
# Unknown handling, per input
# ---------------------------------------------------------------------------


def test_unknown_posting_state_waits_with_recheck():
    outcome = decide(inputs(), "strong", NOW, THRESHOLDS)
    assert outcome.recommended_action == "wait"
    assert outcome.recheck_after_days == THRESHOLDS.recheck_default_days
    # The §1 output shape has no UNKNOWN sentinel: it serializes as "unknown".
    assert outcome.posting_state == "unknown"


@pytest.mark.parametrize("value", [UNKNOWN, False])
def test_unknown_material_negative_event_still_allows_apply_now(value):
    """Documented judgment call: UNKNOWN counts as "not known negative"."""
    outcome = decide(
        inputs(posting_state="open", publish_recency="recent", material_negative_event=value),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.recommended_action == "apply_now"


def test_unknown_freeze_is_not_a_freeze():
    outcome = decide(
        inputs(posting_state="open", publish_recency="recent", freeze_or_pause=UNKNOWN),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P5_open_recent_strong"


def test_unknown_declared_expiry_is_not_an_expiry():
    outcome = decide(
        inputs(posting_state="open", declared_expiry=UNKNOWN),
        "weak",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P6_active_mixed_or_weak"


def test_known_absent_declared_expiry_is_not_an_expiry():
    """`None` means "checked, the publisher declared none" — not an expiry."""
    outcome = decide(
        inputs(posting_state="open", declared_expiry=None),
        "weak",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P6_active_mixed_or_weak"


@pytest.mark.parametrize(
    ("repost_pattern", "long_lived", "signal"),
    [
        (UNKNOWN, True, False),  # history says nothing about reposts
        ("repeated_unchanged", UNKNOWN, False),  # long_lived not established
        ("repeated_unchanged", False, False),  # known NOT long-lived
        ("repeated_unchanged", True, UNKNOWN),  # team_signal never ran
        ("repeated_unchanged", True, True),  # a corroborating signal exists
        ("changed", True, False),  # the repost changed content
        ("none", True, False),
    ],
)
def test_repost_skip_requires_all_three_conjuncts(repost_pattern, long_lived, signal):
    outcome = decide(
        inputs(
            posting_state="open",
            repost_pattern=repost_pattern,
            corroborating_hiring_signal=signal,
        ),
        "strong",
        NOW,
        THRESHOLDS,
        long_lived=long_lived,
    )
    assert outcome.recommended_action != "skip"


def test_unknown_publish_recency_blocks_apply_now():
    outcome = decide(
        inputs(posting_state="open", material_negative_event=False),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P7_default"


# ---------------------------------------------------------------------------
# P3c: material event vs. refresh/publish date (Amendment 2026-09-10)
# ---------------------------------------------------------------------------


def test_p3c_does_not_fire_when_refreshed_after_the_event():
    """A refresh strictly after the event answers it — P3c must not fire.

    `material_negative_event` is still `True`, so P5's own `is not True`
    conjunct (predating the amendment) still blocks `apply_now`; the branch
    that fires is P7, not P5 — asserting that, not just "not P3c", is what
    would catch a regression that let the refresh silently satisfy P5 too.
    """
    outcome = decide(
        inputs(
            posting_state="open",
            publish_recency="recent",
            material_negative_event=True,
            last_material_event_at=NOW - timedelta(days=10),
        ),
        "strong",
        NOW,
        THRESHOLDS,
        last_refreshed_at=NOW - timedelta(days=1),
    )
    assert outcome.branch == "P7_default"
    assert outcome.recommended_action == "quick_apply"


def test_p3c_boundary_equal_refresh_still_fires():
    """The condition is strictly `>` (module docstring): a refresh AT the
    same instant as the event has not yet answered it, so P3c still fires."""
    event_at = NOW - timedelta(days=5)
    outcome = decide(
        inputs(
            posting_state="open",
            material_negative_event=True,
            last_material_event_at=event_at,
        ),
        "strong",
        NOW,
        THRESHOLDS,
        last_refreshed_at=event_at,
    )
    assert outcome.branch == "P3c_material_event_unrefreshed"


def test_p3c_boundary_one_second_after_does_not_fire():
    event_at = NOW - timedelta(days=5)
    outcome = decide(
        inputs(
            posting_state="open",
            publish_recency="recent",
            material_negative_event=True,
            last_material_event_at=event_at,
        ),
        "strong",
        NOW,
        THRESHOLDS,
        last_refreshed_at=event_at + timedelta(seconds=1),
    )
    assert outcome.branch != "P3c_material_event_unrefreshed"


def test_p3c_same_calendar_day_refresh_counts_as_after():
    """Event dates are day-granular midnight UTC (module docstring): a
    refresh later the SAME calendar day is still strictly greater and counts
    as "after the event" — a same-day refresh following a layoff
    announcement is exactly the corroboration P3c looks for, and the
    alternative (comparing calendar dates instead of instants) would discard
    the only ordering information available."""
    event_at = datetime(2026, 9, 1, tzinfo=UTC)
    outcome = decide(
        inputs(
            posting_state="open",
            material_negative_event=True,
            last_material_event_at=event_at,
        ),
        "strong",
        NOW,
        THRESHOLDS,
        last_refreshed_at=event_at + timedelta(hours=6),
    )
    assert outcome.branch != "P3c_material_event_unrefreshed"


@pytest.mark.parametrize("value", [False, UNKNOWN])
def test_p3c_requires_material_negative_event_true(value):
    """`False` ("checked, none found") and `UNKNOWN` ("not checked") must
    not fire P3c even when a dated event value is present on the input —
    isolates the `material_negative_event is True` conjunct from the date
    conjunct below."""
    outcome = decide(
        inputs(
            posting_state="open",
            publish_recency="recent",
            material_negative_event=value,
            last_material_event_at=NOW - timedelta(days=5),
        ),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch != "P3c_material_event_unrefreshed"


@pytest.mark.parametrize("event_at", [UNKNOWN, None])
def test_p3c_requires_a_dated_event(event_at):
    """The branch needs a real date to compare a refresh against (module
    docstring); `UNKNOWN` ("not checked") and `None` (would mean "checked,
    none found", which by the `PolicyInputs.last_material_event_at`
    invariant cannot co-occur with `material_negative_event=True` in
    practice) must both fail to fire it."""
    outcome = decide(
        inputs(
            posting_state="open",
            publish_recency="recent",
            material_negative_event=True,
            last_material_event_at=event_at,
        ),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch != "P3c_material_event_unrefreshed"


def test_p3c_never_fires_on_a_closed_posting():
    """P1 is unconditional and wins before P3c is even considered."""
    outcome = decide(
        inputs(
            posting_state="closed",
            material_negative_event=True,
            last_material_event_at=NOW - timedelta(days=5),
        ),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P1_closed"


def test_p3c_never_fires_when_state_is_unresolved():
    """P2 wins before P3c: we cannot even confirm the posting is active."""
    outcome = decide(
        inputs(
            material_negative_event=True,
            last_material_event_at=NOW - timedelta(days=5),
        ),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P2_state_unresolved"


# ---------------------------------------------------------------------------
# recheck_after_days (spec.md §1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expiry", "expected", "why"),
    [
        (None, 14, "known-absent expiry falls back to the default"),
        (UNKNOWN, 14, "unchecked expiry falls back to the default"),
        (NOW + timedelta(days=3), 3, "expiry in 3 days -> 3 (spec.md §1 example)"),
        (NOW + timedelta(days=3, hours=12), 3, "floor: never recheck after the expiry"),
        (NOW + timedelta(days=13), 13, "just inside the cap"),
        (NOW + timedelta(days=14), 14, "exactly the cap"),
        (NOW + timedelta(days=90), 14, "min(cap, days_until) caps at 14"),
        (NOW + timedelta(hours=12), 0, "less than a day out -> recheck now"),
        (NOW, 0, "expiring exactly now -> recheck now"),
        (NOW - timedelta(days=30), 0, "already passed -> 0, never negative"),
    ],
)
def test_recheck_after_days_rule(expiry, expected, why):
    outcome = decide(
        inputs(posting_state="open", declared_expiry=expiry, freeze_or_pause=True),
        "weak",
        NOW,
        THRESHOLDS,
    )
    assert outcome.recommended_action == "wait"
    assert outcome.recheck_after_days == expected, why


@pytest.mark.parametrize(
    ("kwargs", "quality", "long_lived"),
    [
        ({"posting_state": "closed"}, "strong", UNKNOWN),
        (
            {
                "posting_state": "open",
                "publish_recency": "recent",
                "material_negative_event": False,
            },
            "strong",
            UNKNOWN,
        ),
        ({"posting_state": "open"}, "weak", UNKNOWN),
        (
            {
                "posting_state": "open",
                "repost_pattern": "repeated_unchanged",
                "corroborating_hiring_signal": False,
            },
            "strong",
            True,
        ),
    ],
)
def test_recheck_after_days_is_null_unless_waiting(kwargs, quality, long_lived):
    """spec.md §1 attaches `recheck_after_days` to `wait` alone."""
    outcome = decide(inputs(**kwargs), quality, NOW, THRESHOLDS, long_lived=long_lived)
    assert outcome.recommended_action != "wait"
    assert outcome.recheck_after_days is None


def test_recheck_respects_a_smaller_cap():
    thresholds = THRESHOLDS.model_copy(update={"recheck_cap_days": 7})
    outcome = decide(
        inputs(posting_state="open", declared_expiry=NOW + timedelta(days=30)),
        "weak",
        NOW,
        thresholds,
    )
    assert outcome.recheck_after_days == 7


def test_p3c_recheck_defaults_without_a_declared_expiry():
    outcome = decide(
        inputs(
            posting_state="open",
            material_negative_event=True,
            last_material_event_at=NOW - timedelta(days=5),
        ),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P3c_material_event_unrefreshed"
    assert outcome.recheck_after_days == THRESHOLDS.recheck_default_days


def test_p3c_recheck_respects_a_declared_expiry():
    """P3c is only reachable WITH a declared expiry when the status is
    RESOLVED (`posting_state="open"` + `quality="strong"`): P3b sits above
    P3c and fires first whenever the status is unresolved, so
    `posting_state="open"` + `quality="strong"` is not an arbitrary choice
    here — it is the only combination that can reach P3c with an expiry
    present at all (module docstring's `recheck_after_days` section)."""
    outcome = decide(
        inputs(
            posting_state="open",
            material_negative_event=True,
            last_material_event_at=NOW - timedelta(days=5),
            declared_expiry=NOW + timedelta(days=5),
        ),
        "strong",
        NOW,
        THRESHOLDS,
    )
    assert outcome.branch == "P3c_material_event_unrefreshed"
    assert outcome.recheck_after_days == 5


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(inputs(posting_state="open"), "weak", datetime(2026, 9, 7), THRESHOLDS)


def test_decide_is_deterministic():
    case = inputs(posting_state="open", publish_recency="recent", material_negative_event=False)
    first = decide(case, "strong", NOW, THRESHOLDS)
    second = decide(case, "strong", NOW, THRESHOLDS)
    assert first == second


# ---------------------------------------------------------------------------
# Thresholds and policy_version
# ---------------------------------------------------------------------------


def test_policy_thresholds_constructs_from_five_original_fields_alone():
    """The two amendment thresholds must default: `THRESHOLDS` above (and
    every caller predating the amendment) constructs with only the original
    five numeric fields, and that is load-bearing for every other test in
    this module."""
    thresholds = PolicyThresholds(
        recent_publish_days=14,
        long_lived_days=180,
        contradiction_days=3,
        recheck_default_days=14,
        recheck_cap_days=14,
    )
    assert thresholds.refresh_match_days == 3
    assert thresholds.negative_event_window_days == 180


def test_thresholds_inherit_from_thresholds_table():
    cfg = load_config()
    resolved = PolicyThresholds.from_config(cfg)
    assert resolved.recent_publish_days == cfg.thresholds.recent_publish_days
    assert resolved.long_lived_days == cfg.thresholds.long_lived_days
    assert resolved.recheck_default_days == cfg.thresholds.recheck_default_days
    assert resolved.recheck_cap_days == cfg.thresholds.recheck_cap_days
    assert resolved.contradiction_days == cfg.policy.contradiction_days


def test_policy_table_overrides_thresholds_table():
    cfg = load_config()
    overridden = cfg.model_copy(
        update={
            "policy": cfg.policy.model_copy(
                update={"recent_publish_days": 3, "recheck_cap_days": 2}
            )
        }
    )
    resolved = PolicyThresholds.from_config(overridden)
    assert resolved.recent_publish_days == 3
    assert resolved.recheck_cap_days == 2
    # Untouched keys still come from [thresholds].
    assert resolved.long_lived_days == cfg.thresholds.long_lived_days


def test_coerce_accepts_config_thresholds_or_none():
    cfg = load_config()
    assert PolicyThresholds.coerce(THRESHOLDS) is THRESHOLDS
    assert PolicyThresholds.coerce(cfg) == PolicyThresholds.from_config(cfg)
    assert PolicyThresholds.coerce(None) == PolicyThresholds.from_config(cfg)


def test_policy_version_is_stable_and_threshold_sensitive():
    assert policy_version(THRESHOLDS) == policy_version(THRESHOLDS)
    assert policy_version(THRESHOLDS).startswith("policy-v1:")

    moved = THRESHOLDS.model_copy(update={"recent_publish_days": 21})
    assert policy_version(moved) != policy_version(THRESHOLDS)


def test_policy_version_ignores_frozen_at():
    """Freezing an unchanged policy must not invent a new version."""
    frozen = THRESHOLDS.model_copy(update={"frozen_at": datetime(2026, 1, 1, tzinfo=UTC)})
    assert policy_version(frozen) == policy_version(THRESHOLDS)


def test_policy_version_changes_with_refresh_match_days():
    """`refresh_match_days` is consumed upstream (`rli.eval.case`), not
    inside `_branch` — but it still changes decisions, so it must still be
    part of `fingerprint()` (module docstring's `PolicyThresholds` note)."""
    moved = THRESHOLDS.model_copy(update={"refresh_match_days": 7})
    assert policy_version(moved) != policy_version(THRESHOLDS)


def test_policy_version_changes_with_negative_event_window_days():
    moved = THRESHOLDS.model_copy(update={"negative_event_window_days": 90})
    assert policy_version(moved) != policy_version(THRESHOLDS)


@pytest.mark.parametrize("quality", ["STRONG", "unknown", "", None])
def test_invalid_evidence_quality_is_rejected(quality):
    """An unrecognized quality would silently behave as "not strong"."""
    with pytest.raises(ValueError, match="evidence_quality must be one of"):
        decide(inputs(posting_state="open"), quality, NOW, THRESHOLDS)
