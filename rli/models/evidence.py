"""EvidenceItem — spec.md §3."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from rli.models.time import ensure_aware

SourceQuality = Literal["ats_native", "page_structured", "archive", "news", "enrichment"]


class EvidenceItem(BaseModel):
    """A single, source-linked, timestamped fact (spec.md §3).

    `available_at` is the earliest time this evidence is verifiably available
    to the system; replay at historical time T may only expose evidence with
    `available_at <= T`. For archived evidence, `available_at` is the capture
    time — do not backdate current discoveries to the underlying event time.

    `run_id` scopes run-local evidence ids like `"e1"`: such ids are only
    unique within a single agent-loop run, so the DB primary key is the pair
    `(run_id, id)`, not `id` alone.
    """

    model_config = ConfigDict(frozen=False)

    id: str
    run_id: str | None = None
    probe: str
    claim_type: str
    value: str
    source_url: str
    raw_excerpt: str | None = None
    source_quality: SourceQuality
    source_event_at: datetime | None = None
    available_at: datetime
    fetched_at: datetime

    @field_validator("source_event_at", "available_at", "fetched_at")
    @classmethod
    def _tz_aware_utc(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        if value is None:
            return None
        return ensure_aware(value, info.field_name)
