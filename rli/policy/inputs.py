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
* `refreshed_at` — an ATS `updated_at` timestamp that COINCIDES with a
  content-hash change actually observed in our own or archived captures
  (spec.md §5, Amendment 2026-09-10). `value`/`source_event_at` are the ATS
  timestamp; `available_at` is the later of the `updated_at` claim's own
  `available_at` and the capture that revealed the change, because we could
  not have known about the change before that capture. Synthesized by
  `rli.eval.case` (which holds the corpus connection) and attributed to its
  own non-network probe name, for the same reason `board_absent` is: no
  probe fetched it, it is a re-reading of captures we already hold.

Already emitted by `rli.probes.resolve_posting`: `first_published`,
`updated_at`, `declared_expiry`, `board_listing`. Note that `updated_at`
comes from the ALWAYS-RUN resolver and from nowhere else — no dynamic probe
emits publish or refresh evidence — which is why `publish_recency` cannot
move during the agent loop and why `could_change_action` holds
`last_refreshed_at` fixed rather than enumerating it.

Emitted by `rli.probes.company_events`, and answering THREE policy inputs at
once (`material_negative_event`, `freeze_or_pause`,
`last_material_event_at`):

* one claim per dated event, whose `claim_type` is the raw event type
  (`layoff`, `hiring_freeze`, ... — the full set is
  `rli.events.policy_signals.EVENT_CLAIM_TYPES`), whose `source_event_at` is
  the event's calendar date at midnight UTC, and whose `value` carries the
  stored materiality as a prefix (`"material: Acme lays off 200"`);
* one `company_events_searched` claim
  (`rli.events.policy_signals.CLAIM_EVENTS_SEARCHED`) whose value is the
  moment that company's events were searched. This is what makes the `False`
  answer — "we checked, and nothing qualifying is in the window" — an
  evidence-backed statement rather than an inference from silence.

**No `company_events` evidence at all therefore means UNKNOWN, never False.**
That is the whole point: `False` is a report on a search that happened, so
it requires the search claim; an empty evidence list is "we have not looked",
which is an unresolved question for the controller (spec.md §4), not a
clean bill of health. These three inputs are derived here, from the claims,
and NOT handed in pre-computed by whoever holds the events store — a
pre-populated input made `company_events` ineligible under spec.md §4's
`populates & unpopulated` rule, so the probe never ran, so a `wait` could be
returned citing no layoff evidence at all (see `rli.eval.case`'s docstring
for the full account of that bug).

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
* **`publish_recency` measures the latest of publication and REFRESH.**
  spec.md §5's Amendment 2026-09-10 redefines `recent` as "latest of first
  publish **or** an ATS `updated_at` that coincides with an observed
  content-hash change, <= 30 days ago". `last_publish_or_refresh` is that
  "latest of"; a bare `updated_at` never reaches it, because only a
  corroborated refresh is turned into a `refreshed_at` claim upstream. With
  neither a primary publish claim nor a `refreshed_at` claim the input stays
  UNKNOWN — unchanged behaviour, and still "we have not established a
  trustworthy date" rather than `not_recent`.
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
* **`corroborating_hiring_signal` has exactly one source, and that source
  is a CLAIM.** spec.md §4: "`team_signal` is the only source for the policy
  input `corroborating_hiring_signal`". It is read from a
  `corroborating_hiring_signal` claim emitted by the `team_signal` probe and
  is UNKNOWN otherwise — never inferred from board activity, and never
  threaded in from a `ProbeResult.data` blob. There was once a
  `team_signal=` keyword on `derive_policy_inputs` for exactly that
  threading, used by `rli.eval.case`; it is gone, parameter and all, because
  a `data` blob carries no `available_at` and therefore walked straight past
  replay's point-in-time gate. `rli.eval.case.extend_case_state` documents
  what that cost.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from itertools import product
from typing import TYPE_CHECKING, Any, NamedTuple

from rli.events.policy_signals import (
    CLAIM_EVENTS_SEARCHED,
    COMPANY_EVENTS_PROBE,
    EVENT_CLAIM_TYPES,
    EventFact,
    EventSignals,
    parse_event_claim_value,
    signals_from_facts,
)
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
    "CLAIM_REFRESHED_AT",
    "CLAIM_TEAM_SIGNAL",
    "PRIMARY_PUBLISH_QUALITIES",
    "best_publish_claim",
    "could_change_action",
    "derive_policy_inputs",
    "freeze_event_claims",
    "last_publish_or_refresh",
    "material_event_claims",
    "newest_refresh_claim",
    "unpopulated",
]

CLAIM_POSTING_STATE = "posting_state"
CLAIM_FIRST_PUBLISHED = "first_published"
CLAIM_DECLARED_EXPIRY = "declared_expiry"
CLAIM_BOARD_LISTING = "board_listing"
CLAIM_BOARD_ABSENT = "board_absent"
CLAIM_TEAM_SIGNAL = "corroborating_hiring_signal"
CLAIM_REFRESHED_AT = "refreshed_at"

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


def newest_refresh_claim(evidence: Sequence[EvidenceItem]) -> EvidenceItem | None:
    """The freshest `refreshed_at` claim, or `None`.

    The sibling of `best_publish_claim`, and public for the same reason: the
    explanation layer must cite the very claim whose date the policy used,
    not merely restate the date.

    There is no source ranking to apply — `refreshed_at` is synthesized only
    from an `ats_native` `updated_at` corroborated by an observed hash change
    (`rli.eval.case`), so every such claim is already primary. Freshness is
    therefore the only ordering, and it is `_newest`'s (`available_at` first,
    matching the replay window). Claims without a parsed `source_event_at`
    are dropped for the same reason `best_publish_claim` drops them: a date
    we cannot place on the timeline cannot answer "is it recent?".
    """
    return _newest(
        [c for c in _claims(evidence, CLAIM_REFRESHED_AT) if c.source_event_at is not None]
    )


def last_publish_or_refresh(evidence: Sequence[EvidenceItem]) -> datetime | None:
    """The latest of the best primary publish date and the newest refresh date.

    This is spec.md §5 Amendment 2026-09-10's "latest of first publish **or**
    an ATS `updated_at` that coincides with an observed content-hash change".
    `None` when neither exists — "we hold no date for when this posting last
    moved", which the policy's P3c branch reads as "not refreshed since the
    event" and `_publish_recency` reads as UNKNOWN.

    Note the asymmetry with `best_publish_claim`, which breaks ties toward
    the EARLIEST date because `first_published` means the first publication.
    Here the reduction is MAX, because the question is the opposite one: when
    did this posting last show any sign of life.
    """
    best = best_publish_claim(evidence)
    refresh = newest_refresh_claim(evidence)
    dates = [
        d
        for d in (
            best.source_event_at if best is not None else None,
            refresh.source_event_at if refresh is not None else None,
        )
        if d is not None
    ]
    return max(dates) if dates else None


def _publish_recency(
    evidence: Sequence[EvidenceItem], now: datetime, recent_publish_days: int
) -> PublishRecency | Unknown:
    latest = last_publish_or_refresh(evidence)
    if latest is None:
        return UNKNOWN
    age = now - latest
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
        [c for c in _claims(evidence, CLAIM_TEAM_SIGNAL) if c.probe == _TEAM_SIGNAL_PROBE]
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
# The `company_events` claims adapter
# ---------------------------------------------------------------------------

#: Materiality assumed for a `layoff`/`shutdown` claim whose `value` prefix
#: could not be parsed (an evidence row written by an older probe version, a
#: hand-written fixture, anything not produced by
#: `rli.events.policy_signals.format_event_claim_value`).
#:
#: FAIL-SAFE, and deliberately so. Guessing `"minor"` would silently drop a
#: `wait` on a real layoff — exactly the failure this whole claims path was
#: introduced to prevent — while guessing `"material"` can only over-warn,
#: and an over-warning is visible in the reasons and arguable by the reader.
#: `rli.events.store.classify_materiality` makes the same call for the same
#: reason when a layoff headline carries no extractable magnitude, and
#: `rli.events.policy_signals`' module docstring documents this end of it.
_FAIL_SAFE_MATERIALITY = "material"


def _events_searched(evidence: Iterable[EvidenceItem]) -> bool:
    """Did `company_events` report an actual search (spec.md §4's "checked")?

    The presence of its `company_events_searched` claim, and nothing else.
    Note it is NOT enough for the probe merely to have run: in replay the
    point-in-time gate drops this claim when the search post-dates `T`, which
    is precisely how "searched, but not yet at this instant" collapses back
    to UNKNOWN without a second copy of that comparison living here.
    """
    return any(
        item.probe == COMPANY_EVENTS_PROBE and item.claim_type == CLAIM_EVENTS_SEARCHED
        for item in evidence
    )


def _event_claim_facts(evidence: Iterable[EvidenceItem]) -> list[tuple[EvidenceItem, EventFact]]:
    """Every `company_events` per-event claim, paired with its `EventFact`.

    The pair — not just the fact — because the explanation layer has to cite
    the very claims that made a boolean True, and re-selecting them from a
    second predicate is how the two would drift apart.

    Three filters, each load-bearing:

    * `probe == company_events` — another probe emitting a claim that happens
      to be named `funding` must not feed the company-event rule;
    * `claim_type in EVENT_CLAIM_TYPES` — excludes the
      `company_events_searched` collection claim (read separately, by
      `_events_searched`) and anything else the probe grows later;
    * `source_event_at is not None` — an event we cannot place on the
      calendar cannot be windowed, so it cannot answer "inside the lookback
      window?" either way. Dropping it is the same discipline
      `best_publish_claim` applies to an undated publish claim.
    """
    pairs: list[tuple[EvidenceItem, EventFact]] = []
    for item in evidence:
        if item.probe != COMPANY_EVENTS_PROBE:
            continue
        if item.claim_type not in EVENT_CLAIM_TYPES:
            continue
        if item.source_event_at is None:
            continue
        materiality, _headline = parse_event_claim_value(item.value)
        pairs.append(
            (
                item,
                EventFact(
                    event_type=item.claim_type,
                    event_date=item.source_event_at.date(),
                    materiality=materiality or _FAIL_SAFE_MATERIALITY,
                ),
            )
        )
    return pairs


def _event_signals(
    evidence: Sequence[EvidenceItem], now: datetime, window_days: int
) -> EventSignals:
    """The three event inputs, read from `company_events` claims alone."""
    return signals_from_facts(
        [fact for _item, fact in _event_claim_facts(evidence)],
        now,
        window_days=window_days,
        searched=_events_searched(evidence),
    )


def _claims_that_flip(
    evidence: Sequence[EvidenceItem], now: datetime, window_days: int, index: int
) -> list[EvidenceItem]:
    """The claims that would, ALONE, set signal `index` of the triple True.

    `index` is `0` for `material_negative_event` and `1` for
    `freeze_or_pause`.

    The selection is made by asking `signals_from_facts` about each claim on
    its own rather than by restating its predicates here. That is the point:
    the window, the event-type sets and the materiality rule then have
    exactly ONE definition in the codebase, and `material_event_claims` /
    `freeze_event_claims` cannot drift from the booleans they are supposed to
    explain even if that rule changes. `searched=True` is passed because the
    question asked here is "does this fact qualify?", not "have we searched?"
    — the caller has already established the latter by getting a `True` out
    of `_event_signals`.

    The cost is one pure call per company-event claim, on a list that holds
    the dated events of a single company; this runs once per explanation, not
    inside any loop.
    """
    return [
        item
        for item, fact in _event_claim_facts(evidence)
        if signals_from_facts([fact], now, window_days=window_days, searched=True)[index] is True
    ]


def _window_days(cfg: Config | PolicyThresholds | None) -> int:
    from rli.policy.action import PolicyThresholds as _PolicyThresholds

    return _PolicyThresholds.coerce(cfg).negative_event_window_days


def material_event_claims(
    evidence: Sequence[EvidenceItem],
    now: datetime,
    *,
    cfg: Config | PolicyThresholds | None = None,
) -> list[EvidenceItem]:
    """The windowed `company_events` claims that make `material_negative_event` True.

    Public because `rli.policy.explain_stub` must cite exactly these — the
    layoff/shutdown claims the policy actually read — and not "every claim
    the `company_events` probe produced", which now also includes the
    `company_events_searched` collection claim and any unrelated funding or
    expansion item. A reason whose text says "a dated layoff or shutdown"
    while citing a funding headline is an unsupported citation under
    spec.md §9 even though every id in it resolves.

    Empty when the input is False or UNKNOWN, and never empty when it is
    True — the same call decides both (see `_claims_that_flip`).
    """
    return _claims_that_flip(evidence, ensure_aware(now, "now"), _window_days(cfg), 0)


def freeze_event_claims(
    evidence: Sequence[EvidenceItem],
    now: datetime,
    *,
    cfg: Config | PolicyThresholds | None = None,
) -> list[EvidenceItem]:
    """The windowed `company_events` claims that make `freeze_or_pause` True.

    The sibling of `material_event_claims`, public for the same reason.
    """
    return _claims_that_flip(evidence, ensure_aware(now, "now"), _window_days(cfg), 1)


# ---------------------------------------------------------------------------
# derive_policy_inputs
# ---------------------------------------------------------------------------


def derive_policy_inputs(
    evidence: Sequence[EvidenceItem],
    features: PostingHistoryFeatures | None,
    now: datetime,
    *,
    cfg: Config | PolicyThresholds | None = None,
    resolver_ok: bool = True,
) -> PolicyInputs:
    """Build `PolicyInputs` from the case file's evidence and derived features.

    A pure function of `(evidence, features, now)`. Every input value comes
    from the evidence list or from the history features; `resolver_ok` is the
    one remaining keyword, and it supplies no VALUE — it says whether
    `resolve_posting` ran at all, which is what licenses the known-absent
    `declared_expiry=None` conclusion below. In particular there is NO
    `event_signals` parameter and NO `team_signal` parameter:
    `material_negative_event`, `freeze_or_pause`, `last_material_event_at`
    and `corroborating_hiring_signal` are all read out of their probe's
    claims like every other evidence-backed input, so calling this twice with
    the same evidence cannot produce two different answers depending on who
    called it.

    That "no value parameters" rule is a point-in-time guarantee, not only a
    tidiness one. An `EvidenceItem` carries `available_at`, so replay can
    gate it (spec.md §3: "expose only evidence with `available_at <= T`"); a
    value handed in through a keyword carries no timestamp and cannot be
    gated. `rli.eval.case.extend_case_state` used to pass the `team_signal`
    probe's `data["corroborating_hiring_signal"]` through a `team_signal=`
    keyword here, and that is precisely how a build-time boolean ended up
    deciding archive-era replay cases whose team_signal claims the gate had
    correctly dropped. The keyword was deleted rather than left unused, so
    the leak is now unreachable by construction rather than by discipline.

    Arguments:
        evidence: every `EvidenceItem` gathered so far for this posting.
        features: `rli.history.features.posting_features` output, or `None`
            when no history exists (leaves `repost_pattern` UNKNOWN — spec.md
            §4: "missing history never means flat hiring").
        now: the decision clock. Must be timezone-aware.
        cfg: `Config` or `PolicyThresholds`; supplies `recent_publish_days`.
            Defaults to the loaded project config.
        resolver_ok: `resolve_posting`'s `ProbeResult.ok`. `False` prevents
            the known-absent `declared_expiry=None` conclusion.
    """
    from rli.policy.action import PolicyThresholds as _PolicyThresholds

    now = ensure_aware(now, "now")
    thresholds = _PolicyThresholds.coerce(cfg)

    posting_state = _posting_state(evidence)
    # The three event inputs come from `company_events`' CLAIMS — never from
    # a pre-computed triple handed in by whoever holds the events store. See
    # the evidence-claim contract above, and `rli.eval.case`'s docstring for
    # what the pre-computed version cost.
    material_negative_event, freeze_or_pause, last_material_event_at = _event_signals(
        evidence, now, thresholds.negative_event_window_days
    )

    return PolicyInputs(
        posting_state=posting_state,
        publish_recency=_publish_recency(evidence, now, thresholds.recent_publish_days),
        material_negative_event=material_negative_event,
        freeze_or_pause=freeze_or_pause,
        declared_expiry=_declared_expiry(evidence, posting_state, resolver_ok=resolver_ok),
        repost_pattern=(features.repost_pattern if features is not None else UNKNOWN),
        corroborating_hiring_signal=_team_signal(evidence),
        last_material_event_at=last_material_event_at,
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
# considered, and **UNKNOWN is index 0 of every domain** — the flat-index
# arithmetic in `could_change_action` relies on that to locate the "all other
# free fields still unresolved" slice without searching for it.
#
# Two fields are datetimes, i.e. infinite domains, so each is sampled at the
# points the policy can distinguish, expressed as TOKENS that a per-field
# materializer turns into real datetimes anchored to something:
#
# * `declared_expiry` is anchored to `now` (absent, already passed, inside
#   the recheck cap, beyond the cap) — the four cases `_recheck_after_days`
#   separates;
# * `last_material_event_at` is anchored to `last_refreshed_at` (absent,
#   before the refresh, after the refresh) — the only distinction P3c draws.
_EXPIRY_PAST = "past"
_EXPIRY_SOON = "soon"
_EXPIRY_FAR = "far"
_EVENT_BEFORE_REFRESH = "event_before_refresh"
_EVENT_AFTER_REFRESH = "event_after_refresh"

_INPUT_DOMAINS: dict[str, tuple[Any, ...]] = {
    "posting_state": (UNKNOWN, "open", "closed", "reposted", "unknown"),
    "publish_recency": (UNKNOWN, "recent", "not_recent"),
    "material_negative_event": (UNKNOWN, True, False),
    "freeze_or_pause": (UNKNOWN, True, False),
    "declared_expiry": (UNKNOWN, None, _EXPIRY_PAST, _EXPIRY_SOON, _EXPIRY_FAR),
    "repost_pattern": (UNKNOWN, "repeated_unchanged", "changed", "none"),
    "corroborating_hiring_signal": (UNKNOWN, True, False),
    "last_material_event_at": (
        UNKNOWN,
        None,
        _EVENT_BEFORE_REFRESH,
        _EVENT_AFTER_REFRESH,
    ),
}

_QUALITY_DOMAIN = ("strong", "mixed", "weak")
_LONG_LIVED_DOMAIN: tuple[bool | Unknown, ...] = (UNKNOWN, True, False)


class _Anchors(NamedTuple):
    """The reference points the datetime domains are materialized against."""

    now: datetime
    cap_days: int
    last_refreshed_at: datetime | None | Unknown


def _materialize_expiry(token: Any, anchors: _Anchors) -> Any:
    if token is _EXPIRY_PAST:
        return anchors.now - timedelta(days=1)
    if token is _EXPIRY_SOON:
        return anchors.now + timedelta(days=max(anchors.cap_days - 1, 0))
    if token is _EXPIRY_FAR:
        return anchors.now + timedelta(days=anchors.cap_days + 30)
    return token


def _materialize_event_date(token: Any, anchors: _Anchors) -> Any:
    """Place a material-event date on either side of `last_refreshed_at`.

    When there is no refresh date, "before" and "after" are not distinct
    situations — P3c's `not (refresh > event)` conjunct is vacuously true
    whatever the event date is — so both tokens collapse onto the same
    instant. That is behaviourally identical and keeps the domain a constant
    size, so the flat-index arithmetic does not have to special-case it.
    """
    anchor = anchors.last_refreshed_at
    if not isinstance(anchor, datetime):
        if token in (_EVENT_BEFORE_REFRESH, _EVENT_AFTER_REFRESH):
            return anchors.now - timedelta(days=1)
        return token
    if token is _EVENT_BEFORE_REFRESH:
        return anchor - timedelta(days=1)
    if token is _EVENT_AFTER_REFRESH:
        return anchor + timedelta(days=1)
    return token


#: Per-field token -> value. A field absent from this map has a literal
#: domain and is used as-is. Generalized from the two inline `name ==
#: "declared_expiry"` special cases the single-datetime version had, so a
#: third sampled datetime field is a dict entry rather than another branch.
_MATERIALIZERS: dict[str, Callable[[Any, _Anchors], Any]] = {
    "declared_expiry": _materialize_expiry,
    "last_material_event_at": _materialize_event_date,
}


def could_change_action(
    inputs: PolicyInputs,
    current_action: RecommendedAction,
    *,
    quality: EvidenceQuality | None = None,
    long_lived: bool | Unknown = UNKNOWN,
    last_refreshed_at: datetime | None | Unknown = UNKNOWN,
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
        last_refreshed_at: `last_publish_or_refresh(evidence)`, read by the
            P3c branch. Held FIXED, never enumerated, unlike `long_lived`:
            publish and refresh evidence comes only from the always-run
            `resolve_posting` and from `rli.eval.case`'s re-reading of
            captures we already hold, so no dynamic probe can move it during
            the loop and enumerating it would multiply the search space for
            no gain. Its default (UNKNOWN, i.e. "no refresh date") is also
            the conservative direction: it is the value under which
            `material_negative_event` and `last_material_event_at` stay
            relevant, so a caller that forgets to pass it can only keep a
            question open too long, never close one too early.

    Cost: bounded by the product of the domains above (<= 32400 assignments
    times up to 3 qualities times up to 3 `long_lived` values, i.e. <= 291600
    `_branch` evaluations), each a pure dict/branch evaluation. Fully
    populated inputs cost nothing.

    --------------------------------------------------------------------
    How the enumeration is made cheap (and why it is still honest)
    --------------------------------------------------------------------

    Two optimizations, both forced by that bound — a straightforward
    implementation spends most of its time in pydantic and in tuple/dict
    bookkeeping rather than in the policy:

    1. **One stand-in `PolicyInputs`, mutated in place through
       `__dict__`.** Constructing a model per assignment dominated the
       profile. `_branch` is a PURE function: it reads fields off its
       argument, returns a tuple, and neither retains nor mutates the
       object — so a single instance can be re-used for every assignment.
       Writing `stand_in.__dict__[name] = value` bypasses pydantic's
       `__setattr__` (and its validation) entirely, which is safe here
       because every value written comes from a CLOSED enumeration domain
       defined in this module, not from user input; there is nothing to
       validate. It is a real `PolicyInputs`, so nothing about `_branch`'s
       type contract is being faked. **Revisit this if `_branch` ever
       retains, mutates or hashes its argument.**
    2. **A flat `list` indexed by a mixed-radix index**, over the dimensions
       `(quality, long_lived, *free_fields)` with the outer dimensions
       first, instead of a dict keyed by tuples. Because UNKNOWN is index 0
       of every domain, the "vary field `i`, hold every other free field
       unresolved" slice the DIRECT test needs is exactly
       `base + k * stride_i`, and the groups the JOINT test needs (two
       assignments differing ONLY at index `i`, same quality and
       `long_lived`) are exactly the arithmetic sequences of stride
       `stride_i` and length `size_i` inside each `stride_i * size_i` span.
       That equivalence is what makes this the same test the tuple-keyed
       version performed, just without materializing the keys.

    Measured on the all-UNKNOWN worst case (every field free, all three
    qualities, all three `long_lived` values — 291,600 `_branch` calls): the
    straightforward implementation takes ~3.9 s, this one ~0.63 s. The
    numbers are indicative of one machine, not a contract; what is a
    contract is that this function runs inside the agent loop once per
    iteration, so a multi-second worst case would be a real cost.
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
    long_lived_values = (long_lived,) if not isinstance(long_lived, Unknown) else _LONG_LIVED_DOMAIN

    # Materialize the datetime tokens once, up front: the inner loop below
    # runs hundreds of thousands of times and must stay pure attribute work.
    anchors = _Anchors(now, thresholds.recheck_cap_days, last_refreshed_at)
    domains: list[tuple[Any, ...]] = []
    for name in free_fields:
        materializer = _MATERIALIZERS.get(name)
        raw = _INPUT_DOMAINS[name]
        domains.append(
            raw if materializer is None else tuple(materializer(t, anchors) for t in raw)
        )

    sizes = [len(domain) for domain in domains]
    # Mixed-radix strides over the free fields, last field varying fastest —
    # which is precisely the order `itertools.product` yields.
    strides = [0] * len(free_fields)
    block_size = 1
    for index in range(len(free_fields) - 1, -1, -1):
        strides[index] = block_size
        block_size *= sizes[index]

    # Built once and reused for every (quality, long_lived) block: the
    # assignment sequence does not depend on either.
    assignments = list(product(*domains))

    stand_in = PolicyInputs.model_construct(**inputs.model_dump())
    fields = stand_in.__dict__  # see docstring: deliberate, and safe here
    actions: list[RecommendedAction] = []
    for q in qualities:
        for ll in long_lived_values:
            for assignment in assignments:
                for name, value in zip(free_fields, assignment, strict=True):
                    fields[name] = value
                actions.append(_branch(stand_in, q, ll, last_refreshed_at)[1])  # type: ignore[arg-type]

    relevant: set[str] = set()
    for index, name in enumerate(free_fields):
        size_i, stride_i = sizes[index], strides[index]

        # Direct test: vary `name` alone, against the action we hold now.
        # Every other free field is UNKNOWN, i.e. index 0 of its domain, so
        # the current assignment sits at offset 0 of each block.
        if any(
            actions[base + k * stride_i] != current_action
            for base in range(0, len(actions), block_size)
            for k in range(size_i)
        ):
            relevant.add(name)
            continue

        # Joint test: does `name` ever move the action, holding some
        # assignment of the other unresolved inputs (and of quality /
        # long_lived) fixed? Each `stride_i * size_i` span contains exactly
        # `stride_i` such groups, interleaved.
        span = stride_i * size_i
        if any(
            actions[start + offset + k * stride_i] != actions[start + offset]
            for start in range(0, len(actions), span)
            for offset in range(stride_i)
            for k in range(1, size_i)
        ):
            relevant.add(name)

    return relevant


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
