"""Decision — the exact spec.md §1 user-facing output shape."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from rli.models.evidence import EvidenceItem

PostingState = Literal["open", "closed", "reposted", "unknown"]
RecommendedAction = Literal["apply_now", "quick_apply", "wait", "skip"]
EvidenceQuality = Literal["strong", "mixed", "weak"]


class ReasonItem(BaseModel):
    """One user-facing reason, mapped to the evidence that supports it."""

    text: str
    evidence_ids: list[str]


class Decision(BaseModel):
    """Final, user-facing decision (spec.md §1).

    Never exposes numeric confidence (spec.md §1: "Do not expose numeric
    confidence until it is calibrated on held-out outcome data.").
    """

    posting_state: PostingState
    recommended_action: RecommendedAction
    recheck_after_days: int | None = None
    evidence_quality: EvidenceQuality
    hypotheses: list[str] = Field(default_factory=list)
    reason: list[ReasonItem] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
