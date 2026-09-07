"""The frozen v1 decision layer (spec.md §5; PLAN.md M3).

Everything a system needs to turn accumulated evidence into the spec.md §1
output, with no LLM involved and no non-determinism:

* `rli.policy.inputs` — evidence/history/events -> `PolicyInputs`, plus the
  controller's `unpopulated` / `could_change_action` stop rule (spec.md §4).
* `rli.policy.quality` — the deterministic `evidence_quality` verdict
  (spec.md §1).
* `rli.policy.action` — the §5 action policy with an explicit precedence
  order, `recheck_after_days`, and `policy_version()`.
* `rli.policy.explain_stub` — evidence-cited `reason` items without an LLM,
  so Systems A and B can emit the full §1 shape (spec.md §6).
* `rli.policy.splits` — the temporal and company holdouts policy tuning must
  respect (spec.md §6).

spec.md §2's responsibility split puts all of this on the "code" side of the
line: "Never rely on the LLM alone for budgets, probabilities, stopping,
permissions, or the final action."
"""

from rli.policy.action import (
    PRECEDENCE,
    PolicyOutcome,
    PolicyThresholds,
    decide,
    policy_version,
)
from rli.policy.explain_stub import reasons_from_inputs
from rli.policy.inputs import (
    CLAIM_BOARD_ABSENT,
    CLAIM_BOARD_LISTING,
    CLAIM_DECLARED_EXPIRY,
    CLAIM_FIRST_PUBLISHED,
    CLAIM_POSTING_STATE,
    CLAIM_TEAM_SIGNAL,
    could_change_action,
    derive_policy_inputs,
    unpopulated,
)
from rli.policy.quality import (
    Contradiction,
    QualityVerdict,
    evidence_quality,
    evidence_quality_detail,
    find_contradictions,
)
from rli.policy.splits import (
    SPLITS_COLUMNS,
    SplitAssignment,
    SplitRow,
    assign_splits,
    company_split,
    temporal_split,
    write_splits_csv,
)

__all__ = [
    "CLAIM_BOARD_ABSENT",
    "CLAIM_BOARD_LISTING",
    "CLAIM_DECLARED_EXPIRY",
    "CLAIM_FIRST_PUBLISHED",
    "CLAIM_POSTING_STATE",
    "CLAIM_TEAM_SIGNAL",
    "PRECEDENCE",
    "SPLITS_COLUMNS",
    "Contradiction",
    "PolicyOutcome",
    "PolicyThresholds",
    "QualityVerdict",
    "SplitAssignment",
    "SplitRow",
    "assign_splits",
    "company_split",
    "could_change_action",
    "decide",
    "derive_policy_inputs",
    "evidence_quality",
    "evidence_quality_detail",
    "find_contradictions",
    "policy_version",
    "reasons_from_inputs",
    "temporal_split",
    "unpopulated",
    "write_splits_csv",
]
