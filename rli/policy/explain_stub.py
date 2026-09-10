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
* **Order is fixed** (state, publication, refresh, expiry, events, history,
  hiring signal, board absence) rather than ranked by importance, because a
  stable order is what makes System A/B output diffable across runs.
* **The publication reason never stands alone once a refresh exists.** A
  `refreshed_at` claim can make `publish_recency` "recent" for a posting
  first published long ago, and "The role was first published 340 days ago"
  on its own would read as a contradiction of that verdict. The refresh
  reason is emitted whenever a `refreshed_at` claim is present — not only
  when it happens to be the later of the two dates — so the two ALWAYS
  appear together, adjacently, and the pair reads correctly. That guarantee
  is what makes it safe to leave the publication wording alone.
* **Exactly ONE material-event reason is emitted**, whichever wording
  applies. The specific "dated event, and nothing has changed since" wording
  (the observation behind the policy's P3c `wait`) replaces the generic one
  rather than joining it; two reasons about one event would double-count in
  spec.md §6's citation-support metric and read as two findings.
* **The P3c wording avoids the word "unchanged"** and says "has not changed
  since" instead. `rli.eval.metrics.FAMILY_KEYWORDS` classifies reason text
  by crude substring, and "unchanged" is a `requirements`-family keyword; a
  refresh/event reason misfiled as a requirements-drift reason would be
  counted as an unsupported citation. The classifier is documented as crude
  and this module works around it rather than widening it.
* **Some inputs are cited by probe, not by claim type.** `repost_pattern`
  and `corroborating_hiring_signal` come from probes whose claim vocabulary
  is owned by `rli.probes`; citing "any evidence this probe produced" keeps
  this module correct as those probes evolve, at the cost of a slightly
  coarser citation.
* **The company-event reasons are the exception, and cite PRECISELY.** They
  used to be in the bullet above, citing every `company_events` item. That
  is no longer honest: the probe emits a claim per dated event of ANY type
  (funding, expansion, acquisition) plus a `company_events_searched`
  collection-status claim, so "a dated layoff or shutdown was recorded"
  could end up citing a funding headline and nothing else. So the
  material-event reason cites `rli.policy.inputs.material_event_claims` and
  the freeze reason cites `freeze_event_claims` — in both cases exactly the
  windowed claims that made the boolean True, selected by the very rule that
  set it (those helpers are defined against `signals_from_facts`, so they
  cannot drift from it).

  This matters beyond tidiness: the explanation layer carries a guard that
  drops a reason whose TEXT names a claim type absent from its cited ids, so
  a layoff reason citing only funding ids would be silently deleted, and the
  `wait` it explains would arrive unexplained. Each precise set falls back to
  the old broad `_by_probe` set when it comes back empty — defensive only, as
  it cannot be empty while the corresponding boolean is True.
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
    freeze_event_claims,
    last_publish_or_refresh,
    material_event_claims,
    newest_refresh_claim,
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
            reasons.append(ReasonItem(text=_STATE_TEXT[state], evidence_ids=_ids(state_evidence)))

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

    # 3. Corroborated refresh. Immediately after the publication reason, so
    #    "first published N days ago" is never read alone (module docstring).
    #    The claim only exists when an ATS `updated_at` coincided with a
    #    content change we actually observed (`rli.eval.case`), so the text
    #    states both halves — the date and the corroboration — and nothing
    #    about why the employer touched the posting.
    refresh = newest_refresh_claim(evidence)
    if refresh is not None and refresh.source_event_at is not None:
        reasons.append(
            ReasonItem(
                text=(
                    "The posting was updated on "
                    f"{refresh.source_event_at.date().isoformat()}, and our own or "
                    "archived captures show its content changed around then."
                ),
                evidence_ids=_ids([refresh]),
            )
        )

    # 4. Declared expiry (spec.md §3: publisher-declared, not proof of anything).
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

    # 5. Company events. Only stated when TRUE: "we checked and found nothing"
    #    is not a user-facing reason, and UNKNOWN certainly is not. Each
    #    reason cites the precise claims that set its boolean, not every
    #    `company_events` item — see the module docstring.
    event_evidence = _by_probe(evidence, _EVENTS_PROBE)
    if event_evidence:
        if inputs.material_negative_event is True:
            cited = material_event_claims(evidence, when) or event_evidence
            reasons.append(_material_event_reason(inputs, evidence, cited))
        if inputs.freeze_or_pause is True:
            reasons.append(
                ReasonItem(
                    text=(
                        "A dated hiring freeze or pause was recorded for this company "
                        "inside the policy's lookback window."
                    ),
                    evidence_ids=_ids(freeze_event_claims(evidence, when) or event_evidence),
                )
            )

    # 6. Repost history.
    repost_evidence = _by_probe(evidence, *_REPOST_PROBES)
    pattern = inputs.repost_pattern
    if repost_evidence and not isinstance(pattern, Unknown) and pattern != "none":
        text = (
            "The role has been reposted with unchanged content."
            if pattern == "repeated_unchanged"
            else "The role has been reposted with changed content."
        )
        reasons.append(ReasonItem(text=text, evidence_ids=_ids(repost_evidence)))

    # 7. Corroborating hiring signal.
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

    # 8. Board absence (a fact the user should see even when it contradicts
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


def _material_event_reason(
    inputs: PolicyInputs,
    evidence: Sequence[EvidenceItem],
    event_evidence: Sequence[EvidenceItem],
) -> ReasonItem:
    """The one reason emitted when `material_negative_event is True`.

    `event_evidence` is the caller's already-narrowed citation set — the
    layoff/shutdown claims that actually set the boolean
    (`rli.policy.inputs.material_event_claims`), or the whole
    `company_events` set as a defensive fallback. It is passed in rather than
    re-selected here so both wordings below cite the same thing.

    Two wordings, one reason (module docstring). The specific wording states
    the observation behind the policy's P3c `wait` branch — a dated event,
    and no sign the posting's content has moved since — and it cites BOTH
    halves of that observation in a single `ReasonItem`: the `company_events`
    evidence for the date, and the refresh/publish claim for the "since".
    Citing only the event would leave the second half of the sentence
    unsupported, which is exactly what spec.md §9 forbids.

    The "since" is recomputed here from the evidence, via the same
    `last_publish_or_refresh` the policy uses, rather than being handed in:
    this module's contract is `(inputs, evidence)` and a derived date that
    can be recomputed from the evidence does not justify widening it.

    It falls back to the generic wording whenever the specific one cannot be
    supported — no event DATE (an older `PolicyInputs`, or the "checked, none
    found" case that cannot reach here), a refresh/publish date LATER than
    the event (the posting did move afterwards, so the sentence would be
    false), or no publish/refresh evidence to cite at all. Note the third
    case is a citation problem, not a factual one: "not refreshed since" is
    still true, and the policy still fires P3c on it; there is simply no
    evidence id for the absence, and an uncited half-sentence is worse than
    a coarser true one.
    """
    generic = ReasonItem(
        text=(
            "A dated layoff or shutdown was recorded for this company inside "
            "the policy's lookback window."
        ),
        evidence_ids=_ids(event_evidence),
    )

    event_at = inputs.last_material_event_at
    if not isinstance(event_at, datetime):
        return generic

    refreshed_at = last_publish_or_refresh(evidence)
    if refreshed_at is not None and refreshed_at > event_at:
        return generic

    # The refresh claim is the better citation when it exists; the publish
    # claim is the fallback, since "first published, and never refreshed
    # since" is the same observation with a coarser source.
    cited = newest_refresh_claim(evidence) or best_publish_claim(evidence)
    if cited is None:
        return generic

    return ReasonItem(
        # "has not changed since", never "unchanged" — see the module
        # docstring's note on `rli.eval.metrics`' keyword classifier.
        text=(
            "A dated layoff or shutdown was recorded for this company on "
            f"{event_at.date().isoformat()}, and the posting's content has not "
            "changed since then."
        ),
        evidence_ids=_ids([*event_evidence, cited]),
    )


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
