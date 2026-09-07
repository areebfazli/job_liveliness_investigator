"""Derive the spec.md §5 policy inputs from evidence, history and events.

This module answers three questions for the controller (spec.md §4):

1. `derive_policy_inputs` — what do we currently know? (a `PolicyInputs`)
2. `unpopulated` — which policy inputs are still unresolved questions?
3. `could_change_action` — of those, which could *actually* change the
   recommended action? spec.md §4's hard stop "no unresolved question could
   change the action" is only deterministic and testable if that set is
   computed rather than guessed, so it is computed here by brute-force
   enumeration through the real policy function.

--------------------------------------------------------------------------
The evidence-claim contract
--------------------------------------------------------------------------

Policy inputs are derived from `EvidenceItem`s, never from a probe's raw
`ProbeResult.data`, because spec.md §9 requires every user-facing statement
to map to evidence. `rli.probes` therefore owns the *fetching*, and whatever
drives a probe (agent loop / System A runner) is responsible for turning the
probe's structured result into `EvidenceItem`s with the `claim_type` values
below. Two of them are not emitted by `rli.probes` today and are stated here
as the contract that layer must satisfy:

* `posting_state` — value is one of `open | closed | reposted | unknown`,
  from `resolve_posting`'s `data["posting_state"]`. Without it the observed
  state is not evidence-backed and the policy must not assert it.
* `board_absent` — the posting's job id was NOT in a `board_snapshot`
  capture. Needed for the "resolver says open but a fresher board snapshot
  lacks the job" contradiction (see `rli.policy.quality`).

Already emitted by `rli.probes.resolve_posting`: `first_published`,
`updated_at`, `declared_expiry`, `board_listing`.

--------------------------------------------------------------------------
Judgment calls (documented because they are not forced by spec.md)
--------------------------------------------------------------------------

* **`publish_recency` needs a PRIMARY publish date.** spec.md §5's
  `apply_now` branch says "recent *primary* publish evidence", and spec.md
  §3 ranks `ats_native` above `page_structured`; `archive`, `news` and
  `enrichment` publish claims are not primary. So an archive-only
  `first_published` leaves `publish_recency` UNKNOWN — "we have not
  established a trustworthy publish date" — rather than answering
  `not_recent`. Under the current §5 table both block `apply_now`
  identically, but only UNKNOWN keeps the question open for the controller
  (and `rli.policy.quality` separately marks archive-only evidence weak).
* **Ties are broken toward the EARLIEST date.** Within the best available
  source tier, the earliest `source_event_at` wins, because `first_published`
  means the first publication; taking the latest would understate the
  posting's age and flatter `recent`. Disagreement between sources is not
  hidden by this — it is what `rli.policy.quality` reports as a material
  contradiction.
* **`declared_expiry = None` (known-absent) is asserted narrowly.** Only
  when `resolve_posting` both ran (`resolver_ok=True`) and returned a
  decisive state (`open`/`closed`/`reposted`) and emitted no
  `declared_expiry` claim. A resolver that failed, or that could not
  determine a state, leaves the input UNKNOWN — "we did not check" — since
  the JSON-LD fetch that carries `validThrough` may be exactly what failed.
* **`posting_state` is taken from the resolver only.** `"reposted"` is not
  inferred here from history features: that would let the policy layer
  silently overrule the resolver's direct observation. If a posting is to be
  called `reposted`, the resolver/history layer must say so in the
  `posting_state` claim itself.
* **`corroborating_hiring_signal` has exactly one source.** spec.md §4:
  "`team_signal` is the only source for the policy input
  `corroborating_hiring_signal`". It is read from `team_signal` evidence
  (or injected via the `team_signal=` keyword) and is UNKNOWN otherwise —
  never inferred from board activity.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from itertools import product
from typing import TYPE_CHECKING, Any

from rli.models.decision import EvidenceQuality, RecommendedAction
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import (
    UNKNOWN,
    PolicyInputs,
    PostingState,
    PublishRecency,
    Unknown,
)
from rli.models.time import ensure_aware

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.config import Config
    from rli.history.features import PostingHistoryFeatures
    from rli.policy.action import PolicyThresholds

__all__ = [
    "CLAIM_BOARD_ABSENT",
    "CLAIM_BOARD_LISTING",
    "CLAIM_DECLARED_EXPIRY",
    "CLAIM_FIRST_PUBLISHED",
    "CLAIM_POSTING_STATE",
    "CLAIM_TEAM_SIGNAL",
    "PRIMARY_PUBLISH_QUALITIES",
    "best_publish_claim",
    "could_change_action",
    "derive_policy_inputs",
    "unpopulated",
]

CLAIM_POSTING_STATE = "posting_state"
CLAIM_FIRST_PUBLISHED = "first_published"
CLAIM_DECLARED_EXPIRY = "declared_expiry"
CLAIM_BOARD_LISTING = "board_listing"
CLAIM_BOARD_ABSENT = "board_absent"
CLAIM_TEAM_SIGNAL = "corroborating_hiring_signal"

# spec.md §3 source ranking, best first. Only these two count as "primary
# publish evidence" for spec.md §5's `apply_now` branch.
PRIMARY_PUBLISH_QUALITIES: tuple[str, ...] = ("ats_native", "page_structured")

_RESOLVER_PROBE = "resolve_posting"
_TEAM_SIGNAL_PROBE = "team_signal"
_DECISIVE_STATES = frozenset({"open", "closed", "reposted"})
_TRUE_VALUES = frozenset({"true", "yes", "1"})
_FALSE_VALUES = frozenset({"false", "no", "0"})


# ---------------------------------------------------------------------------
# Evidence readers
# ---------------------------------------------------------------------------


def _claims(evidence: Iterable[EvidenceItem], claim_type: str) -> list[EvidenceItem]:
    return [item for item in evidence if item.claim_type == claim_type]


def _newest(items: Sequence[EvidenceItem]) -> EvidenceItem | None:
    """The freshest evidence item, by `available_at` then `fetched_at` then id.

    `available_at` (not `fetched_at`) leads because it is the timestamp the
    replay window is defined on (spec.md §3), so ordering by it here matches
    the ordering a point-in-time replay would see. The remaining keys only
    exist to make the choice total and therefore reproducible.
    """
    if not items:
        return None
    return max(items, key=lambda e: (e.available_at, e.fetched_at, e.id))


def _posting_state(evidence: Sequence[EvidenceItem]) -> PostingState | Unknown:
    claims = _claims(evidence, CLAIM_POSTING_STATE)
    resolver_claims = [c for c in claims if c.probe == _RESOLVER_PROBE]
    newest = _newest(resolver_claims or claims)
    if newest is None:
        return UNKNOWN
    value = newest.value.strip().lower()
    if value in {"open", "closed", "reposted", "unknown"}:
        return value  # type: ignore[return-value]
    return UNKNOWN


def best_publish_claim(evidence: Sequence[EvidenceItem]) -> EvidenceItem | None:
    """Best primary `first_published` claim: `ats_native` > `page_structured`.

    Claims without a parsed `source_event_at` are ignored: a publish claim we
    could not place on the timeline cannot answer "is it recent?".
    """
    candidates = [
        c
        for c in _claims(evidence, CLAIM_FIRST_PUBLISHED)
        if c.source_event_at is not None and c.source_quality in PRIMARY_PUBLISH_QUALITIES
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda c: (
            PRIMARY_PUBLISH_QUALITIES.index(c.source_quality),
            c.source_event_at,  # earliest publication wins within a tier
            c.id,
        ),
    )


def _publish_recency(
    evidence: Sequence[EvidenceItem], now: datetime, recent_publish_days: int
) -> PublishRecency | Unknown:
    best = best_publish_claim(evidence)
    if best is None or best.source_event_at is None:
        return UNKNOWN
    age = now - best.source_event_at
    return "recent" if age <= timedelta(days=recent_publish_days) else "not_recent"


def _declared_expiry(
    evidence: Sequence[EvidenceItem],
    posting_state: PostingState | Unknown,
    *,
    resolver_ok: bool,
) -> datetime | None | Unknown:
    claim = _newest(
        [c for c in _claims(evidence, CLAIM_DECLARED_EXPIRY) if c.source_event_at is not None]
    )
    if claim is not None and claim.source_event_at is not None:
        return claim.source_event_at

    resolver_ran = any(item.probe == _RESOLVER_PROBE for item in evidence)
    decisive = not isinstance(posting_state, Unknown) and posting_state in _DECISIVE_STATES
    if resolver_ok and resolver_ran and decisive:
        # Checked; the publisher declared no expiry (spec.md §3: validThrough
        # is present "only when an expiration date exists").
        return None
    return UNKNOWN


def _team_signal(evidence: Sequence[EvidenceItem]) -> bool | Unknown:
    claim = _newest(
        [
            c
            for c in _claims(evidence, CLAIM_TEAM_SIGNAL)
            if c.probe == _TEAM_SIGNAL_PROBE
        ]
    )
    if claim is None:
        return UNKNOWN
    value = claim.value.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return UNKNOWN


# ---------------------------------------------------------------------------
# derive_policy_inputs
# ---------------------------------------------------------------------------


def derive_policy_inputs(
    evidence: Sequence[EvidenceItem],
    features: PostingHistoryFeatures | None,
    event_signals: tuple[bool | Unknown, bool | Unknown] | None,
    now: datetime,
    *,
    cfg: Config | PolicyThresholds | None = None,
    resolver_ok: bool = True,
    team_signal: bool | Unknown = UNKNOWN,
) -> PolicyInputs:
    """Build `PolicyInputs` from the case file's evidence and derived features.

    Arguments:
        evidence: every `EvidenceItem` gathered so far for this posting.
        features: `rli.history.features.posting_features` output, or `None`
            when no history exists (leaves `repost_pattern` UNKNOWN — spec.md
            §4: "missing history never means flat hiring").
        event_signals: the `(material_negative_event, freeze_or_pause)` pair
            returned by `rli.events.policy_signals.derive_policy_signals`, or
            `None` when `company_events` has not run (both UNKNOWN).
        now: the decision clock. Must be timezone-aware.
        cfg: `Config` or `PolicyThresholds`; supplies `recent_publish_days`.
            Defaults to the loaded project config.
        resolver_ok: `resolve_posting`'s `ProbeResult.ok`. `False` prevents
            the known-absent `declared_expiry=None` conclusion.
        team_signal: direct injection for `corroborating_hiring_signal`, for
            callers that hold the `team_signal` probe result but have not yet
            turned it into evidence. Evidence wins when both are present.
    """
    from rli.policy.action import PolicyThresholds as _PolicyThresholds

    now = ensure_aware(now, "now")
    thresholds = _PolicyThresholds.coerce(cfg)

    posting_state = _posting_state(evidence)
    material_negative_event, freeze_or_pause = (
        event_signals if event_signals is not None else (UNKNOWN, UNKNOWN)
    )

    from_evidence = _team_signal(evidence)
    corroborating = from_evidence if not isinstance(from_evidence, Unknown) else team_signal

    return PolicyInputs(
        posting_state=posting_state,
        publish_recency=_publish_recency(evidence, now, thresholds.recent_publish_days),
        material_negative_event=material_negative_event,
        freeze_or_pause=freeze_or_pause,
        declared_expiry=_declared_expiry(evidence, posting_state, resolver_ok=resolver_ok),
        repost_pattern=(features.repost_pattern if features is not None else UNKNOWN),
        corroborating_hiring_signal=corroborating,
    )


def unpopulated(inputs: PolicyInputs) -> set[str]:
    """The unresolved questions: policy inputs still at the UNKNOWN sentinel.

    spec.md §4: "An unresolved question is a policy input (§5) that is still
    unpopulated." Thin wrapper over `PolicyInputs.unpopulated()` so the
    controller depends on one module (`rli.policy`) rather than reaching into
    the model.
    """
    return inputs.unpopulated()


# ---------------------------------------------------------------------------
# could_change_action
# ---------------------------------------------------------------------------

# Enumeration domains for the brute-force relevance search below. Every
# domain includes UNKNOWN so that "stays unresolved" is one of the outcomes
# considered. `declared_expiry` is a datetime, i.e. an infinite domain, so it
# is sampled at the four points the policy can distinguish: absent, already
# passed, inside the recheck cap, and beyond the recheck cap.
_EXPIRY_PAST = "past"
_EXPIRY_SOON = "soon"
_EXPIRY_FAR = "far"

_INPUT_DOMAINS: dict[str, tuple[Any, ...]] = {
    "posting_state": (UNKNOWN, "open", "closed", "reposted", "unknown"),
    "publish_recency": (UNKNOWN, "recent", "not_recent"),
    "material_negative_event": (UNKNOWN, True, False),
    "freeze_or_pause": (UNKNOWN, True, False),
    "declared_expiry": (UNKNOWN, None, _EXPIRY_PAST, _EXPIRY_SOON, _EXPIRY_FAR),
    "repost_pattern": (UNKNOWN, "repeated_unchanged", "changed", "none"),
    "corroborating_hiring_signal": (UNKNOWN, True, False),
}

_QUALITY_DOMAIN = ("strong", "mixed", "weak")
_LONG_LIVED_DOMAIN: tuple[bool | Unknown, ...] = (UNKNOWN, True, False)


def _materialize_expiry(token: Any, now: datetime, cap_days: int) -> Any:
    if token is _EXPIRY_PAST:
        return now - timedelta(days=1)
    if token is _EXPIRY_SOON:
        return now + timedelta(days=max(cap_days - 1, 0))
    if token is _EXPIRY_FAR:
        return now + timedelta(days=cap_days + 30)
    return token


def could_change_action(
    inputs: PolicyInputs,
    current_action: RecommendedAction,
    *,
    quality: EvidenceQuality | None = None,
    long_lived: bool | Unknown = UNKNOWN,
    now: datetime | None = None,
    cfg: Config | PolicyThresholds | None = None,
) -> set[str]:
    """Unpopulated policy inputs whose value could still change the action.

    This is the deterministic core of spec.md §4's hard stop ("no unresolved
    question could change the action") and of the controller's eligibility
    rule ("a candidate probe is eligible only if it can populate at least one
    of them"). It is computed by running the *real* `rli.policy.action.decide`
    over an enumeration of the unresolved inputs, so it can never drift from
    the policy the way a hand-maintained table would.

    An unresolved input `f` is included when either:

    * **direct** — some value of `f`, with everything else exactly as it is
      now, yields an action different from `current_action`; or
    * **joint** — there is some assignment of the *other* unresolved inputs
      under which two values of `f` yield different actions.

    The joint test is what makes the stop rule safe. spec.md §5's
    `skip` branch needs `repost_pattern` **and** `corroborating_hiring_signal`
    together: with only the direct test, neither input alone would change the
    action, the controller would stop, and the branch could never fire. The
    joint test is the standard "is this variable relevant to the function"
    definition and is deliberately conservative — it keeps a question open
    whenever some reachable combination makes it matter.

    Note the asymmetry: the joint test compares two *counterfactual* worlds
    against each other, never a counterfactual against `current_action`.
    Comparing a joint counterfactual to `current_action` would blame `f` for a
    change actually caused by the other inputs that moved with it, and would
    mark nearly every input relevant.

    Arguments:
        quality: the evidence quality to hold fixed. Pass it when it is
            known and cannot move. Left `None`, all three values are
            enumerated, because a probe that populates an input generally
            appends evidence and can therefore change
            `rli.policy.quality.evidence_quality` at the same time; holding a
            stale quality fixed would under-approximate the set and stop the
            loop early.
        long_lived: `PostingHistoryFeatures.long_lived`. It is not a
            `PolicyInputs` field but the §5 `skip` branch reads it, so it is
            enumerated when UNKNOWN. It is never a member of the returned
            set: that set is always a subset of `PolicyInputs` field names,
            which is what the controller maps to probes.

    Cost: bounded by the product of the domains above (<= 8100 assignments
    times up to 3 qualities times up to 3 `long_lived` values), each a pure
    dict/branch evaluation. Fully populated inputs cost nothing.
    """
    # `_branch`, not `decide`: branch selection IS the action, and it is the
    # function `policy_version()` hashes, so this enumeration can never drift
    # from the frozen policy. Skipping `decide` also skips `recheck_after_days`,
    # which by design cannot change the action.
    from rli.policy.action import PolicyThresholds as _PolicyThresholds
    from rli.policy.action import _branch

    thresholds = _PolicyThresholds.coerce(cfg)
    now = ensure_aware(now, "now") if now is not None else _reference_now()

    free_fields = sorted(inputs.unpopulated())
    if not free_fields:
        return set()

    qualities = (quality,) if quality is not None else _QUALITY_DOMAIN
    long_lived_values = (
        (long_lived,) if not isinstance(long_lived, Unknown) else _LONG_LIVED_DOMAIN
    )

    base = inputs.model_dump()
    # Materialize the expiry tokens once, up front: the inner loop below runs
    # tens of thousands of times and must stay pure tuple/dict work.
    domains = [
        tuple(
            _materialize_expiry(token, now, thresholds.recheck_cap_days)
            for token in _INPUT_DOMAINS[name]
        )
        if name == "declared_expiry"
        else _INPUT_DOMAINS[name]
        for name in free_fields
    ]

    # action_by[(quality, long_lived, assignment)] -> action, where
    # `assignment` is the tuple of values for `free_fields` in order.
    action_by: dict[tuple[Any, ...], RecommendedAction] = {}
    for q, ll, assignment in product(qualities, long_lived_values, product(*domains)):
        candidate = dict(base)
        candidate.update(zip(free_fields, assignment, strict=True))
        action_by[(q, ll, assignment)] = _branch(
            PolicyInputs.model_construct(**candidate),
            q,  # type: ignore[arg-type]
            ll,
        )[1]

    # Every free field is UNKNOWN by definition, and UNKNOWN is in every
    # domain, so the current assignment is always a key of `action_by`.
    current_assignment = tuple(UNKNOWN for _ in free_fields)
    relevant: set[str] = set()

    for index, name in enumerate(free_fields):
        domain = domains[index]

        # Direct test: vary `name` alone, against the action we hold now.
        for q in qualities:
            for ll in long_lived_values:
                for value in domain:
                    key = (q, ll, _replaced(current_assignment, index, value))
                    if action_by[key] != current_action:
                        relevant.add(name)
                        break
                if name in relevant:
                    break
            if name in relevant:
                break
        if name in relevant:
            continue

        # Joint test: does `name` ever move the action, holding some
        # assignment of the other unresolved inputs fixed?
        seen: dict[tuple[Any, ...], RecommendedAction] = {}
        for key, action in action_by.items():
            q, ll, assignment = key
            others = (q, ll, _replaced(assignment, index, None))
            previous = seen.setdefault(others, action)
            if previous != action:
                relevant.add(name)
                break

    return relevant


def _replaced(assignment: tuple[Any, ...], index: int, value: Any) -> tuple[Any, ...]:
    return assignment[:index] + (value,) + assignment[index + 1 :]


def _reference_now() -> datetime:
    """Clock for the enumeration when the caller did not supply one.

    The result does not depend on it: `now` is used only to synthesize expiry
    datetimes *relative to itself* (already passed / inside the cap / beyond
    the cap), so any instant produces the same set. Callers should still pass
    an explicit `now` so a replayed controller decision is reproducible from
    its inputs alone (spec.md §6).
    """
    from rli.models.time import now_utc

    return now_utc()
