"""CaseFile — the deterministic input handed to the LLM investigator (spec.md §2)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import PolicyInputs


class CaseFile(BaseModel):
    """Posting identity + accumulated evidence + current policy-input state.

    Produced by the resolver/board-snapshot step and grown by the agent loop
    as probes append evidence (spec.md §2).
    """

    posting_id: str
    company_id: str
    canonical_url: str
    title: str | None = None
    team: str | None = None
    location: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    policy_inputs: PolicyInputs = Field(default_factory=PolicyInputs)
