"""PolicyInputs — the action-policy inputs from spec.md §5.

Each field's type is `<real type> | Unknown`, defaulting to the `UNKNOWN`
sentinel, so "not yet investigated" (unpopulated) is a distinct value from
"investigated, and the answer is a known negative/absent" — e.g.
`declared_expiry=None` means "checked; the publisher declared no expiry",
while `declared_expiry=UNKNOWN` means "not checked". Likewise
`posting_state="unknown"` is a *known* answer (the observed state is
indeterminate) and is not "unpopulated"; `material_negative_event=False`
means "checked, none found", not "unchecked".

Per spec.md §4: "An unresolved question is a policy input (§5) that is
still unpopulated." The controller computes the set of unpopulated inputs
via `unpopulated()`.

`evidence_quality` is deliberately NOT a field on this model even though
spec.md §5's prose lists it among the action policy's inputs. Per spec.md
§1, `evidence_quality` is a deterministic *derived output* of the evidence
set, not an unresolved question that a probe can populate — including it
here would let the controller believe some probe could "resolve" it and
would leak it into `unpopulated()`. The action policy is instead handed
`evidence_quality` alongside `PolicyInputs`, computed separately from the
evidence list.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, field_validator

from rli.models.time import ensure_aware

PostingState = Literal["open", "closed", "reposted", "unknown"]

# PLACEHOLDER (M0 judgment call): categorical bucket for how recent the
# posting's primary publish evidence is, derived from the resolver's
# `first_published`/`datePosted` evidence vs. `thresholds.recent_publish_days`.
PublishRecency = Literal["recent", "not_recent"]

# PLACEHOLDER (M0 judgment call): categorical summary of repost/version
# history, derived from `repost_history` + `requirements_drift`. spec.md §5
# policy branch references "repeated unchanged repost + long-lived history".
RepostPattern = Literal["repeated_unchanged", "changed", "none"]


class Unknown(Enum):
    """Sentinel: this policy input has not been populated yet."""

    UNKNOWN = "__unknown__"


UNKNOWN = Unknown.UNKNOWN


class PolicyInputs(BaseModel):
    """Inputs to the fixed v1 action policy (spec.md §5)."""

    posting_state: PostingState | Unknown = UNKNOWN
    publish_recency: PublishRecency | Unknown = UNKNOWN
    material_negative_event: bool | Unknown = UNKNOWN
    freeze_or_pause: bool | Unknown = UNKNOWN
    # PLACEHOLDER (M0 judgment call): the declared expiry timestamp itself
    # (JSON-LD `validThrough`), not merely whether one exists — this also
    # feeds `recheck_after_days = min(14, days_until_validThrough)` (§1).
    # `None` means "checked; publisher declared no expiry"; `UNKNOWN` means
    # "not checked".
    declared_expiry: datetime | None | Unknown = UNKNOWN
    repost_pattern: RepostPattern | Unknown = UNKNOWN
    corroborating_hiring_signal: bool | Unknown = UNKNOWN

    @field_validator("declared_expiry")
    @classmethod
    def _declared_expiry_tz_aware_utc(
        cls, value: datetime | None | Unknown
    ) -> datetime | None | Unknown:
        if value is None or isinstance(value, Unknown):
            return value
        return ensure_aware(value, "declared_expiry")

    def unpopulated(self) -> set[str]:
        """Return the set of field names still at the `UNKNOWN` sentinel."""
        return {
            name
            for name in type(self).model_fields
            if isinstance(getattr(self, name), Unknown)
        }
