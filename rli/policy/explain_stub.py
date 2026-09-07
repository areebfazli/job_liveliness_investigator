"""Deterministic, evidence-cited reasons — the non-LLM explanation path.

This is **not** the spec.md §2 "LLM evidence-cited explanation" (that lands in
M5 with the investigator). It is the fallback that lets System A (full probes)
and System B (rules) emit the complete spec.md §1 output shape — including the
`reason` array — with no model call at all. spec.md §6 requires A, B and C to
share one frozen action policy; sharing one *explanation* implementation for
A and B keeps the only difference between them the probes they run, and keeps
`reason` reproducible in replay where an LLM is not.

**The invariant this module exists to guarantee** (spec.md §9: "every
user-facing reason maps to evidence"): every returned reason cites at least
one `evidence_ids` entry, and every cited id is the id of an `EvidenceItem`
actually present in the `evidence` argument. A policy input that is known but
not evidence-backed in the list produces **no reason** rather than an
uncited sentence — silence is correct, an unsupported claim is not. That rule
is enforced structurally at the end of `reasons_from_inputs`, not merely
observed by each writer, so a new reason cannot regress it.

Judgment calls:

* **Wording follows spec.md §1's example** ("Role was first published 11 days
  ago.") — plain past-tense statements of observation, never conclusions
  about employer intent (spec.md §5: "This policy is a user-effort heuristic,
  not a claim about employer intent"). No reason says "ghost job", "evergreen"
  or "filled" (spec.md §1/§5/§9).
* **Order is fixed** (state, publication, expiry, events, history, hiring
  signal) rather than ranked by importance, because a stable order is what
  makes System A/B output diffable across runs.
* **Some inputs are cited by probe, not by claim type.** `repost_pattern`,
  the company-event signals and `corroborating_hiring_signal` come from
  probes whose claim vocabulary is owned by `rli.probes`; citing "any
  evidence this probe produced" keeps this module correct as those probes
  evolve, at the cost of a slightly coarser citation.
* **`now` defaults to the current clock** so a caller can omit it, but replay
  and tests must pass it explicitly — an implicit clock in a replayed
  explanation is exactly the kind of leak spec.md §6 counts.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rli.models.decision import ReasonItem
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import PolicyInputs, Unknown
from rli.models.time import ensure_aware, now_utc
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_DECLARED_EXPIRY,
    CLAIM_POSTING_STATE,
    best_publish_claim,
)

__all__ = ["reasons_from_inputs"]

_EVENTS_PROBE = "company_events"
_REPOST_PROBES = ("repost_history", "requirements_drift")
_TEAM_SIGNAL_PROBE = "team_signal"

_STATE_TEXT = {
    "open": "The posting was still listed on the company's job board when we checked.",
    "closed": "The posting was no longer listed on the company's job board when we checked.",
    "reposted": "The posting was observed closing and reappearing as a new listing.",
    "unknown": "We could not establish whether the posting is still listed.",
}


def _ids(items: Sequence[EvidenceItem]) -> list[str]:
    """Evidence ids in the order given, de-duplicated."""
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item.id, None)
    return list(seen)


def _by_claim(evidence: Sequence[EvidenceItem], claim_type: str) -> list[EvidenceItem]:
    return [item for item in evidence if item.claim_type == claim_type]


def _by_probe(evidence: Sequence[EvidenceItem], *probes: str) -> list[EvidenceItem]:
    return [item for item in evidence if item.probe in probes]


def reasons_from_inputs(
    inputs: PolicyInputs,
    evidence: Sequence[EvidenceItem],
    *,
    now: datetime | None = None,
) -> list[ReasonItem]:
    """Build the spec.md §1 `reason` array deterministically from evidence.

    Returns one `ReasonItem` per statement the evidence actually supports.
    Reasons whose supporting evidence is absent are dropped, so the result can
    legitimately be empty (an investigation that produced no evidence has
    nothing to say, and must not invent something).
    """
    when = ensure_aware(now, "now") if now is not None else now_utc()
    reasons: list[ReasonItem] = []

    # 1. Observed state.
    state = inputs.posting_state
    if not isinstance(state, Unknown):
        state_evidence = _by_claim(evidence, CLAIM_POSTING_STATE)
        if state_evidence:
            reasons.append(
                ReasonItem(text=_STATE_TEXT[state], evidence_ids=_ids(state_evidence))
            )

    # 2. Publication date. Phrased in days-ago to match spec.md §1's example;
    #    the source tier is named because spec.md §3 ranks sources and the
    #    user is entitled to know which one this came from.
    publish = best_publish_claim(evidence)
    if publish is not None and publish.source_event_at is not None:
        age_days = max(0, (when - publish.source_event_at).days)
        day_word = "day" if age_days == 1 else "days"
        reasons.append(
            ReasonItem(
                text=(
                    f"The role was first published {age_days} {day_word} ago, according to "
                    f"{publish.source_quality.replace('_', '-')} evidence."
                ),
                evidence_ids=_ids([publish]),
            )
        )

    # 3. Declared expiry (spec.md §3: publisher-declared, not proof of anything).
    if isinstance(inputs.declared_expiry, datetime):
        expiry_evidence = _by_claim(evidence, CLAIM_DECLARED_EXPIRY)
        if expiry_evidence:
            expired = inputs.declared_expiry <= when
            reasons.append(
                ReasonItem(
                    text=(
                        "The publisher declared this posting valid through "
                        f"{inputs.declared_expiry.date().isoformat()}"
                        + (", which has now passed." if expired else ".")
                    ),
                    evidence_ids=_ids(expiry_evidence),
                )
            )

    # 4. Company events. Only stated when TRUE: "we checked and found nothing"
    #    is not a user-facing reason, and UNKNOWN certainly is not.
    event_evidence = _by_probe(evidence, _EVENTS_PROBE)
    if event_evidence:
        if inputs.material_negative_event is True:
            reasons.append(
                ReasonItem(
                    text=(
                        "A dated layoff or shutdown was recorded for this company inside "
                        "the policy's lookback window."
                    ),
                    evidence_ids=_ids(event_evidence),
                )
            )
        if inputs.freeze_or_pause is True:
            reasons.append(
                ReasonItem(
                    text=(
                        "A dated hiring freeze or pause was recorded for this company "
                        "inside the policy's lookback window."
                    ),
                    evidence_ids=_ids(event_evidence),
                )
            )

    # 5. Repost history.
    repost_evidence = _by_probe(evidence, *_REPOST_PROBES)
    pattern = inputs.repost_pattern
    if repost_evidence and not isinstance(pattern, Unknown) and pattern != "none":
        text = (
            "The role has been reposted with unchanged content."
            if pattern == "repeated_unchanged"
            else "The role has been reposted with changed content."
        )
        reasons.append(ReasonItem(text=text, evidence_ids=_ids(repost_evidence)))

    # 6. Corroborating hiring signal.
    team_evidence = _by_probe(evidence, _TEAM_SIGNAL_PROBE)
    signal = inputs.corroborating_hiring_signal
    if team_evidence and not isinstance(signal, Unknown):
        reasons.append(
            ReasonItem(
                text=(
                    "Recent hiring activity was found for this team."
                    if signal
                    else "No recent hiring activity was found for this team."
                ),
                evidence_ids=_ids(team_evidence),
            )
        )

    # 7. Board absence (a fact the user should see even when it contradicts
    #    the resolver — spec.md §1 shows contradictions as `mixed`, and hiding
    #    the conflicting observation would leave that unexplained).
    absence_evidence = _by_claim(evidence, CLAIM_BOARD_ABSENT)
    if absence_evidence:
        reasons.append(
            ReasonItem(
                text="A board snapshot taken since then did not list this posting.",
                evidence_ids=_ids(absence_evidence),
            )
        )

    return _enforce_citations(reasons, evidence)


def _enforce_citations(
    reasons: Sequence[ReasonItem], evidence: Sequence[EvidenceItem]
) -> list[ReasonItem]:
    """Drop uncited reasons; raise if one cites an id that does not exist.

    The two failure modes are treated differently on purpose. An empty
    citation list is a reason this module chose not to support — dropping it
    is the documented behaviour. A citation to a NON-EXISTENT id is a bug in
    this module (or evidence handed in inconsistently) that would put a
    dangling reference in front of the user and break spec.md §9's citation
    metric, so it fails loudly instead.
    """
    known = {item.id for item in evidence}
    kept: list[ReasonItem] = []
    for reason in reasons:
        if not reason.evidence_ids:
            continue
        missing = [eid for eid in reason.evidence_ids if eid not in known]
        if missing:
            raise ValueError(
                f"reason {reason.text!r} cites unknown evidence id(s) {missing!r}; "
                "every user-facing reason must map to evidence (spec.md §9)"
            )
        kept.append(reason)
    return kept
