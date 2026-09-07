"""rli.probes — the probe base class, the always-run probes, and the dynamic ones.

* `Probe` / `ProbeContext` / `ProbeClaim` — the shared probe contract.
* `resolve_posting` / `ResolvePostingProbe` — ATS + JSON-LD identity probe.
* `board_snapshot` / `BoardSnapshotProbe` — current open-jobs probe.
* `repost_history`, `requirements_drift`, `company_events`, `team_signal` —
  the four spec.md §4 dynamic probes.
* `rli.probes.registry` — deterministic eligibility filtering and cost-aware
  ranking over those four (spec.md §4 agent-loop steps 4-5).
* `rli.probes.lookups` — read-only lookups shared by the history-gated probes.
* `rli.probes.persist` — the caller-side persistence functions probes
  themselves never call (probes are pure; see `rli.probes.base`).
"""

from rli.probes.base import CostTier, Probe, ProbeClaim, ProbeContext
from rli.probes.board_snapshot import BoardJob, BoardSnapshotArgs, BoardSnapshotProbe
from rli.probes.company_events import CompanyEventsArgs, CompanyEventsProbe, company_events
from rli.probes.lookups import has_usable_history, posting_row
from rli.probes.registry import (
    DYNAMIC_PROBES,
    build_args,
    cost_value,
    eligible_probes,
    latency_estimate_s,
)
from rli.probes.repost_history import RepostHistoryArgs, RepostHistoryProbe, repost_history
from rli.probes.requirements_drift import (
    RequirementsDriftArgs,
    RequirementsDriftProbe,
    requirements_drift,
)
from rli.probes.resolve_posting import ResolvePostingArgs, ResolvePostingProbe
from rli.probes.team_signal import (
    NullTeamSignalSource,
    TeamSignalArgs,
    TeamSignalProbe,
    TeamSignalSource,
    team_signal,
)

__all__ = [
    "DYNAMIC_PROBES",
    "BoardJob",
    "BoardSnapshotArgs",
    "BoardSnapshotProbe",
    "CompanyEventsArgs",
    "CompanyEventsProbe",
    "CostTier",
    "NullTeamSignalSource",
    "Probe",
    "ProbeClaim",
    "ProbeContext",
    "RepostHistoryArgs",
    "RepostHistoryProbe",
    "RequirementsDriftArgs",
    "RequirementsDriftProbe",
    "ResolvePostingArgs",
    "ResolvePostingProbe",
    "TeamSignalArgs",
    "TeamSignalProbe",
    "TeamSignalSource",
    "build_args",
    "company_events",
    "cost_value",
    "eligible_probes",
    "has_usable_history",
    "latency_estimate_s",
    "posting_row",
    "repost_history",
    "requirements_drift",
    "team_signal",
]
