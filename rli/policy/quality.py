"""Deterministic `evidence_quality` (spec.md §1).

spec.md §1 defines the term but not the procedure:

```text
strong: primary/structured evidence supports the action and no material
        contradiction remains
mixed:  material evidence conflicts
weak:   key evidence is missing, archive-only, or affected by failures
```

This module makes that decidable. It is deliberately a pure function of
`(evidence, inputs, failures)` — no clock, no network, no database — so that
System A, System B and System C (spec.md §6) cannot disagree about it and so
replay reproduces it exactly.

--------------------------------------------------------------------------
Rules, first match wins
--------------------------------------------------------------------------

Read as a chain of `if ... return`, in this order:

* **Q1 `probe_failure`** — any `ProbeResult` in `failures` has `ok=False`
  -> `weak`
* **Q2 `no_primary_publish_evidence`** — no `first_published` claim with a
  parsed date and a `source_quality` in
  `rli.policy.inputs.PRIMARY_PUBLISH_QUALITIES` -> `weak`
* **Q3 `posting_state_unknown`** — `posting_state` is UNKNOWN or the literal
  `"unknown"` -> `weak`
* **Q4 `contradiction`** — any material contradiction (defined below)
  -> `mixed`
* **Q5 `strong`** — otherwise -> `strong`

**Why the two `weak` rules outrank `mixed`.** A case can be both missing key
evidence and self-contradictory. `weak` is the more pessimistic verdict and
both route to the same action today (`quick_apply`, branch P6), so the
ordering only affects what the user is told — and understating how thin the
evidence is, is the costlier mistake. It also keeps the rule that `apply_now`
(which requires `strong`) can never be reached through a failure.

**Q2 subsumes "archive-only".** spec.md §1 names archive-only evidence as
`weak` directly; since `archive` is not one of the primary source qualities
(`rli.policy.inputs.PRIMARY_PUBLISH_QUALITIES`, from spec.md §3's source
ranking), a case whose only publish date came from Wayback fails Q2 and is
`weak` without needing a separate rule.

**Q3 is not redundant with the action policy.** `rli.policy.action` already
routes an unresolved `posting_state` to `wait`, but `evidence_quality` is
reported to the user independently of the action (spec.md §1's output shape),
and "we could not establish whether this role is still open" is exactly the
"key evidence is missing" case.

**`failures`.** `ProbeResult` carries no probe name, so this function cannot
tell an always-run probe from a dynamic one; the caller passes the results it
wants to count, which per spec.md §4 means the always-run pair
(`resolve_posting`, `board_snapshot`). Entries with `ok=True` are ignored, so
callers may simply pass every always-run result. Passing dynamic-probe
failures too is a caller's choice, and is *not* the default reading: a
`team_signal` outage should not make a well-evidenced posting weak.

--------------------------------------------------------------------------
Material contradiction (the definition spec.md §1 leaves open)
--------------------------------------------------------------------------

**C1 — conflicting publish dates.** Group every dated `first_published` claim
by `(source_quality, source_url)`, take each group's earliest date, and flag a
contradiction when two groups differ by more than `policy.contradiction_days`.

* *Grouping by source, not by claim*: re-fetching the same URL twice must not
  contradict itself, and a source that legitimately reports the same date
  repeatedly must not accumulate "evidence" of conflict.
* *Threshold, not exact equality*: `ats_native` and `page_structured` dates
  routinely differ by a day (timezone handling, render lag). That is not a
  conflict; `contradiction_days` is where the project draws the line and it
  is a frozen, documented threshold per spec.md §5.
* *Archive claims are included.* An archived `datePosted` disagreeing with the
  ATS by a month is a real conflict about the posting's history, and the user
  is better served by `mixed` than by a confident `strong`.
* *Within-source drift over time is NOT a contradiction* — a publisher that
  edits its own `datePosted` between two of our fetches is a versioning
  event (spec.md §4's `requirements_drift`), not two sources disagreeing.

**C2 — open, but missing from a fresher board snapshot.** The newest
`posting_state` claim says `"open"` while a `board_absent` claim (the posting's
job id was not in a board capture) has an equal or later `available_at` (equal
covers the same-run case where both always-run probes share one `now`). Order
is by `available_at`, the timestamp spec.md §3 defines the replay window on,
so the comparison means the same thing live and in replay. The reverse order
is deliberately *not* a contradiction: an absence followed by a fresher
"open" is a posting that came back, which is history, not conflict.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from rli.models.decision import EvidenceQuality
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import PolicyInputs, Unknown
from rli.models.probe import ProbeResult
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_FIRST_PUBLISHED,
    CLAIM_POSTING_STATE,
    PRIMARY_PUBLISH_QUALITIES,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.config import Config
    from rli.policy.action import PolicyThresholds

__all__ = [
    "Contradiction",
    "QualityVerdict",
    "evidence_quality",
    "evidence_quality_detail",
    "find_contradictions",
]

QualityRule = Literal[
    "probe_failure",
    "no_primary_publish_evidence",
    "posting_state_unknown",
    "contradiction",
    "strong",
]


class Contradiction(BaseModel):
    """One material contradiction, with the evidence that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["publish_date_conflict", "open_but_absent_from_board"]
    detail: str
    evidence_ids: tuple[str, ...]


class QualityVerdict(BaseModel):
    """`evidence_quality` plus why — for the run trace and the explanation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quality: EvidenceQuality
    rule: QualityRule
    detail: str
    contradictions: tuple[Contradiction, ...] = ()


def _event_at(item: EvidenceItem) -> datetime:
    """`source_event_at` of an item already filtered to have one.

    A plain attribute read would be `datetime | None` at every call site;
    this keeps the sort keys and arithmetic below total without an `assert`
    (which `python -O` strips) or a `# type: ignore`.
    """
    if item.source_event_at is None:  # pragma: no cover - filtered upstream
        raise ValueError(f"evidence {item.id!r} has no source_event_at")
    return item.source_event_at


def _newest(items: Sequence[EvidenceItem]) -> EvidenceItem | None:
    if not items:
        return None
    return max(items, key=lambda e: (e.available_at, e.fetched_at, e.id))


def _primary_publish_claims(evidence: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    return [
        item
        for item in evidence
        if item.claim_type == CLAIM_FIRST_PUBLISHED
        and item.source_event_at is not None
        and item.source_quality in PRIMARY_PUBLISH_QUALITIES
    ]


def find_contradictions(
    evidence: Sequence[EvidenceItem],
    cfg: Config | PolicyThresholds | None = None,
) -> list[Contradiction]:
    """Every material contradiction in `evidence` (C1 and C2 in the docstring)."""
    from rli.policy.action import PolicyThresholds as _PolicyThresholds

    thresholds = _PolicyThresholds.coerce(cfg)
    found: list[Contradiction] = []

    # C1 — conflicting first_published dates across distinct sources.
    by_source: dict[tuple[str, str], list[EvidenceItem]] = {}
    for item in evidence:
        if item.claim_type == CLAIM_FIRST_PUBLISHED and item.source_event_at is not None:
            by_source.setdefault((item.source_quality, item.source_url), []).append(item)

    if len(by_source) > 1:
        # One representative per source: its earliest claimed publication (see
        # `rli.policy.inputs` — "first published" means the earliest).
        representatives = [
            min(items, key=lambda e: (_event_at(e), e.id)) for items in by_source.values()
        ]
        earliest = min(representatives, key=lambda e: (_event_at(e), e.id))
        latest = max(representatives, key=lambda e: (_event_at(e), e.id))
        spread = _event_at(latest) - _event_at(earliest)
        if spread > timedelta(days=thresholds.contradiction_days):
            found.append(
                Contradiction(
                    kind="publish_date_conflict",
                    detail=(
                        f"{earliest.source_quality} says {_event_at(earliest).date()} "
                        f"but {latest.source_quality} says {_event_at(latest).date()} "
                        f"({spread.days} days apart, over the "
                        f"{thresholds.contradiction_days}-day threshold)"
                    ),
                    evidence_ids=tuple(sorted({earliest.id, latest.id})),
                )
            )

    # C2 — resolver says open, a fresher board capture lacks the job.
    state_claim = _newest([e for e in evidence if e.claim_type == CLAIM_POSTING_STATE])
    if state_claim is not None and state_claim.value.strip().lower() == "open":
        fresher_absences = [
            e
            for e in evidence
            if e.claim_type == CLAIM_BOARD_ABSENT and e.available_at >= state_claim.available_at
        ]
        absence = _newest(fresher_absences)
        if absence is not None:
            found.append(
                Contradiction(
                    kind="open_but_absent_from_board",
                    detail=(
                        "the resolver reported the posting open at "
                        f"{state_claim.available_at.isoformat()} but a board snapshot at "
                        f"{absence.available_at.isoformat()} did not list it"
                    ),
                    evidence_ids=tuple(sorted({state_claim.id, absence.id})),
                )
            )

    return found


def evidence_quality_detail(
    evidence: Sequence[EvidenceItem],
    inputs: PolicyInputs,
    failures: Sequence[ProbeResult] = (),
    cfg: Config | PolicyThresholds | None = None,
) -> QualityVerdict:
    """`evidence_quality` with the rule that decided it and any contradictions."""
    contradictions = tuple(find_contradictions(evidence, cfg))

    # Q1 — failures.
    failed = [result for result in failures if not result.ok]
    if failed:
        errors = ", ".join(sorted({result.error or "unspecified error" for result in failed}))
        return QualityVerdict(
            quality="weak",
            rule="probe_failure",
            detail=f"{len(failed)} always-run probe failure(s): {errors}",
            contradictions=contradictions,
        )

    # Q2 — no primary publish evidence (covers the archive-only case).
    if not _primary_publish_claims(evidence):
        archive_only = any(
            item.claim_type == CLAIM_FIRST_PUBLISHED and item.source_quality == "archive"
            for item in evidence
        )
        return QualityVerdict(
            quality="weak",
            rule="no_primary_publish_evidence",
            detail=(
                "the only publish evidence is archive-derived"
                if archive_only
                else "no ats_native or page_structured publish date was established"
            ),
            contradictions=contradictions,
        )

    # Q3 — the observed state was never established.
    state = inputs.posting_state
    if isinstance(state, Unknown) or state == "unknown":
        return QualityVerdict(
            quality="weak",
            rule="posting_state_unknown",
            detail="the posting's current state could not be established",
            contradictions=contradictions,
        )

    # Q4 — material contradiction.
    if contradictions:
        return QualityVerdict(
            quality="mixed",
            rule="contradiction",
            detail="; ".join(c.detail for c in contradictions),
            contradictions=contradictions,
        )

    # Q5 — primary publish evidence, a known state, nothing conflicting.
    return QualityVerdict(
        quality="strong",
        rule="strong",
        detail=(
            f"primary publish evidence and an observed state of {state!r} "
            "with no material contradiction"
        ),
        contradictions=(),
    )


def evidence_quality(
    evidence: Sequence[EvidenceItem],
    inputs: PolicyInputs,
    failures: Sequence[ProbeResult] = (),
    cfg: Config | PolicyThresholds | None = None,
) -> EvidenceQuality:
    """Deterministic `strong` / `mixed` / `weak` verdict (spec.md §1).

    See the module docstring for the rule order and the definition of a
    material contradiction. Use `evidence_quality_detail` when the reason
    matters (run trace, explanation).
    """
    return evidence_quality_detail(evidence, inputs, failures, cfg).quality
