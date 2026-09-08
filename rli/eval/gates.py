"""spec.md §6 pass/fail gates over the M6 metrics (PLAN.md M6).

`rli.eval.metrics` measures; this module judges. It contains the two
verdicts spec.md §6 defines and nothing else — no measurement of its own, so
a gate can never disagree with the report printed next to it.

    Agent gate (spec.md §6)
        System C is kept only if, on the same replay cases, it uses at most
        70% of System B's medium/high-cost probe budget while staying within
        2 percentage points of System B's action agreement with System A —
        BOTH overall and macro-averaged.

    Product gate (spec.md §6)
        The product claim ("this saves wasted applications") is only proven
        on held-out postings with recorded outcomes: applications per screen
        and per interview must be lower for postings the system recommended
        applying to than for the rest.

--------------------------------------------------------------------------
Gate arithmetic, exactly
--------------------------------------------------------------------------

    probe_use_pass := candidate_mh_per_run
                        <= probe_ratio_max * baseline_mh_per_run + GATE_TOLERANCE
    overall_pass   := candidate_overall
                        >= baseline_overall - agreement_margin - GATE_TOLERANCE
    macro_pass     := candidate_macro
                        >= baseline_macro   - agreement_margin - GATE_TOLERANCE
    passed         := probe_use_pass and overall_pass and macro_pass

`GATE_TOLERANCE` exists because these are float comparisons against numbers
produced by division. A candidate at exactly 70% of the baseline's probe use,
or exactly 2 percentage points below its agreement, MUST pass — the spec says
"at most 70%" and "within 2 points" — and without the slack a value like
`0.7 * 0.3` landing on `0.21000000000000002` would fail a gate the spec says
passes. The tolerance is 1e-9: far below any difference that could matter on
a corpus of a few hundred cases, and far above float noise.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **Per-run rates, never raw totals.** The candidate and the baseline can
  evaluate different numbers of cases (a system that failed to start on some
  cases has fewer runs). Comparing `medium_high_probe_steps` directly would
  let a system pass the gate by crashing more often. The gate compares
  `medium_high_probe_steps / runs` on both sides, and reports both the raw
  counts and the run counts so the reader can see the denominators.

* **Both overall AND macro agreement must hold.** They are not redundant:
  overall agreement on a `quick_apply`-heavy corpus is dominated by the
  majority class, and a candidate that collapses onto that class loses almost
  nothing on overall while losing everything on the minority classes. macro
  agreement — the unweighted mean of the per-class rates, with the classes
  taken from System A — is the figure that catches exactly that, which is
  why spec.md §6 asks for both and why `and` here is not `or`.

* **A gate that cannot be evidenced does not pass.** When the candidate has
  no scoped runs at all, the result is `status="not_run"` with
  `passed=None` and every sub-check `None` — not `False`, because nothing was
  measured and "System C failed the gate" is a materially different claim
  from "System C was never run". This is the state on the real database
  today: there is no `ANTHROPIC_API_KEY`, so System C has no runs. But once
  the candidate HAS runs and some comparison input is missing (the baseline
  never ran, or produced no paired cases so its agreement is undefined), the
  affected sub-check is `False` with an explanatory note: at that point a
  claim was made and could not be substantiated, and a gate whose job is to
  stop an unproven system from shipping must resolve an unsubstantiated
  claim against the candidate.

* **Zero baseline probe use is handled explicitly, not by dividing.**
  `probe_ratio = candidate_rate / baseline_rate` is `None` when the baseline
  rate is 0 (an undefined ratio is never printed as `inf` or `0.0`), but the
  PASS/FAIL arithmetic above never divides: `candidate <= 0.7 * 0 + tol` is
  `True` for a candidate that also used none, and `False` for one that used
  any. That is the correct reading of "at most 70% of B" when B used nothing.

* **`notes` carries the System A structural caveat verbatim.** Every
  probe-count figure in the report is measured against a System A that
  neuters spec.md §4's unresolved-question gate (see
  `rli.eval.metrics.SYSTEM_A_CAVEAT`). The agent gate's own probe ratio is
  measured against B precisely because of this, but its agreement figures are
  still agreement *with A*, so the caveat travels with the verdict. `notes`
  also spells out the exact numbers each sub-check compared, so a reader
  never has to recompute the verdict to trust it, and flags when the held-out
  `test` split was in scope.

* **The product gate's default answer is `"unproven"`, and it is very hard
  to move off it.** `"pass"` requires held-out postings, at least
  `min_outcomes` recorded outcomes, and both effort ratios defined and
  strictly better. `"fail"` is reserved for the case where the data IS
  sufficient and the result is not better — so `"fail"` is a real negative
  finding and `"unproven"` is the honest answer to "we have no outcome data".
  On the current database `outcomes` is empty, so the honest answer is
  `"unproven"` and the `reason` says which of the requirements is missing.
  Nothing here ever returns `"pass"` on zero rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.metrics import (
    DEFAULT_SPLITS,
    SYSTEM_A_CAVEAT,
    EfficiencyMetrics,
    MetricsCaseSet,
    agent_efficiency,
    collect_system_runs,
)

__all__ = [
    "AGENT_GATE_AGREEMENT_MARGIN",
    "AGENT_GATE_PROBE_RATIO",
    "GATE_TOLERANCE",
    "ActionOutcomeStats",
    "AgentGateResult",
    "ProductGateResult",
    "agent_gate",
    "product_gate",
]

#: spec.md §6: "C is kept only if it uses <= 70% of B's medium/high-cost probe budget".
AGENT_GATE_PROBE_RATIO: float = 0.70

#: spec.md §6: "...within 2 percentage points of B's agreement with A".
AGENT_GATE_AGREEMENT_MARGIN: float = 0.02

#: Float slack so an EXACT 70% ratio and an EXACT -2pp agreement drop PASS.
#: See the module docstring — this is a correctness requirement, not a fudge.
GATE_TOLERANCE: float = 1e-9

#: `outcomes.outcome_type`'s CHECK constraint, in report order.
_OUTCOME_TYPES: tuple[str, ...] = (
    "applied",
    "reply",
    "screen",
    "interview",
    "offer",
    "rejection",
    "silence",
)

_SQL_CHUNK = 400


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _verdict(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "PASS" if value else "FAIL"


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _chunks(values: Sequence[str], size: int = _SQL_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]


# ---------------------------------------------------------------------------
# 1. Agent gate (spec.md §6)
# ---------------------------------------------------------------------------


class AgentGateResult(BaseModel):
    """The spec.md §6 agent gate verdict, with every input it used.

    The model carries both sides of all three comparisons plus the thresholds
    that were applied, so the verdict is reproducible by hand from the
    result alone. `passed` is `None` exactly when `status == "not_run"`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    allowed_splits: tuple[str, ...]
    candidate: str = "C"
    baseline: str = "B"
    reference: str = "A"

    status: Literal["pass", "fail", "not_run"] = "not_run"
    passed: bool | None = None

    candidate_runs: int = 0
    baseline_runs: int = 0
    candidate_paired_cases: int = 0
    baseline_paired_cases: int = 0

    candidate_medium_high_steps: int = 0
    baseline_medium_high_steps: int = 0
    candidate_medium_high_per_run: float | None = None
    baseline_medium_high_per_run: float | None = None
    probe_ratio: float | None = None
    probe_ratio_threshold: float = AGENT_GATE_PROBE_RATIO
    probe_use_pass: bool | None = None

    candidate_overall_agreement: float | None = None
    baseline_overall_agreement: float | None = None
    overall_required: float | None = None
    overall_pass: bool | None = None

    candidate_macro_agreement: float | None = None
    baseline_macro_agreement: float | None = None
    macro_required: float | None = None
    macro_pass: bool | None = None
    agreement_margin: float = AGENT_GATE_AGREEMENT_MARGIN

    candidate_action_distribution: dict[str, int] = {}
    baseline_action_distribution: dict[str, int] = {}
    reference_action_distribution: dict[str, int] = {}

    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        headline = {
            "pass": f"PASS — {self.candidate} is kept",
            "fail": f"FAIL — {self.candidate} is not kept",
            "not_run": f"NOT RUN — {self.candidate} has no runs in scope; nothing was measured",
        }[self.status]
        lines = [
            f"agent gate (spec.md §6): {headline}",
            f"  dataset={self.dataset_id!r} "
            f"splits=[{', '.join(self.allowed_splits) or 'none'}] "
            f"candidate={self.candidate} baseline={self.baseline} "
            f"reference={self.reference}",
            f"  runs: {self.candidate}={self.candidate_runs} "
            f"{self.baseline}={self.baseline_runs}; paired cases vs {self.reference}: "
            f"{self.candidate}={self.candidate_paired_cases} "
            f"{self.baseline}={self.baseline_paired_cases}",
            f"  probe use [{_verdict(self.probe_use_pass)}]: "
            f"{self.candidate}={_num(self.candidate_medium_high_per_run)} vs "
            f"{self.baseline}={_num(self.baseline_medium_high_per_run)} "
            "medium/high probe steps per run "
            f"(ratio {_num(self.probe_ratio)}, allowed <= {self.probe_ratio_threshold:.2f})",
            f"  overall agreement with {self.reference} [{_verdict(self.overall_pass)}]: "
            f"{self.candidate}={_pct(self.candidate_overall_agreement)} vs "
            f"{self.baseline}={_pct(self.baseline_overall_agreement)} "
            f"(required >= {_pct(self.overall_required)})",
            f"  macro agreement with {self.reference} [{_verdict(self.macro_pass)}]: "
            f"{self.candidate}={_pct(self.candidate_macro_agreement)} vs "
            f"{self.baseline}={_pct(self.baseline_macro_agreement)} "
            f"(required >= {_pct(self.macro_required)})",
            f"  actions ({self.candidate}): {self.candidate_action_distribution or '(none)'}",
            f"  actions ({self.baseline}): {self.baseline_action_distribution or '(none)'}",
            f"  actions ({self.reference}): {self.reference_action_distribution or '(none)'}",
        ]
        for note in self.notes:
            lines.append(f"  NOTE: {note}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def agent_gate(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    splits: Mapping[str, str],
    candidate: str = "C",
    baseline: str = "B",
    reference: str = "A",
    allowed_splits: tuple[str, ...] = DEFAULT_SPLITS,
    probe_ratio_max: float = AGENT_GATE_PROBE_RATIO,
    agreement_margin: float = AGENT_GATE_AGREEMENT_MARGIN,
    candidate_metrics: EfficiencyMetrics | None = None,
    baseline_metrics: EfficiencyMetrics | None = None,
    case_set: MetricsCaseSet | None = None,
) -> AgentGateResult:
    """Evaluate spec.md §6's agent gate for `candidate` against `baseline`.

    Both systems are measured against the same `reference` and the same case
    set, so the two agreement figures are comparable by construction. Pass
    `candidate_metrics` / `baseline_metrics` when the caller has already
    computed them (`rli.eval.evaluate` does) to avoid re-querying; they are
    recomputed otherwise. See the module docstring for the arithmetic and for
    why an unevidenced sub-check resolves against the candidate.
    """
    systems = tuple(dict.fromkeys((candidate, baseline, reference)))
    resolved_case_set = case_set
    if resolved_case_set is None or not all(name in resolved_case_set.systems for name in systems):
        resolved_case_set = collect_system_runs(
            conn,
            dataset_id=dataset_id,
            splits=splits,
            systems=systems,
            allowed_splits=allowed_splits,
        )

    if candidate_metrics is None:
        candidate_metrics = agent_efficiency(
            conn,
            cfg,
            dataset_id=dataset_id,
            splits=splits,
            system=candidate,
            reference=reference,
            allowed_splits=allowed_splits,
            case_set=resolved_case_set,
        )
    if baseline_metrics is None:
        baseline_metrics = agent_efficiency(
            conn,
            cfg,
            dataset_id=dataset_id,
            splits=splits,
            system=baseline,
            reference=reference,
            allowed_splits=allowed_splits,
            case_set=resolved_case_set,
        )

    notes: list[str] = [SYSTEM_A_CAVEAT]
    if "test" in tuple(allowed_splits):
        notes.append(
            "the held-out 'test' split is IN SCOPE for this gate. spec.md §6 permits this once, "
            "for the final evaluation; any tuning decision made after reading it invalidates the "
            "holdout."
        )

    candidate_rate = candidate_metrics.mean_medium_high_probes_per_run
    baseline_rate = baseline_metrics.mean_medium_high_probes_per_run
    probe_ratio = None
    if candidate_rate is not None and baseline_rate:
        probe_ratio = candidate_rate / baseline_rate

    base = AgentGateResult(
        dataset_id=dataset_id,
        allowed_splits=tuple(allowed_splits),
        candidate=candidate,
        baseline=baseline,
        reference=reference,
        candidate_runs=candidate_metrics.runs,
        baseline_runs=baseline_metrics.runs,
        candidate_paired_cases=candidate_metrics.paired_cases,
        baseline_paired_cases=baseline_metrics.paired_cases,
        candidate_medium_high_steps=candidate_metrics.medium_high_probe_steps,
        baseline_medium_high_steps=baseline_metrics.medium_high_probe_steps,
        candidate_medium_high_per_run=candidate_rate,
        baseline_medium_high_per_run=baseline_rate,
        probe_ratio=probe_ratio,
        probe_ratio_threshold=probe_ratio_max,
        agreement_margin=agreement_margin,
        candidate_overall_agreement=candidate_metrics.overall_agreement,
        baseline_overall_agreement=baseline_metrics.overall_agreement,
        candidate_macro_agreement=candidate_metrics.macro_agreement,
        baseline_macro_agreement=baseline_metrics.macro_agreement,
        candidate_action_distribution=dict(candidate_metrics.action_distribution),
        baseline_action_distribution=dict(baseline_metrics.action_distribution),
        reference_action_distribution=dict(candidate_metrics.reference_action_distribution)
        or dict(baseline_metrics.reference_action_distribution),
    )

    if candidate_metrics.runs == 0:
        notes.append(
            f"system {candidate} has no runs in scope for dataset {dataset_id!r} on splits "
            f"{tuple(allowed_splits)!r}, so the gate was not evaluated. On the current "
            "database this is expected for System C: there is no ANTHROPIC_API_KEY, so C was "
            "never run. `passed` is None (unknown), NOT False (failed)."
        )
        return base.model_copy(update={"status": "not_run", "passed": None, "notes": tuple(notes)})

    # --- Probe use ----------------------------------------------------------
    effective_candidate_rate = candidate_rate if candidate_rate is not None else 0.0
    effective_baseline_rate = baseline_rate if baseline_rate is not None else 0.0
    if baseline_rate is None:
        notes.append(
            f"system {baseline} has no runs in scope, so its medium/high probe rate is "
            "undefined and was treated as 0.0 for the probe-use comparison: without a baseline "
            "there is no reduction to demonstrate."
        )
    probe_threshold = probe_ratio_max * effective_baseline_rate
    probe_use_pass = effective_candidate_rate <= probe_threshold + GATE_TOLERANCE
    notes.append(
        f"probe use: {candidate}={effective_candidate_rate:.6f} medium/high probe steps per run "
        f"({candidate_metrics.medium_high_probe_steps} steps / {candidate_metrics.runs} runs) vs "
        f"{baseline}={effective_baseline_rate:.6f} "
        f"({baseline_metrics.medium_high_probe_steps} steps / {baseline_metrics.runs} runs); "
        f"allowed <= {probe_ratio_max:.2f} x {effective_baseline_rate:.6f} = "
        f"{probe_threshold:.6f} (+{GATE_TOLERANCE:g} tolerance) -> {_verdict(probe_use_pass)}"
    )

    # --- Agreement ----------------------------------------------------------
    def _agreement_check(
        label: str, candidate_value: float | None, baseline_value: float | None
    ) -> tuple[float | None, bool]:
        if baseline_value is None or candidate_value is None:
            notes.append(
                f"{label} agreement could not be compared "
                f"({candidate}={_pct(candidate_value)}, {baseline}={_pct(baseline_value)}); "
                "an unevidenced gate does not pass, so this sub-check is recorded as FAIL."
            )
            return None, False
        required = baseline_value - agreement_margin
        passed = candidate_value >= required - GATE_TOLERANCE
        notes.append(
            f"{label} agreement with {reference}: {candidate}={candidate_value:.6f} vs "
            f"{baseline}={baseline_value:.6f}; required >= {baseline_value:.6f} - "
            f"{agreement_margin:.2f} = {required:.6f} (-{GATE_TOLERANCE:g} tolerance) -> "
            f"{_verdict(passed)}"
        )
        return required, passed

    overall_required, overall_pass = _agreement_check(
        "overall", candidate_metrics.overall_agreement, baseline_metrics.overall_agreement
    )
    macro_required, macro_pass = _agreement_check(
        "macro", candidate_metrics.macro_agreement, baseline_metrics.macro_agreement
    )

    passed = bool(probe_use_pass and overall_pass and macro_pass)
    return base.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "passed": passed,
            "probe_use_pass": probe_use_pass,
            "overall_required": overall_required,
            "overall_pass": overall_pass,
            "macro_required": macro_required,
            "macro_pass": macro_pass,
            "notes": tuple(notes),
        }
    )


# ---------------------------------------------------------------------------
# 2. Product gate (spec.md §6)
# ---------------------------------------------------------------------------


class ActionOutcomeStats(BaseModel):
    """Recorded outcomes for the postings the system gave one recommended action.

    `effort_per_screen` / `effort_per_interview` are `applied / screen` and
    `applied / interview` — "how many applications did it take to get one",
    so LOWER IS BETTER. Both are `None` when the denominator is zero, which
    is the common case on a small corpus and must never be printed as a
    perfect `0.0`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    postings: int = 0
    applied: int = 0
    reply: int = 0
    screen: int = 0
    interview: int = 0
    offer: int = 0
    rejection: int = 0
    silence: int = 0
    effort_per_screen: float | None = None
    effort_per_interview: float | None = None

    def describe(self) -> str:
        return (
            f"  {self.action}: postings={self.postings} applied={self.applied} "
            f"reply={self.reply} screen={self.screen} interview={self.interview} "
            f"offer={self.offer} rejection={self.rejection} silence={self.silence} "
            f"-> applications/screen={_num(self.effort_per_screen, 2)} "
            f"applications/interview={_num(self.effort_per_interview, 2)}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


class ProductGateResult(BaseModel):
    """The spec.md §6 product gate verdict.

    `status` starts at `"unproven"` and stays there unless every requirement
    in `agent_gate`'s sibling docstring is met. `reason` always names what is
    missing (or, for a decided verdict, what was compared), so an
    `"unproven"` result is actionable rather than merely disappointing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["unproven", "pass", "fail"] = "unproven"
    outcomes_total: int = 0
    postings_with_outcomes: int = 0
    postings_matched_to_a_run: int = 0
    held_out_postings: int = 0
    outcome_counts: dict[str, int] = {}
    by_action: dict[str, ActionOutcomeStats] = {}
    recommended_actions: tuple[str, ...] = ("apply_now", "quick_apply")
    recommended_effort_per_screen: float | None = None
    comparison_effort_per_screen: float | None = None
    recommended_effort_per_interview: float | None = None
    comparison_effort_per_interview: float | None = None
    min_outcomes_required: int = 30
    reason: str = ""

    def describe(self) -> str:
        lines = [
            f"product gate (spec.md §6): {self.status.upper()}",
            f"  outcomes: {self.outcomes_total} row(s) over "
            f"{self.postings_with_outcomes} posting(s); "
            f"{self.held_out_postings} held-out; "
            f"{self.postings_matched_to_a_run} matched to a completed run",
            f"  outcome counts: {self.outcome_counts or '(none)'}",
            f"  recommended actions: {', '.join(self.recommended_actions) or '(none)'}",
            f"  applications/screen: recommended={_num(self.recommended_effort_per_screen, 2)} "
            f"vs rest={_num(self.comparison_effort_per_screen, 2)} (lower is better)",
            f"  applications/interview: "
            f"recommended={_num(self.recommended_effort_per_interview, 2)} "
            f"vs rest={_num(self.comparison_effort_per_interview, 2)} (lower is better)",
        ]
        for action in sorted(self.by_action):
            lines.append(self.by_action[action].describe())
        lines.append(f"  reason: {self.reason}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _decode_action(payload: object) -> str | None:
    """`recommended_action` out of a stored `runs.final_decision`, or `None`.

    Mirrors the total, non-raising decode that `rli.eval.baseline`,
    `rli.eval.report` and `rli.eval.metrics` each keep private to themselves:
    a NULL column, an empty string, invalid JSON, a non-object, or a decision
    with no `recommended_action` all yield `None`. Narrower than those
    helpers on purpose — this module needs exactly one field, and a gate that
    raised on one corrupt historical run would take the whole verdict with
    it.
    """
    if not isinstance(payload, str) or not payload.strip():
        return None
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    action = decoded.get("recommended_action")
    return None if action is None else str(action)


def _latest_action_by_posting(
    conn: sqlite3.Connection, posting_ids: Sequence[str], system: str
) -> dict[str, str]:
    """`posting_id -> recommended_action` from that posting's latest completed run.

    "Latest" is the greatest `(started_at, id)`, the same rule
    `rli.eval.baseline` uses to collapse duplicate runs. Both live and replay
    runs are eligible: an outcome is a real-world fact about a posting, and
    the recommendation attached to it is whatever this system most recently
    said about that posting, however it was produced. A run whose
    `final_decision` does not decode simply contributes no mapping.
    """
    actions: dict[str, str] = {}
    for chunk in _chunks(sorted(set(posting_ids))):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT posting_id, id, final_decision
            FROM runs
            WHERE posting_id IN ({placeholders})
                  AND system = ? AND status = 'completed'
            ORDER BY started_at, id
            """,
            (*chunk, system),
        ).fetchall()
        for row in rows:
            action = _decode_action(row["final_decision"])
            if action is None:
                continue
            # Rows arrive oldest-first, so a later row overwrites an earlier
            # one and the last write wins == the latest run wins.
            actions[str(row["posting_id"])] = action
    return actions


def _effort(applied: int, denominator: int) -> float | None:
    return _ratio(applied, denominator)


def product_gate(
    conn: sqlite3.Connection,
    *,
    splits: Mapping[str, str] | None = None,
    allowed_splits: tuple[str, ...] | None = None,
    system: str = "A",
    recommended_actions: tuple[str, ...] = ("apply_now", "quick_apply"),
    min_outcomes: int = 30,
) -> ProductGateResult:
    """Evaluate spec.md §6's product gate over recorded application outcomes.

    `allowed_splits=None` means `("test",)`: spec.md §6's product claim is
    only meaningful on postings the system never saw during development, so
    the holdout is the DEFAULT scope here — the opposite of every other entry
    point in this package, and deliberately so. Reading `outcomes` spends no
    holdout information about the *system* (the outcomes were produced by a
    human applying for jobs, not by a model), which is why this one gate may
    default to the test split while `rli.eval.metrics.resolve_allowed_splits`
    guards everything else.

    `splits=None` disables the split filter entirely; the result then still
    reports `held_out_postings=0` and stays `"unproven"`, with a `reason`
    saying that no posting could be shown to be held out. That is not a
    technicality: an effort comparison over postings the policy was tuned on
    proves nothing, and this function will not launder it into a `"pass"`.
    """
    scope_splits = ("test",) if allowed_splits is None else tuple(allowed_splits)

    outcome_rows = conn.execute(
        "SELECT posting_id, outcome_type FROM outcomes"
    ).fetchall()

    outcomes_total = len(outcome_rows)
    all_postings = {str(row["posting_id"]) for row in outcome_rows}
    outcome_counts: dict[str, int] = {}
    for row in outcome_rows:
        key = str(row["outcome_type"])
        outcome_counts[key] = outcome_counts.get(key, 0) + 1

    if splits is None:
        held_out = set()
    else:
        held_out = {
            posting_id
            for posting_id in all_postings
            if splits.get(posting_id) in scope_splits
        }

    # Everything downstream is measured over the in-scope postings only:
    # the held-out ones when a split map was supplied, and (so the report
    # still shows the shape of the data) all of them when it was not.
    in_scope = held_out if splits is not None else all_postings
    actions = _latest_action_by_posting(conn, sorted(in_scope), system)
    matched = {posting_id for posting_id in in_scope if posting_id in actions}

    per_action: dict[str, dict[str, int]] = {}
    per_action_postings: dict[str, set[str]] = {}
    for row in outcome_rows:
        posting_id = str(row["posting_id"])
        if posting_id not in matched:
            continue
        action = actions[posting_id]
        bucket = per_action.setdefault(action, dict.fromkeys(_OUTCOME_TYPES, 0))
        outcome_type = str(row["outcome_type"])
        if outcome_type in bucket:
            bucket[outcome_type] += 1
        per_action_postings.setdefault(action, set()).add(posting_id)

    by_action: dict[str, ActionOutcomeStats] = {}
    for action, bucket in per_action.items():
        by_action[action] = ActionOutcomeStats(
            action=action,
            postings=len(per_action_postings.get(action, set())),
            effort_per_screen=_effort(bucket["applied"], bucket["screen"]),
            effort_per_interview=_effort(bucket["applied"], bucket["interview"]),
            **bucket,
        )

    def _group(actions_in_group: tuple[str, ...]) -> tuple[int, int, int]:
        applied = screen = interview = 0
        for action, bucket in per_action.items():
            if action not in actions_in_group:
                continue
            applied += bucket["applied"]
            screen += bucket["screen"]
            interview += bucket["interview"]
        return applied, screen, interview

    other_actions = tuple(action for action in per_action if action not in recommended_actions)
    rec_applied, rec_screen, rec_interview = _group(recommended_actions)
    cmp_applied, cmp_screen, cmp_interview = _group(other_actions)

    recommended_effort_per_screen = _effort(rec_applied, rec_screen)
    comparison_effort_per_screen = _effort(cmp_applied, cmp_screen)
    recommended_effort_per_interview = _effort(rec_applied, rec_interview)
    comparison_effort_per_interview = _effort(cmp_applied, cmp_interview)

    result = ProductGateResult(
        outcomes_total=outcomes_total,
        postings_with_outcomes=len(all_postings),
        postings_matched_to_a_run=len(matched),
        held_out_postings=len(held_out),
        outcome_counts=outcome_counts,
        by_action=by_action,
        recommended_actions=tuple(recommended_actions),
        recommended_effort_per_screen=recommended_effort_per_screen,
        comparison_effort_per_screen=comparison_effort_per_screen,
        recommended_effort_per_interview=recommended_effort_per_interview,
        comparison_effort_per_interview=comparison_effort_per_interview,
        min_outcomes_required=min_outcomes,
    )

    missing: list[str] = []
    if outcomes_total == 0:
        missing.append(
            "the `outcomes` table is empty: no application, screen or interview outcome has "
            "ever been recorded, so the product claim has no evidence of any kind"
        )
    elif outcomes_total < min_outcomes:
        missing.append(
            f"only {outcomes_total} outcome row(s) recorded; spec.md §6's product claim needs "
            f"at least min_outcomes={min_outcomes} before an effort ratio means anything"
        )
    if splits is None:
        missing.append(
            "no split map was supplied, so no posting can be shown to be held out; spec.md §6 "
            "requires the product gate to be evaluated on held-out postings"
        )
    elif not held_out:
        missing.append(
            f"no posting with recorded outcomes falls in the held-out split(s) "
            f"{scope_splits!r}"
        )
    if outcomes_total and not matched:
        missing.append(
            f"no outcome-bearing posting in scope has a completed System {system} run, so no "
            "recommended_action can be attributed to any outcome"
        )
    if recommended_effort_per_screen is None or comparison_effort_per_screen is None:
        missing.append(
            "applications-per-screen is undefined for the recommended group "
            f"({_num(recommended_effort_per_screen, 2)}) or the comparison group "
            f"({_num(comparison_effort_per_screen, 2)}): no screen outcomes to divide by"
        )
    if recommended_effort_per_interview is None or comparison_effort_per_interview is None:
        missing.append(
            "applications-per-interview is undefined for the recommended group "
            f"({_num(recommended_effort_per_interview, 2)}) or the comparison group "
            f"({_num(comparison_effort_per_interview, 2)}): no interview outcomes to divide by"
        )

    if missing:
        return result.model_copy(
            update={
                "status": "unproven",
                "reason": "unproven: " + "; ".join(missing),
            }
        )

    # Every input is present. Only now can the answer be pass or fail.
    better = (
        recommended_effort_per_screen < comparison_effort_per_screen  # type: ignore[operator]
        and recommended_effort_per_interview < comparison_effort_per_interview  # type: ignore[operator]
    )
    verdict = "pass" if better else "fail"
    return result.model_copy(
        update={
            "status": verdict,
            "reason": (
                f"{verdict}: over {len(held_out)} held-out posting(s) and {outcomes_total} "
                f"outcome(s), applications/screen "
                f"{_num(recommended_effort_per_screen, 2)} vs "
                f"{_num(comparison_effort_per_screen, 2)} and applications/interview "
                f"{_num(recommended_effort_per_interview, 2)} vs "
                f"{_num(comparison_effort_per_interview, 2)} "
                f"(recommended actions {recommended_actions!r} vs everything else; "
                "lower is better)"
            ),
        }
    )
