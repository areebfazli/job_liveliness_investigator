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
  today: no LLM endpoint was reachable during development, so System C has
  no runs. But once
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
    MetricsCase,
    MetricsCaseSet,
    agent_efficiency,
    collect_system_runs,
)
from rli.probes.registry import DYNAMIC_PROBES

__all__ = [
    "AGENT_GATE_AGREEMENT_MARGIN",
    "AGENT_GATE_PROBE_RATIO",
    "GATE_TOLERANCE",
    "ActionOutcomeStats",
    "AgentGateResult",
    "CaseDifference",
    "LlmValueComparison",
    "LlmValueStatus",
    "LLM_MIN_AGREEMENT_GAIN",
    "LLM_MIN_COMPARED_CASES",
    "LLM_MIN_DIFFERING_CASES",
    "LLM_MIN_RELATIVE_PROBE_SAVING",
    "MAX_LISTED_DIFFERENCES",
    "SPEC_PREFER_SIMPLER",
    "ProductGateResult",
    "agent_gate",
    "llm_value_comparison",
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

    #: Which slice of the case set this verdict was computed over.
    #: `"pooled"` — the default — is the authoritative spec.md §6 verdict
    #: over every scoped case. Any other value (`"live-era"`) marks an
    #: INFORMATIONAL re-run over a subset, whose numbers are NOT the spec
    #: verdict. Only `describe()` reads this, but it has to exist: without
    #: it every gate announces itself as "agent gate (spec.md §6)", so the
    #: console output of `rli eval run` prints the live-era re-run under
    #: the spec's own name, one line below the real one, and the two are
    #: then distinguishable only by their run counts.
    scope: str = "pooled"

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
        # The scope has to be in the headline, not merely implied by the run
        # counts below it: `rli eval run` echoes the pooled gate and the
        # live-era re-run back to back, and an identical "agent gate
        # (spec.md §6)" banner on both invites reading the second block's
        # numbers as the spec verdict.
        title = (
            "agent gate (spec.md §6)"
            if self.scope == "pooled"
            else f"agent gate ({self.scope} slice, INFORMATIONAL - NOT the spec.md §6 verdict)"
        )
        lines = [
            f"{title}: {headline}",
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
    scope: str = "pooled",
) -> AgentGateResult:
    """Evaluate spec.md §6's agent gate for `candidate` against `baseline`.

    Both systems are measured against the same `reference` and the same case
    set, so the two agreement figures are comparable by construction. Pass
    `candidate_metrics` / `baseline_metrics` when the caller has already
    computed them (`rli.eval.evaluate` does) to avoid re-querying; they are
    recomputed otherwise. See the module docstring for the arithmetic and for
    why an unevidenced sub-check resolves against the candidate.

    `scope` labels which slice of the case set the caller passed in. Leave it
    at `"pooled"` for the spec.md §6 verdict; pass the era name when
    `case_set` is an era-filtered subset, so the result announces itself as
    informational rather than as the spec verdict.
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
        scope=scope,
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
            "database this is expected for System C: no LLM endpoint was configured or "
            "reachable, so C was never run. `passed` is None (unknown), NOT False (failed)."
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
# 1b. Does the LLM add anything? System C vs System R
# ---------------------------------------------------------------------------

#: How many differing cases `LlmValueComparison.differences` lists in full.
MAX_LISTED_DIFFERENCES = 50

#: spec.md §6, quoted where the comparison invokes it.
SPEC_PREFER_SIMPLER = "If rules are equally good and simpler, remove the agent."

#: MATERIALITY thresholds for crediting (or debiting) the LLM. A difference
#: counts only when BOTH hold: the rate moves by at least this much AND at
#: least `LLM_MIN_DIFFERING_CASES` compared cases move in that direction.
#: A float tolerance would let one run's quirk (one investigator that stopped
#: early, say) be reported as "the LLM's measured contribution".
#:
#: probe use: relative change in medium/high probe steps per compared case,
#: `(R - C) / R`, at least 1%.
LLM_MIN_RELATIVE_PROBE_SAVING = 0.01
#: agreement with the reference: at least 1 percentage point, overall or macro.
LLM_MIN_AGREEMENT_GAIN = 0.01
#: ...and at least this many compared cases in that direction (cases where C
#: used fewer medium/high probes than R; cases where C agrees with the
#: reference and R does not), mirrored for the "worse" direction.
LLM_MIN_DIFFERING_CASES = 10
#: Below this many COMPARED paired cases nothing is concluded either way
#: (`inconclusive`): ten-odd cases cannot show that R is "equally good".
LLM_MIN_COMPARED_CASES = 30

_DIM_PROBE_USE = "probe use"
#: A material LOSS charged to C when at least `LLM_MIN_DIFFERING_CASES`
#: paired C runs had an investigator error: those runs are excluded from the
#: rates (a failed investigator skips probes; that is not a saving), so their
#: cost to C has to be charged somewhere.
_DIM_RELIABILITY = "investigator reliability"

LlmValueStatus = Literal["not_run", "inconclusive", "llm_adds_nothing", "llm_better", "mixed"]


class CaseDifference(BaseModel):
    """One paired case where C and R chose different probes or actions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_url: str
    replay_at: str
    split: str
    candidate_probes: tuple[str, ...] = ()
    deterministic_probes: tuple[str, ...] = ()
    candidate_action: str | None = None
    deterministic_action: str | None = None
    reference_action: str | None = None
    candidate_investigator_error: bool = False


class LlmValueComparison(BaseModel):
    """System C (LLM investigator + controller) against System R (controller, no LLM).

    Both run the SAME controller, eligibility, ranking and budgets; the only
    difference is who proposes candidates. This is NOT a spec.md §6 gate (the
    agent gate, C vs B, stays as specified); it is what spec.md §6's last
    sentence needs to be applied honestly.

    Everything is measured on PAIRED cases only (same `(input_url,
    replay_at)` for C and R), and the C/R rates exclude cases whose C run had
    an investigator error: such a run skipped probes because the model
    failed, which is a C FAILURE, not a saving. Those cases are counted
    separately (`candidate_investigator_error_cases`).

    `status`:
    * `not_run` — no paired C/R case to compare;
    * `inconclusive` — fewer than `LLM_MIN_COMPARED_CASES` compared cases, or
      C wins no dimension materially but beats R's RATE on some dimension
      without enough cases behind it: the data can neither credit the LLM
      nor say R is as good;
    * `llm_adds_nothing` — on a sufficient sample, C's advantage on every
      dimension is below the RATE thresholds: R is equally good or better;
    * `llm_better` — C wins on at least one dimension and loses on none;
    * `mixed` — C wins on at least one dimension and loses on another
      (including investigator reliability).

    Agreement wins are counted NET and per improving class: for overall
    agreement, (cases where only C agrees with the reference) minus (only R
    agrees) must reach `LLM_MIN_DIFFERING_CASES`; for macro agreement, the
    same net count restricted to the reference classes where C's per-class
    rate is higher — so a few extra hits in a rare class cannot carry a
    macro "win" on their own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: str = "C"
    deterministic: str = "R"
    reference: str = "A"
    status: LlmValueStatus = "not_run"

    candidate_runs: int = 0
    deterministic_runs: int = 0
    #: Cases where both C and R ran.
    paired_cases: int = 0
    #: Paired cases whose C run had an investigator error (a C failure).
    candidate_investigator_error_cases: int = 0
    #: Paired cases the rates below are computed over (paired minus the above).
    compared_cases: int = 0

    candidate_medium_high_per_case: float | None = None
    deterministic_medium_high_per_case: float | None = None
    candidate_probe_points_per_case: float | None = None
    deterministic_probe_points_per_case: float | None = None
    #: `(R - C) / R` on medium/high probe steps; negative = C used more.
    relative_probe_saving: float | None = None
    candidate_cheaper_cases: int = 0
    deterministic_cheaper_cases: int = 0

    candidate_overall_agreement: float | None = None
    deterministic_overall_agreement: float | None = None
    candidate_macro_agreement: float | None = None
    deterministic_macro_agreement: float | None = None
    #: Compared cases where only C (only R) agrees with the reference.
    candidate_only_agrees_cases: int = 0
    deterministic_only_agrees_cases: int = 0

    #: C's action == R's action, over ALL paired cases.
    action_agreement: float | None = None
    same_sequence_cases: int = 0
    same_action_cases: int = 0
    same_sequence_and_action_cases: int = 0
    differing_cases_total: int = 0
    differing_with_investigator_error: int = 0
    differences: tuple[CaseDifference, ...] = ()

    #: Model usage over ALL of C's scoped runs (R's must be zero).
    candidate_model_steps: int = 0
    candidate_model_cost_usd: float = 0.0
    candidate_input_tokens: int = 0
    candidate_output_tokens: int = 0
    deterministic_model_steps: int = 0
    deterministic_model_cost_usd: float = 0.0

    #: Dimensions (`probe use`, `overall agreement with A`, `macro agreement
    #: with A`) C won / lost by a material margin.
    won: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    min_relative_probe_saving: float = LLM_MIN_RELATIVE_PROBE_SAVING
    min_agreement_gain: float = LLM_MIN_AGREEMENT_GAIN
    min_differing_cases: int = LLM_MIN_DIFFERING_CASES
    min_compared_cases: int = LLM_MIN_COMPARED_CASES
    #: Dimensions where one side's RATE advantage passes the threshold but too
    #: few cases support it: `"<dimension> (favours C|R)"`.
    underpowered: tuple[str, ...] = ()

    candidate_gate_status: str = "not_run"
    #: The same spec.md §6 legs with R as the candidate (informational).
    deterministic_gate: AgentGateResult | None = None

    verdict: str = ""

    @property
    def beats_on_cost(self) -> bool:
        return _DIM_PROBE_USE in self.won

    @property
    def worse_on_cost(self) -> bool:
        return _DIM_PROBE_USE in self.lost

    @property
    def beats_on_agreement(self) -> bool:
        return any(dim != _DIM_PROBE_USE for dim in self.won)

    @property
    def worse_on_agreement(self) -> bool:
        return any(dim != _DIM_PROBE_USE for dim in self.lost)

    def materiality_rule(self) -> str:
        return (
            f"A dimension is credited to (or charged against) {self.candidate} only when the "
            f"difference is material: medium/high probe use per compared case differs by >= "
            f"{self.min_relative_probe_saving:.0%} relative, or agreement with "
            f"{self.reference} (overall or macro) by >= {self.min_agreement_gain:.0%} points, "
            f"AND at least {self.min_differing_cases} compared cases move in that direction "
            "(net, per improving class for agreement). Fewer than "
            f"{self.min_compared_cases} compared cases is inconclusive. Rates are over paired "
            f"cases only, excluding {self.candidate} investigator-error runs; "
            f"{self.min_differing_cases} or more such runs is a material loss on investigator "
            "reliability."
        )

    def describe(self) -> str:
        lines = [
            f"does the LLM add anything? ({self.candidate} vs {self.deterministic}): "
            f"{self.status.upper()}",
            f"  paired cases={self.paired_cases}; compared={self.compared_cases} "
            f"(excluded {self.candidate} investigator-error cases="
            f"{self.candidate_investigator_error_cases}); same dynamic-probe sequence="
            f"{self.same_sequence_cases}; same action={self.same_action_cases}",
            f"  medium/high probes per compared case: {self.candidate}="
            f"{_num(self.candidate_medium_high_per_case)} {self.deterministic}="
            f"{_num(self.deterministic_medium_high_per_case)} (relative saving "
            f"{_pct(self.relative_probe_saving)}; {self.candidate} cheaper on "
            f"{self.candidate_cheaper_cases}, {self.deterministic} cheaper on "
            f"{self.deterministic_cheaper_cases})",
            f"  agreement with {self.reference} (overall/macro): {self.candidate}="
            f"{_pct(self.candidate_overall_agreement)}/{_pct(self.candidate_macro_agreement)} "
            f"{self.deterministic}={_pct(self.deterministic_overall_agreement)}/"
            f"{_pct(self.deterministic_macro_agreement)}",
            f"  model calls: {self.candidate}={self.candidate_model_steps} "
            f"(${self.candidate_model_cost_usd:.4f}, tokens "
            f"{self.candidate_input_tokens}/{self.candidate_output_tokens}) "
            f"{self.deterministic}={self.deterministic_model_steps}",
        ]
        if self.verdict:
            lines.append(f"  {self.verdict}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _dynamic_sequence(run: object) -> tuple[str, ...]:
    probes: tuple[str, ...] = getattr(run, "probes_run", ())
    return tuple(name for name in probes if name in DYNAMIC_PROBES)


def _agreement(
    pairs: Sequence[tuple[str | None, str | None]],
) -> tuple[float | None, float | None]:
    """`(overall, macro)` agreement of `(reference action, system action)` pairs.

    Same definitions as `rli.eval.metrics.agent_efficiency`: classes come
    from the reference; a `None` reference action is never a match.
    """
    if not pairs:
        return None, None
    overall = sum(1 for ref, got in pairs if ref is not None and ref == got) / len(pairs)
    per_class = []
    for action in sorted({ref for ref, _ in pairs if ref is not None}):
        in_class = [got for ref, got in pairs if ref == action]
        per_class.append(sum(1 for got in in_class if got == action) / len(in_class))
    return overall, (sum(per_class) / len(per_class) if per_class else None)


def _net_in_improving_classes(
    compared: Sequence[MetricsCase], candidate: str, deterministic: str, reference: str
) -> tuple[int, int]:
    """Net exclusive agreements inside the classes each side improves.

    Returns `(C-only minus R-only summed over the reference classes where C's
    per-class rate is higher, R-only minus C-only summed over the classes
    where R's is higher)`. This is the case count behind a macro-agreement
    difference.
    """
    by_class: dict[str, list[int]] = {}
    for case in compared:
        ref_run = case.runs.get(reference)
        ref_action = ref_run.action if ref_run is not None else None
        if ref_action is None:
            continue
        c_hit = case.runs[candidate].action == ref_action
        r_hit = case.runs[deterministic].action == ref_action
        counts = by_class.setdefault(ref_action, [0, 0])
        counts[0] += c_hit and not r_hit
        counts[1] += r_hit and not c_hit
    c_net = sum(c - r for c, r in by_class.values() if c > r)
    r_net = sum(r - c for c, r in by_class.values() if r > c)
    return c_net, r_net


def llm_value_comparison(
    case_set: MetricsCaseSet,
    *,
    run_costs: Mapping[str, tuple[int, float]],
    candidate_metrics: EfficiencyMetrics | None = None,
    deterministic_metrics: EfficiencyMetrics | None = None,
    candidate: str = "C",
    deterministic: str = "R",
    reference: str = "A",
    investigator_error_run_ids: frozenset[str] | set[str] = frozenset(),
    candidate_gate_status: str = "not_run",
    deterministic_gate: AgentGateResult | None = None,
    max_listed: int = MAX_LISTED_DIFFERENCES,
    min_relative_probe_saving: float = LLM_MIN_RELATIVE_PROBE_SAVING,
    min_agreement_gain: float = LLM_MIN_AGREEMENT_GAIN,
    min_differing_cases: int = LLM_MIN_DIFFERING_CASES,
    min_compared_cases: int | None = None,
) -> LlmValueComparison:
    """Compare C and R on PAIRED cases: probe use, agreement, per-case choices, model cost.

    `run_costs` maps `run_id -> (medium/high probe steps, probe cost points)`
    (`rli.eval.diagnostics.run_probe_costs`). The metrics objects only supply
    run totals and model usage. See `LlmValueComparison` for the exclusion of
    investigator-error runs and the materiality rule. `min_compared_cases`
    defaults to `LLM_MIN_COMPARED_CASES`, read at call time.
    """
    if min_compared_cases is None:
        min_compared_cases = LLM_MIN_COMPARED_CASES
    base = LlmValueComparison(
        candidate=candidate,
        deterministic=deterministic,
        reference=reference,
        candidate_gate_status=candidate_gate_status,
        deterministic_gate=deterministic_gate,
        candidate_runs=len(case_set.run_ids.get(candidate, ())),
        deterministic_runs=len(case_set.run_ids.get(deterministic, ())),
        min_relative_probe_saving=min_relative_probe_saving,
        min_agreement_gain=min_agreement_gain,
        min_differing_cases=min_differing_cases,
        min_compared_cases=min_compared_cases,
    )
    paired = case_set.cases_for(candidate, deterministic)
    if not paired:
        missing = [
            name for name in (candidate, deterministic) if not case_set.run_ids.get(name)
        ] or [f"{candidate}+{deterministic} on the same cases"]
        return base.model_copy(
            update={
                "verdict": (
                    f"Not compared: no paired {candidate}/{deterministic} case "
                    f"({', '.join(missing)} missing). Replay System {deterministic} offline "
                    f"(`rli replay run --system {deterministic} --dataset "
                    f"{case_set.dataset_id}`; no LLM) and re-run the evaluation to see "
                    "whether the LLM adds anything over the deterministic controller."
                ),
            }
        )

    same_sequence = same_action = same_both = 0
    differences: list[CaseDifference] = []
    differing_total = differing_errors = error_cases = 0
    compared = []
    for case in paired:
        c_run = case.runs[candidate]
        r_run = case.runs[deterministic]
        had_error = c_run.run_id in investigator_error_run_ids
        error_cases += had_error
        if not had_error:
            compared.append(case)
        sequence_equal = _dynamic_sequence(c_run) == _dynamic_sequence(r_run)
        action_equal = c_run.action == r_run.action
        same_sequence += sequence_equal
        same_action += action_equal
        same_both += sequence_equal and action_equal
        if sequence_equal and action_equal:
            continue
        differing_total += 1
        differing_errors += had_error
        if len(differences) < max_listed:
            ref_run = case.runs.get(reference)
            differences.append(
                CaseDifference(
                    input_url=case.input_url,
                    replay_at=case.replay_at,
                    split=case.split,
                    candidate_probes=_dynamic_sequence(c_run),
                    deterministic_probes=_dynamic_sequence(r_run),
                    candidate_action=c_run.action,
                    deterministic_action=r_run.action,
                    reference_action=ref_run.action if ref_run is not None else None,
                    candidate_investigator_error=had_error,
                )
            )

    # --- rates over the compared cases ------------------------------------
    c_mh = r_mh = 0
    c_points = r_points = 0.0
    c_cheaper = r_cheaper = c_only = r_only = 0
    c_pairs: list[tuple[str | None, str | None]] = []
    r_pairs: list[tuple[str | None, str | None]] = []
    for case in compared:
        c_run, r_run = case.runs[candidate], case.runs[deterministic]
        c_cost = run_costs.get(c_run.run_id, (0, 0.0))
        r_cost = run_costs.get(r_run.run_id, (0, 0.0))
        c_mh += c_cost[0]
        r_mh += r_cost[0]
        c_points += c_cost[1]
        r_points += r_cost[1]
        c_cheaper += c_cost[0] < r_cost[0]
        r_cheaper += r_cost[0] < c_cost[0]
        ref_run = case.runs.get(reference)
        ref_action = ref_run.action if ref_run is not None else None
        c_pairs.append((ref_action, c_run.action))
        r_pairs.append((ref_action, r_run.action))
        if ref_action is not None:
            c_hit = c_run.action == ref_action
            r_hit = r_run.action == ref_action
            c_only += c_hit and not r_hit
            r_only += r_hit and not c_hit
    n = len(compared)
    c_overall, c_macro = _agreement(c_pairs)
    r_overall, r_macro = _agreement(r_pairs)
    relative_saving = None if not n or r_mh == 0 else (r_mh - c_mh) / r_mh

    # --- materiality, per dimension and direction ----------------------------
    won: list[str] = []
    lost: list[str] = []
    underpowered: list[str] = []

    def judge(label: str, *, c_rate_ok: bool, c_count: int, r_rate_ok: bool, r_count: int) -> None:
        if c_rate_ok:
            if c_count >= min_differing_cases:
                won.append(label)
            else:
                underpowered.append(f"{label} (favours {candidate})")
        if r_rate_ok:
            if r_count >= min_differing_cases:
                lost.append(label)
            else:
                underpowered.append(f"{label} (favours {deterministic})")

    if n:
        judge(
            _DIM_PROBE_USE,
            c_rate_ok=relative_saving is not None and relative_saving >= min_relative_probe_saving,
            c_count=c_cheaper,
            r_rate_ok=(
                relative_saving is not None and -relative_saving >= min_relative_probe_saving
            )
            or (r_mh == 0 and c_mh > 0),
            r_count=r_cheaper,
        )
        if c_overall is not None and r_overall is not None:
            judge(
                f"overall agreement with {reference}",
                c_rate_ok=c_overall - r_overall >= min_agreement_gain,
                c_count=c_only - r_only,
                r_rate_ok=r_overall - c_overall >= min_agreement_gain,
                r_count=r_only - c_only,
            )
        if c_macro is not None and r_macro is not None:
            c_net, r_net = _net_in_improving_classes(compared, candidate, deterministic, reference)
            judge(
                f"macro agreement with {reference}",
                c_rate_ok=c_macro - r_macro >= min_agreement_gain,
                c_count=c_net,
                r_rate_ok=r_macro - c_macro >= min_agreement_gain,
                r_count=r_net,
            )
    if error_cases >= min_differing_cases:
        lost.append(_DIM_RELIABILITY)

    status: LlmValueStatus
    if not paired:
        status = "not_run"
    elif n < min_compared_cases:
        status = "inconclusive"
    elif won:
        status = "mixed" if lost else "llm_better"
    elif any(item.endswith(f"(favours {candidate})") for item in underpowered):
        status = "inconclusive"
    else:
        status = "llm_adds_nothing"

    c_cost_split = candidate_metrics.cost if candidate_metrics is not None else None
    model_steps = c_cost_split.model_steps if c_cost_split is not None else 0
    model_usd = c_cost_split.model_cost_usd if c_cost_split is not None else 0.0
    tokens_in = c_cost_split.input_tokens if c_cost_split is not None else 0
    tokens_out = c_cost_split.output_tokens if c_cost_split is not None else 0

    def rate(total: float) -> float | None:
        return total / n if n else None

    numbers = (
        f"Over {n} compared paired case(s), {deterministic} uses {_num(rate(r_mh))} medium/high "
        f"probe steps per case vs {candidate}'s {_num(rate(c_mh))} (relative saving by "
        f"{candidate}: {_pct(relative_saving)}; {candidate} cheaper on {c_cheaper} case(s), "
        f"{deterministic} cheaper on {r_cheaper}) and agrees with {reference} "
        f"{_pct(r_overall)} overall / {_pct(r_macro)} macro vs {candidate}'s "
        f"{_pct(c_overall)} / {_pct(c_macro)} (only {candidate} agrees on {c_only} case(s), only "
        f"{deterministic} on {r_only}), with 0 model calls; {candidate} made {model_steps} model "
        f"calls over all its runs (${model_usd:.4f}, {tokens_in}/{tokens_out} tokens in/out). "
        f"On all {len(paired)} paired cases the two chose the same dynamic-probe sequence "
        f"{same_sequence} times and the same action {same_action} times."
    )
    failures = (
        f" {error_cases} paired {candidate} run(s) had an investigator error; they are excluded "
        f"from these rates and counted as {candidate} FAILURES (a failed investigator skips "
        "probes; that is not a saving)."
        if error_cases
        else ""
    )
    rule = base.materiality_rule()
    weak = (
        f" Rate advantages without enough cases behind them: {', '.join(underpowered)}."
        if underpowered
        else ""
    )
    if status == "not_run":
        verdict = (
            f"Not compared: every paired case's {candidate} run had an investigator error."
            + failures
        )
    elif status == "inconclusive":
        reason = (
            f"only {n} compared paired case(s) (minimum {min_compared_cases})"
            if n < min_compared_cases
            else f"{candidate} beats {deterministic}'s rate on some dimension, but too few "
            "cases support it"
        )
        verdict = (
            f"INCONCLUSIVE: {reason}. {numbers}{failures}{weak} {rule} This sample can neither "
            f"credit the LLM nor show that {deterministic} is equally good; do not attribute "
            f"the spec.md §6 agent-gate result for {candidate} ({candidate_gate_status}) either "
            "way from this comparison."
        )
    elif status == "llm_adds_nothing":
        verdict = (
            f"The LLM adds nothing measurable on this dataset. {numbers}{failures}{weak} "
            f"On {n} compared cases {candidate}'s advantage on every dimension is below the rate "
            f"thresholds. {rule} The spec.md §6 agent-gate result for {candidate} "
            f"({candidate_gate_status}) is therefore attributable to the deterministic controller "
            "both systems share (eligibility gate, could-change-action filter, cost-aware "
            f'ranking, budgets), not to the LLM investigator. spec.md §6: "{SPEC_PREFER_SIMPLER}" '
            f"{deterministic} is equally good or better and simpler, so by spec.md §6 "
            f"{deterministic} should be preferred to {candidate}."
        )
    elif status == "mixed":
        verdict = (
            f"Mixed: {candidate} wins on {', '.join(won)} but loses on {', '.join(lost)}. "
            f"{numbers}{failures}{weak} {rule} Whether that trade is worth an LLM is a judgment "
            "the agent gate alone does not make; spec.md §6 prefers the simpler system when it "
            "is equally good."
        )
    else:
        verdict = (
            f"{candidate} wins on {', '.join(won)} and loses on none. {numbers}{failures}{weak} "
            f"{rule} That is the LLM's measured contribution, bought at the model cost above."
        )

    return base.model_copy(
        update={
            "status": status,
            "paired_cases": len(paired),
            "candidate_investigator_error_cases": error_cases,
            "compared_cases": n,
            "candidate_medium_high_per_case": rate(c_mh),
            "deterministic_medium_high_per_case": rate(r_mh),
            "candidate_probe_points_per_case": rate(c_points),
            "deterministic_probe_points_per_case": rate(r_points),
            "relative_probe_saving": relative_saving,
            "candidate_cheaper_cases": c_cheaper,
            "deterministic_cheaper_cases": r_cheaper,
            "candidate_overall_agreement": c_overall,
            "deterministic_overall_agreement": r_overall,
            "candidate_macro_agreement": c_macro,
            "deterministic_macro_agreement": r_macro,
            "candidate_only_agrees_cases": c_only,
            "deterministic_only_agrees_cases": r_only,
            "action_agreement": _ratio(same_action, len(paired)),
            "same_sequence_cases": same_sequence,
            "same_action_cases": same_action,
            "same_sequence_and_action_cases": same_both,
            "differing_cases_total": differing_total,
            "differing_with_investigator_error": differing_errors,
            "differences": tuple(differences),
            "candidate_model_steps": model_steps,
            "candidate_model_cost_usd": model_usd,
            "candidate_input_tokens": tokens_in,
            "candidate_output_tokens": tokens_out,
            "deterministic_model_steps": (
                deterministic_metrics.cost.model_steps if deterministic_metrics else 0
            ),
            "deterministic_model_cost_usd": (
                deterministic_metrics.cost.model_cost_usd if deterministic_metrics else 0.0
            ),
            "won": tuple(won),
            "lost": tuple(lost),
            "underpowered": tuple(underpowered),
            "verdict": verdict,
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

    outcome_rows = conn.execute("SELECT posting_id, outcome_type FROM outcomes").fetchall()

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
            posting_id for posting_id in all_postings if splits.get(posting_id) in scope_splits
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
            f"no posting with recorded outcomes falls in the held-out split(s) {scope_splits!r}"
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
