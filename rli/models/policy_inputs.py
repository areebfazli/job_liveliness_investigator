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

from pydantic import BaseModel, ValidationInfo, field_validator

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
    # The date of the most recent MATERIAL negative event that is already
    # INSIDE `thresholds.negative_event_window_days`. spec.md §5's Amendment
    # 2026-09-10 adds the row "open + material negative event after last
    # refresh -> wait", which needs the event's DATE, not merely the boolean.
    #
    # The window is applied UPSTREAM, by
    # `rli.events.policy_signals.signals_from_facts` — the one rule both the
    # `company_events` probe and `rli.policy.inputs`' claims adapter run —
    # before this value is ever set. That is why `rli.policy.action._branch`
    # does not re-check it,
    # and must not: `_branch` is deliberately clock-free so the precedence
    # table is testable without a clock, and a window is a statement about
    # `now`. The consequence is that `negative_event_window_days` changes
    # decisions from outside the policy function, which is why it is part of
    # `rli.policy.action.PolicyThresholds.fingerprint()`.
    #
    # INVARIANT (guaranteed by `signals_from_facts`, which produces both
    # values from the same filtered event list): once populated,
    # `material_negative_event is True` <=> `last_material_event_at` is a
    # `datetime`. `False` pairs with `None` ("checked; no qualifying event"),
    # and UNKNOWN pairs with UNKNOWN ("not checked").
    #
    # Dates are DAY-granular: `company_events` stores a calendar date and
    # this is midnight UTC on it (`rli.events.store`'s own convention), so a
    # refresh later on the same calendar day counts as "after the event".
    last_material_event_at: datetime | None | Unknown = UNKNOWN

    @field_validator("declared_expiry", "last_material_event_at")
    @classmethod
    def _tz_aware_utc(
        cls, value: datetime | None | Unknown, info: ValidationInfo
    ) -> datetime | None | Unknown:
        """Both datetime fields live on the `available_at <= T` timeline.

        One validator rather than two siblings: the rule is identical (a
        naive datetime cannot be placed on the point-in-time replay timeline
        of spec.md §3/§6) and `info.field_name` keeps the error message
        specific, so nothing is lost by sharing it.
        """
        if value is None or isinstance(value, Unknown):
            return value
        return ensure_aware(value, info.field_name or "datetime field")

    def unpopulated(self) -> set[str]:
        """Return the set of field names still at the `UNKNOWN` sentinel."""
        return {
            name for name in type(self).model_fields if isinstance(getattr(self, name), Unknown)
        }
