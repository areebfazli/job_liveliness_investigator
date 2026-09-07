"""rli.eval — the spec.md §6 baseline systems (PLAN.md M3, last bullet).

`rli.eval.system_a.run_system_a` (full probes) and
`rli.eval.system_b.run_system_b` (deterministic rules, versioned and frozen)
are the two systems this package ships; `rli.eval.report.summarize_runs`
reads their traces back. Systems C and C2 (spec.md §6) land in PLAN.md M5 and
reuse the same `rli.eval.runner` machinery, which is where the invariant that
makes any of it comparable lives: every system shares one frozen action
policy, one case-state builder and one trace format, and differs only in
which dynamic probes it chooses to run.

Nothing in this package writes to the collection corpus — see
`rli.eval.runner`'s write invariant.
"""

from rli.eval.case import CaseState, build_case_state, extend_case_state
from rli.eval.report import RunsSummary, summarize_runs
from rli.eval.runner import Run, RunResult, config_hash, decide_and_finish
from rli.eval.system_a import run_system_a
from rli.eval.system_b import B_VERSION, RouteDecision, b_rules_hash, route, run_system_b

__all__ = [
    "B_VERSION",
    "CaseState",
    "Run",
    "RunResult",
    "RouteDecision",
    "RunsSummary",
    "b_rules_hash",
    "build_case_state",
    "config_hash",
    "decide_and_finish",
    "extend_case_state",
    "route",
    "run_system_a",
    "run_system_b",
    "summarize_runs",
]
