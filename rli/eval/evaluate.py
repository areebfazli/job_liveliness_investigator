"""The spec.md §6 final evaluation, orchestrated end to end (PLAN.md M6).

Every other module in `rli.eval` answers one question about one replay
dataset: `rli.eval.metrics` pairs runs and computes agent-efficiency and
data-quality figures, `rli.eval.gates` turns those figures into the two
spec.md §6 verdicts, `rli.eval.ranker` asks whether a learned probe ranking
(spec.md §6 "C2") beats the deterministic one, `rli.eval.survival` describes
how postings actually behave, and `rli.replay.leakage` audits whether any of
it saw the future. This module is the one that runs all of them over the
SAME dataset, the SAME split map and the SAME case set, and writes the single
Markdown artifact PLAN.md M6 asks for.

It is deliberately thin on arithmetic and thick on bookkeeping: nothing here
computes a metric that `metrics.py` / `gates.py` / `ranker.py` already
define. What it owns is the set of decisions that only make sense once, at
the top of the evaluation —

* which splits may be read, and how loudly that is recorded;
* which systems need replaying versus which already have runs on disk;
* what "System C" means when there is no model to call;
* which sample sizes the headline gate is actually judged on; and
* every caveat that must travel attached to a number rather than three
  files away in a docstring.

--------------------------------------------------------------------------
The `test` holdout: a READ permission, not a BUILD permission
--------------------------------------------------------------------------

`rli/replay/build.py` refuses `split="test"` outright, and that refusal is
untouched by this module — deliberately. Building a replay dataset spends
LIVE probe calls against real ATS boards and the Wayback Machine; it is the
one irreversible, rate-limited, externally-visible act in the whole system.
An interlock that stops the holdout from being *collected* early is
protecting a resource that cannot be un-spent, and it stays.

`rli.eval.baseline._check_allowed_splits` is the same interlock one layer up
for M4's baseline report, and it is also untouched: it refuses `"test"`
unconditionally, with no override flag, exactly as PLAN.md M4 requires.

What M6 needs is neither of those. spec.md §6 says the final evaluation is
scored on the held-out split; by the time this module runs, the dataset in
question has already been built (from `dev` or `validation`, or from `test`
by a deliberate, separate act), and reading a `runs` row costs nothing and
reveals nothing to the outside world. So `evaluate` gets its OWN gate —
`rli.eval.metrics.resolve_allowed_splits(..., allow_test=True)` — which
widens the READ-side filter only, defaults to `allow_test=True` because this
IS the final evaluation, and leaves both write-side interlocks exactly where
they are.

Because "we read the holdout" is precisely the fact a later reader must not
have to take on trust, reading it is recorded in three places at once:

1. **In the trace.** Every scoped run whose case falls in the `test` split
   gets one extra `run_steps` row — `component='controller'`,
   `decision_type='holdout_test_evaluated'`, with the explanation in the
   `error` column so it shows up in any "what went wrong / what is unusual"
   query rather than only in a metrics view. It is written at most once per
   run (an existing row short-circuits it), so re-running `evaluate` audits
   the same runs without spamming their traces, and it is written ONLY for
   runs that really are in the holdout — not for every run in a scoped
   dataset, which would make the marker meaningless.
2. **In `agent_gate.notes`**, next to the gate arithmetic it affects.
3. **In the report's Limitations section.**

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **`evaluate` never makes a network call, and never builds a dataset.**
  `rli.replay.build.build_dataset` is not imported here at all. The only
  execution this module can trigger is `rli.replay.run.run_replay`, which is
  structurally offline (`rli.replay.mode.ReplayNetClient` raises on any
  attempt). An evaluation that could silently start collecting would be an
  evaluation that could silently change what it is evaluating.

* **Reuse is the default; re-running is opt-in.** A system with at least one
  scoped run for the dataset is `"reused"`. Replaying is deterministic, so a
  re-run would normally produce the same numbers — but "normally" is doing a
  lot of work there (the frozen policy, the config hash, the cached probe
  record and the collection-status CSV all feed it), and an evaluation that
  quietly rewrote the runs it was about to score would make "the report
  disagrees with the database" impossible to diagnose. `rerun=True` forces
  `run_replay(..., replace=True)`, which deletes the previous scoped runs
  first so the case pairing stays unambiguous (see `rli.replay.run`'s
  docstring on why a kept duplicate degenerates to "greatest uuid wins").

* **"System C was not run" is a first-class state, not a gap.** This module
  will never attempt a MODEL call to discover whether System C is usable.
  When `with_c` is requested without an explicit `llm` / `llm_factory`, the
  configured endpoint (`[llm].base_url`) is checked by
  `rli.llm.client.endpoint_unavailable_reason` — a config check plus a
  bounded, free `GET {base_url}/models` that runs no model and spends no
  tokens. If it is not usable, C's status is
  `"skipped: not run: no LLM endpoint configured"`, the agent gate's status is
  `"not_run"` (not `"fail"` — an ungraded candidate has not failed), and the
  literal phrase `not run: no LLM endpoint configured` is printed next to
  every place a C
  number would otherwise appear. Tests drive C through
  `rli.llm.client.ScriptedClient` via `llm=` / `llm_factory=`, which is the
  only supported way to exercise the path.

* **Cost is reported as two columns that are never added together.**
  `run_steps.cost_usd` is overloaded: rows with `component='probe'` carry the
  unitless placeholder cost POINTS from `[probe_costs]`, rows with
  `component='model'` carry real USD. `runs.total_cost_usd` sums them, which
  makes it meaningless across systems (a probe-heavy System A and a
  model-heavy System C are not on the same axis). `rli.eval.metrics.CostSplit`
  keeps the two apart and this module's writer keeps them in separate columns
  with an explicit "different units, never summed" note, because a single
  "total cost" figure in a Markdown table will be quoted as dollars by
  someone who never read this docstring.

* **The System A structural caveat is printed next to the gate, not in a
  footnote.** `rli.eval.system_a` neutralises the unresolved-question gate
  (`eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`) while
  System C is gated by `could_change_action`, so any probe-count comparison
  against A is biased in C's favour BY CONSTRUCTION. spec.md §6's agent gate
  is a C-vs-B ratio and therefore not directly affected, but the agreement
  legs are both measured against A, and every efficiency block in the report
  is an "X vs A" block. `rli.eval.metrics.SYSTEM_A_CAVEAT` therefore rides on
  `EfficiencyMetrics.structural_caveat`, on `AgentGateResult.notes`, and is
  printed immediately below the gate verdict in the report.

* **Data quality is scoped to System A's runs.** A, B and C share one cached
  probe record and one case-state builder, so ATS resolution and publish-date
  coverage are properties of the DATASET, not of the system reading it — but
  only A runs every probe. Scoping the data-quality block to A measures the
  record at its fullest; scoping it to all three would report the same
  postings two or three times over with B's deliberately narrower probe set
  dragging coverage down for reasons that have nothing to do with data
  quality. When A has no scoped runs the block falls back to whatever
  systems do, so an A-less dataset still produces a report. Citation
  support is the exception: it is a property of what each SYSTEM published
  (C publishes LLM-written reasons), so it is reported per system, with C's
  dropped citations, explanation fallbacks and investigator errors next to
  it (`rli.eval.diagnostics.agent_trace_stats`).

* **System R is the LLM's control.** `rli.eval.system_r` runs System C's
  controller, eligibility, ranking and budgets with a deterministic "propose
  every eligible probe" step instead of the LLM investigator, and makes no
  model call. spec.md §6's agent gate (C vs B) is kept exactly as specified;
  next to it the report compares C with R (`rli.eval.gates.
  llm_value_comparison`) and says plainly when C beats R on neither probe
  cost nor agreement, because then the gate pass belongs to the
  deterministic controller and spec.md §6 says to prefer the simpler
  system. R is offline like A and B: it is reused when the dataset has R
  runs and replayed only on request (`with_r=True`).

* **`read_only=True` writes nothing.** No replay, no holdout marker; the
  report says which systems were absent and that no marker was written. The
  CLI's `--read-only` additionally opens the database with SQLite's
  `mode=ro`, so a report can be generated from a live database safely.

* **Two sample-size answers are reported, and the headline gate is judged on
  the smaller one.** spec.md §6's targets (>=300 postings, >=40 companies,
  >=100 closure events) are about the evidence behind the conclusions. The
  collection corpus can clear them comfortably while the replay dataset that
  was actually scored is a far smaller slice of it, and quoting the corpus
  number as though the evaluation had that much backing would be the single
  most misleading thing this report could do. So `SampleSizes` carries both,
  the writer puts them in adjacent columns, and `meets_postings` /
  `meets_companies` are computed on what was actually SCORED — cases that
  passed the split gate and whose identity resolved
  (`rli.eval.diagnostics.scored_sample`) — shown next to the BUILT counts
  recorded on the dataset row.
  `meets_closures` is the exception, and an acknowledged one: a replay
  dataset's unit is a `(posting, T)` grid point, not a closure event, so
  there is no dataset-scoped closure count to compare against — that leg is
  necessarily corpus-wide and is labelled as such everywhere it appears.

* **The survival summary is corpus-wide, and says so.**
  `rli.eval.survival.behavior_report` takes no dataset or split argument; its
  only scope is a single `company_id`. Calling it once, unscoped, is the only
  honest option — a per-dataset survival curve is not something that function
  can produce, and silently labelling a corpus-wide curve "dataset" would be
  a fabrication. It is imported lazily (lifelines pulls pandas, scipy and
  autograd, and costs seconds to import plus tens of seconds to fit on the
  real database) and wrapped in `try/except Exception`: a fitter that fails
  on a degenerate sample must cost the report one section, never the whole
  run. `include_survival=False` skips it entirely, which is what the fast
  tests use.

* **Nothing here raises on corrupt stored data.** A NULL or unparseable
  `final_decision`, a missing `replay_datasets` row, a survival fit that
  blows up, an outcomes table with zero rows — each degrades to a counted,
  named, printed state. `write_evaluation_report` in particular must produce
  a valid file for ANY `EvaluationReport`, including a wholly empty one:
  a report that cannot be written is a report that cannot be reviewed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.diagnostics import (
    AgentTraceStats,
    CompanyHoldoutCheck,
    EraBoundaries,
    GridDistribution,
    ProbeDependence,
    ScoredSample,
    SplitCounts,
    agent_trace_stats,
    company_holdout_check,
    era_boundaries,
    grid_distribution,
    policy_branch_counts,
    probe_dependence,
    run_probe_costs,
    scored_sample,
    split_case_set_by_company_era,
    split_counts,
)
from rli.eval.gates import (
    AgentGateResult,
    LlmValueComparison,
    ProductGateResult,
    agent_gate,
    llm_value_comparison,
    product_gate,
)
from rli.eval.metrics import (
    DEFAULT_MATCH_PRECISION_PATH,
    SYSTEM_A_CAVEAT,
    CitationSupport,
    DataQuality,
    EfficiencyMetrics,
    MetricsCaseSet,
    agent_efficiency,
    citation_support_for_runs,
    collect_system_runs,
    data_quality,
    resolve_allowed_splits,
    split_map_for_dataset,
)
from rli.eval.ranker import RankerConfig, RankerResult, evaluate_ranker

# `endpoint_unavailable_reason` is imported at module scope, not lazily like
# `rli.agent.loop` below: `rli.llm.client` pulls only httpx and the config
# models (never the agent/investigator stack), and a module-level name is
# what lets a test substitute the probe.
from rli.llm.client import endpoint_unavailable_reason
from rli.models.time import now_utc, to_utc_z
from rli.policy.action import policy_version

__all__ = [
    "C_NOT_RUN_REASON",
    "READ_ONLY_SKIP",
    "DEFAULT_REPORT_PATH",
    "HOLDOUT_TEST_NOTE",
    "HOLDOUT_TEST_STEP",
    "SPEC_TARGET_CLOSURES",
    "SPEC_TARGET_COMPANIES",
    "SPEC_TARGET_POSTINGS",
    "EvaluationReport",
    "SampleSizes",
    "evaluate",
    "write_evaluation_report",
]

#: Where PLAN.md M6's tracked artifact lives. The CLI's `--out` default
#: mirrors this string literally (it cannot import this module at option
#: definition time without paying for the import on every `rli --help`).
DEFAULT_REPORT_PATH = "reports/evaluation.md"

#: spec.md §6 sample-size targets. Judged on the EVALUATED dataset for
#: postings/companies; necessarily corpus-wide for closures (see the module
#: docstring's sample-size judgment call).
SPEC_TARGET_POSTINGS = 300
SPEC_TARGET_COMPANIES = 40
SPEC_TARGET_CLOSURES = 100

#: `run_steps.decision_type` for the holdout-read marker. Not part of any
#: system's trace vocabulary on purpose — it records an act of the EVALUATOR,
#: not a decision the system under test made.
HOLDOUT_TEST_STEP = "holdout_test_evaluated"

HOLDOUT_TEST_NOTE = (
    "spec.md §6 final evaluation: the 'test' holdout was read by "
    "rli.eval.evaluate with allow_test=True. This is a READ-side permission "
    "over a dataset that already exists; rli.replay.build's build-time "
    "refusal of split='test' and rli.eval.baseline's unconditional refusal "
    "are both untouched. Any metric computed after this step has seen the "
    "final holdout and can no longer be used to tune anything."
)

#: The exact phrase that must appear next to every System C figure when no
#: model client is available. Callers grep for it; do not reword it.
C_NOT_RUN_REASON = "not run: no LLM endpoint configured"

_SYSTEM_ORDER = ("A", "B", "C", "R", "C2")

#: Systems `evaluate` scores whenever the dataset carries their runs.
_SCORED_SYSTEMS = ("A", "B", "C", "R")

#: `systems_run` status of a system that was absent under `read_only=True`.
READ_ONLY_SKIP = "skipped: read-only evaluation (no runs on this dataset; nothing was replayed)"

_MISSING = "n/a"


# ---------------------------------------------------------------------------
# 1. Small formatting helpers (shared by describe() and the writer)
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return _MISSING if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 2) -> str:
    return _MISSING if value is None else f"{value:.{digits}f}"


def _flag(value: bool | None) -> str:
    if value is None:
        return _MISSING
    return "yes" if value else "NO"


def _render(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown table, padding short rows so it always stays valid.

    Mirrors `rli.eval.baseline._markdown_table` (kept local rather than
    imported, since that helper is private to the M4 report writer), with one
    addition: a row shorter than `headers` is padded instead of producing a
    ragged table, because this writer must never raise for any report.
    """
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        lines.append("| " + " | ".join(padded[: len(headers)]) + " |")
    if not rows:
        lines.append("| " + " | ".join(["(none)"] * len(headers)) + " |")
    return "\n".join(lines)


def _ordered_systems(names: object) -> list[str]:
    """`names` sorted A, B, C, C2 first, then anything else alphabetically."""
    try:
        present = list(names)  # type: ignore[call-overload]
    except TypeError:  # pragma: no cover - defensive
        return []
    known = [name for name in _SYSTEM_ORDER if name in present]
    extra = sorted(str(name) for name in present if name not in _SYSTEM_ORDER)
    return known + extra


# ---------------------------------------------------------------------------
# 2. Sample sizes (spec.md §6 headline gate)
# ---------------------------------------------------------------------------


class SampleSizes(BaseModel):
    """spec.md §6's sample-size targets, answered three times over.

    `dataset_*` is what the replay dataset was BUILT with (its
    `replay_datasets` row); `scored_*` is what this evaluation actually
    SCORED — cases that passed the split gate and whose identity resolved
    (`rli.eval.diagnostics.scored_sample`); `corpus_*` is the whole
    collection corpus. The `meets_*` flags for postings and companies are
    judged on the SCORED figures; closures are necessarily corpus-wide.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_postings: int = 0
    dataset_companies: int = 0
    dataset_cases: int = 0

    scored_postings: int = 0
    scored_companies: int = 0
    scored_cases: int = 0
    identity_unresolved_cases: int = 0
    unassigned_cases: int = 0
    excluded_holdout_cases: int = 0

    corpus_postings: int = 0
    corpus_companies: int = 0
    corpus_closures: int = 0

    target_postings: int = SPEC_TARGET_POSTINGS
    target_companies: int = SPEC_TARGET_COMPANIES
    target_closures: int = SPEC_TARGET_CLOSURES

    meets_postings: bool = False
    meets_companies: bool = False
    meets_closures: bool = False

    #: The EARLIEST own (`source='own'`) board capture anywhere — display
    #: only. Era membership is per company: a case is live-era iff T is at or
    #: after ITS company's first own capture (`rli.eval.diagnostics.
    #: EraBoundaries`). `None` means no own capture was ever recorded.
    era_boundary: str | None = None
    #: Companies (of the cases in scope) with / without any own capture.
    era_companies_with_own: int = 0
    era_companies_total: int = 0
    live_era_cases: int = 0
    archive_era_cases: int = 0
    #: `live_era_cases / (live_era_cases + archive_era_cases)`, `None` only
    #: when that denominator is 0 (an empty case set).
    live_era_share: float | None = None

    @property
    def headline_gate_met(self) -> bool:
        """All three spec.md §6 sample-size legs cleared (postings/companies on SCORED data)."""
        return self.meets_postings and self.meets_companies and self.meets_closures

    def describe(self) -> str:
        lines = [
            "sample sizes (spec.md §6 targets):",
            f"  built dataset: postings={self.dataset_postings} "
            f"companies={self.dataset_companies} cases={self.dataset_cases}",
            f"  scored: postings={self.scored_postings} companies={self.scored_companies} "
            f"cases={self.scored_cases} (excluded: identity_unresolved="
            f"{self.identity_unresolved_cases} unassigned={self.unassigned_cases} "
            f"holdout_not_read={self.excluded_holdout_cases})",
            f"  collection corpus: postings={self.corpus_postings} "
            f"companies={self.corpus_companies} closure_events={self.corpus_closures}",
            f"  targets: postings>={self.target_postings} "
            f"companies>={self.target_companies} closures>={self.target_closures}",
            f"  met (on SCORED data): postings={_flag(self.meets_postings)} "
            f"companies={_flag(self.meets_companies)} "
            f"closures(corpus-wide)={_flag(self.meets_closures)}",
            f"  headline gate met: {_flag(self.headline_gate_met)}",
            f"  era split (per-company boundary): live={self.live_era_cases} "
            f"archive={self.archive_era_cases} live-era_share={_pct(self.live_era_share)}; "
            f"{self.era_companies_with_own}/{self.era_companies_total} companies have an own "
            "capture; earliest own capture="
            + (
                self.era_boundary
                if self.era_boundary is not None
                else "(none — no own board snapshots yet, every case is archive-era)"
            ),
        ]
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


# ---------------------------------------------------------------------------
# 3. The report model
# ---------------------------------------------------------------------------


class EvaluationReport(BaseModel):
    """Everything spec.md §6 asks for about one replay dataset, in one object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    split_kind: str
    split_name: str
    allowed_splits: tuple[str, ...]
    allow_test: bool
    generated_at: str
    policy_version: str

    systems_run: dict[str, str]
    case_set: MetricsCaseSet
    efficiency: dict[str, EfficiencyMetrics]
    #: Per-era (`"live-era"` / `"archive-era"`) copy of `efficiency`, keyed
    #: `[era][system]`. Informational only — see `_era_split_section`; the
    #: pooled `efficiency` above stays what every other section reads.
    efficiency_by_era: dict[str, dict[str, EfficiencyMetrics]] = {}
    data_quality: DataQuality
    agent_gate: AgentGateResult
    #: Informational, NON-authoritative re-run of `agent_gate` scoped to
    #: live-era cases only (spec.md §1's era split). The pooled `agent_gate`
    #: above remains the spec.md §6 verdict; this field never substitutes
    #: for it. Always populated by `evaluate` (its `status` reads `not_run`
    #: exactly when the pooled gate's does, e.g. System C absent).
    live_era_gate: AgentGateResult
    product_gate: ProductGateResult
    ranker: RankerResult
    sample_sizes: SampleSizes

    survival: object | None = None
    survival_note: str = ""
    limitations: tuple[str, ...] = ()

    #: `True` when nothing was written: no replay, no holdout marker.
    read_only: bool = False
    #: C vs R: does the LLM add anything over the deterministic controller?
    llm_value: LlmValueComparison = LlmValueComparison()
    #: The no-dynamic-probe counterfactual and the probe-dependent subset.
    probe_dependence: ProbeDependence = ProbeDependence()
    probe_dependent_efficiency: dict[str, EfficiencyMetrics] = {}
    probe_dependent_gate: AgentGateResult | None = None
    #: Citation support per system (A, B, C, R) over each system's scoped runs.
    citation_by_system: dict[str, CitationSupport] = {}
    #: Model calls, investigator errors, dropped citations, fallbacks per system.
    trace_stats: dict[str, AgentTraceStats] = {}
    #: Policy branch counts per system (`policy_decision:<branch>:...` rows).
    policy_branches: dict[str, dict[str, int]] = {}
    #: Strong / apply_now per grid point, pooled and per era.
    grid: GridDistribution = GridDistribution()
    grid_by_era: dict[str, GridDistribution] = {}
    #: `[era][system] -> evidence_quality counts` (per-company era).
    era_evidence_quality: dict[str, dict[str, dict[str, int]]] = {}
    #: Cases / runs read per split; whether test was read.
    split_counts: SplitCounts = SplitCounts()
    holdout_markers_written: int = 0
    #: Company-split datasets: are the test companies absent from every
    #: non-test dataset in the database?
    company_holdout: CompanyHoldoutCheck = CompanyHoldoutCheck()

    def describe(self) -> str:
        systems = _ordered_systems(self.systems_run)
        lines = [
            f"evaluation report: dataset={self.dataset_id!r} "
            f"split_kind={self.split_kind} split_name={self.split_name} "
            f"allowed_splits={list(self.allowed_splits)} allow_test={self.allow_test}",
            f"  policy_version={self.policy_version} generated_at={self.generated_at}",
            "  systems: "
            + (
                " ".join(f"{name}={self.systems_run.get(name, '?')}" for name in systems)
                or "(none)"
            ),
        ]
        lines.append("  " + self.sample_sizes.describe().replace("\n", "\n  "))
        lines.append("  " + self.case_set.describe().replace("\n", "\n  "))
        for name in _ordered_systems(self.efficiency):
            lines.append("  " + self.efficiency[name].describe().replace("\n", "\n  "))
        for era in ("live-era", "archive-era"):
            per_era = self.efficiency_by_era.get(era, {})
            for name in _ordered_systems(per_era):
                lines.append(f"  [{era}] " + per_era[name].describe().replace("\n", "\n  "))
        lines.append("  " + self.data_quality.describe().replace("\n", "\n  "))
        lines.append("  " + self.agent_gate.describe().replace("\n", "\n  "))
        lines.append(
            "  live-era gate (informational): "
            + self.live_era_gate.describe().replace("\n", "\n  ")
        )
        lines.append("  " + self.llm_value.describe().replace("\n", "\n  "))
        lines.append("  " + self.probe_dependence.describe())
        lines.append("  " + self.grid.describe())
        split_line = ", ".join(
            f"{name}={count}" for name, count in sorted(self.split_counts.cases_by_split.items())
        )
        lines.append(
            f"  splits read: {split_line or '(none)'}; test cases read="
            f"{self.split_counts.test_cases_read}; excluded holdout="
            f"{self.split_counts.excluded_holdout}"
        )
        lines.append("  " + self.product_gate.describe().replace("\n", "\n  "))
        lines.append("  " + self.ranker.describe().replace("\n", "\n  "))
        if self.survival is None:
            lines.append(f"  survival: (not included) {self.survival_note}".rstrip())
        else:
            described = getattr(self.survival, "describe", None)
            text = described() if callable(described) else str(self.survival)
            lines.append("  survival (corpus-wide, NOT dataset-scoped):")
            lines.append("  " + str(text).replace("\n", "\n  "))
        if self.limitations:
            lines.append("  limitations:")
            lines.extend(f"    - {item}" for item in self.limitations)
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


# ---------------------------------------------------------------------------
# 4. Dataset / corpus bookkeeping
# ---------------------------------------------------------------------------


def _dataset_row(conn: sqlite3.Connection, dataset_id: str) -> sqlite3.Row | None:
    try:
        return conn.execute(
            "SELECT dataset_id, created_at, split_kind, split_name, postings, companies, cases "
            "FROM replay_datasets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - defensive (pre-v3 schema)
        return None


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:  # pragma: no cover - defensive
        return 0
    if row is None:
        return 0
    value = row[0]
    return int(value) if isinstance(value, int | float) else 0


def _sample_sizes(
    conn: sqlite3.Connection,
    dataset_row: sqlite3.Row | None,
    *,
    case_set: MetricsCaseSet,
    eras: EraBoundaries,
    scored: ScoredSample,
) -> SampleSizes:
    dataset_postings = int(dataset_row["postings"]) if dataset_row is not None else 0
    dataset_companies = int(dataset_row["companies"]) if dataset_row is not None else 0
    dataset_cases = int(dataset_row["cases"]) if dataset_row is not None else 0

    corpus_postings = _count(conn, "SELECT COUNT(*) FROM postings")
    corpus_companies = _count(conn, "SELECT COUNT(*) FROM companies")
    # A closure EVENT is a posting observed to have gone absent. spec.md §5
    # keeps closures interval-censored, so `first_seen_absent IS NOT NULL` is
    # the count of bracketed closures — there is no exact `closed_at` to
    # count instead, by design.
    corpus_closures = _count(
        conn, "SELECT COUNT(*) FROM postings WHERE first_seen_absent IS NOT NULL"
    )

    # spec.md §1's archive/live era split, PER COMPANY, over the same case
    # set every other section reads.
    live_era_cases = sum(1 for case in case_set.cases if eras.era_for_case(case) == "live-era")
    archive_era_cases = len(case_set.cases) - live_era_cases
    era_total = live_era_cases + archive_era_cases
    companies = set(eras.case_company.values())

    return SampleSizes(
        dataset_postings=dataset_postings,
        dataset_companies=dataset_companies,
        dataset_cases=dataset_cases,
        scored_postings=scored.postings,
        scored_companies=scored.companies,
        scored_cases=scored.cases,
        identity_unresolved_cases=scored.identity_unresolved_cases,
        unassigned_cases=scored.unassigned_cases,
        excluded_holdout_cases=scored.excluded_holdout_cases,
        corpus_postings=corpus_postings,
        corpus_companies=corpus_companies,
        corpus_closures=corpus_closures,
        meets_postings=scored.postings >= SPEC_TARGET_POSTINGS,
        meets_companies=scored.companies >= SPEC_TARGET_COMPANIES,
        meets_closures=corpus_closures >= SPEC_TARGET_CLOSURES,
        era_boundary=eras.earliest,
        era_companies_with_own=len(companies & set(eras.by_company)),
        era_companies_total=len(companies),
        live_era_cases=live_era_cases,
        archive_era_cases=archive_era_cases,
        live_era_share=(live_era_cases / era_total) if era_total else None,
    )


def _scoped_run_ids(conn: sqlite3.Connection, *, dataset_id: str, system: str) -> list[str]:
    """Run ids belonging to `(dataset_id, system)`.

    Dataset membership is `mode='replay'` AND a `config_hash` ENDING in
    `|dataset:<id>` — a Python `endswith`, not a SQL `LIKE`, so a dataset id
    containing `%` or `_` cannot accidentally widen the match, and so
    `rli.replay.build.case_state_at`'s `|case_state` inspection runs are
    excluded for the right reason rather than by luck.
    """
    suffix = f"|dataset:{dataset_id}"
    rows = conn.execute(
        "SELECT id, config_hash FROM runs WHERE mode = 'replay' AND system = ?",
        (system,),
    ).fetchall()
    return [row["id"] for row in rows if str(row["config_hash"] or "").endswith(suffix)]


def _note_holdout_runs(conn: sqlite3.Connection, run_ids: list[str]) -> int:
    """Append the `holdout_test_evaluated` marker to each run, at most once.

    Returns how many rows were actually inserted. Committed here rather than
    left to the caller: the point of the marker is that it survives even if
    the report generation that follows it fails.
    """
    inserted = 0
    stamp = to_utc_z(now_utc())
    for run_id in run_ids:
        existing = conn.execute(
            "SELECT 1 FROM run_steps WHERE run_id = ? AND decision_type = ? LIMIT 1",
            (run_id, HOLDOUT_TEST_STEP),
        ).fetchone()
        if existing is not None:
            continue
        row = conn.execute(
            "SELECT MAX(step_index) AS max_index FROM run_steps WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        current = row["max_index"] if row is not None else None
        # `Run.step` numbers steps from 1, so a run with no steps at all (which
        # a replay run never is — `replay_hook` writes `replay_dataset:<id>`
        # first) gets index 1 here rather than a 0 no other writer produces.
        next_index = int(current) + 1 if current is not None else 1
        conn.execute(
            """
            INSERT INTO run_steps
                (run_id, step_index, component, decision_type, error, created_at)
            VALUES (?, ?, 'controller', ?, ?, ?)
            """,
            (run_id, next_index, HOLDOUT_TEST_STEP, HOLDOUT_TEST_NOTE, stamp),
        )
        inserted += 1
    if inserted:
        conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# 5. System execution / reuse
# ---------------------------------------------------------------------------


def _c_runner(
    cfg: Config, llm: object | None, llm_factory: object | None
) -> tuple[object | None, str]:
    """`(runner, reason)` for System C — never making a MODEL call to decide.

    A live completion to "check" whether the endpoint works would be exactly
    the thing this function exists to avoid: it costs money on a paid
    endpoint and minutes on a local one. What it does instead is ask
    `rli.llm.client.endpoint_unavailable_reason`, which is config plus a
    bounded `GET {base_url}/models` — no model runs, no tokens are spent.

    That probe is a deliberate change from the key-only check this replaced.
    With a local endpoint as the default, "is there an API key" no longer
    answers the question at all: a local Ollama needs none, and the only
    thing that distinguishes "C can run" from "C cannot" is whether anything
    is actually listening.
    """
    if llm is None and llm_factory is None and endpoint_unavailable_reason(cfg) is not None:
        return None, C_NOT_RUN_REASON
    # Imported here, not at module scope: `rli.agent.loop` pulls the whole
    # investigator/controller stack, which an A/B-only evaluation never needs.
    from rli.agent.loop import make_system_c

    return make_system_c(llm=llm, llm_factory=llm_factory), ""  # type: ignore[arg-type]


def _run_or_reuse(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    system: str,
    runner: object | None,
    rerun: bool,
    limit_cases: int | None,
    collection_status_csv: str | Path | None,
    read_only: bool = False,
) -> str:
    """Replay `system` when it has no scoped runs (or `rerun`); else reuse.

    Returns the `systems_run` status string. `LookupError` from
    `run_replay` (a dataset with no cases) is allowed to propagate: it means
    the caller named a dataset that does not exist or was never built, and
    silently reporting zeros for it would be worse than failing. Under
    `read_only` nothing is replayed: an absent system is `READ_ONLY_SKIP`.
    """
    existing = _scoped_run_ids(conn, dataset_id=dataset_id, system=system)
    if existing and not rerun:
        return "reused"
    if read_only:
        return READ_ONLY_SKIP

    # Imported here so an evaluation that reuses everything never pays for
    # the replay stack (point-in-time views, the probe registry, the
    # archive adapters).
    from rli.replay.run import run_replay

    run_replay(
        conn,
        cfg,
        dataset_id=dataset_id,
        system=system,
        runner=runner,  # type: ignore[arg-type]
        limit_cases=limit_cases,
        collection_status_csv=collection_status_csv,
        replace=True,
    )
    return "ran"


# ---------------------------------------------------------------------------
# 6. Limitations
# ---------------------------------------------------------------------------


def _company_event_coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """`(companies with any event, companies in the corpus, events)`."""
    covered = _count(conn, "SELECT COUNT(DISTINCT company_id) FROM company_events")
    total = _count(conn, "SELECT COUNT(*) FROM companies")
    events = _count(conn, "SELECT COUNT(*) FROM company_events")
    return covered, total, events


def _render_by_system(counts: dict[str, int]) -> str:
    if not counts:
        return "(no systems)"
    return ", ".join(f"{name}={counts[name]}" for name in _ordered_systems(counts))


def _team_signal_limitation(
    cfg: Config,
    efficiency: dict[str, EfficiencyMetrics],
    policy_branches: dict[str, dict[str, int]],
) -> str:
    """The `team_signal` / high-tier / P4 statement, derived from config and the traces."""
    from rli.probes.team_signal import TeamSignalProbe

    name = TeamSignalProbe.name
    enabled = cfg.team_signal.enabled
    runs_by_system = {
        system: metrics.probe_counts.get(name, 0) for system, metrics in efficiency.items()
    }
    high_by_system = {
        system: metrics.probe_counts_by_tier.get("high", 0)
        for system, metrics in efficiency.items()
    }
    p4 = {
        system: branches.get("P4_repeated_repost", 0)
        for system, branches in policy_branches.items()
    }
    p5b = {
        system: branches.get("P5b_hiring_activity", 0)
        for system, branches in policy_branches.items()
    }
    text = (
        f"`{name}` is {'ENABLED' if enabled else 'DISABLED'} in this config "
        f"(`[team_signal].enabled = {str(enabled).lower()}`); it is the only "
        f"`{TeamSignalProbe.cost_tier}`-tier probe and the only source of "
        "`corroborating_hiring_signal`, which policy branches P4 (repeated unchanged repost -> "
        "skip) and P5b (hiring activity -> apply_now) need. Measured on this dataset's scoped "
        f"runs: `{name}` executions {_render_by_system(runs_by_system)}; high-tier probe "
        f"executions {_render_by_system(high_by_system)}; P4 fired {_render_by_system(p4)}; "
        f"P5b fired {_render_by_system(p5b)}."
    )
    if not enabled:
        text += " With the probe disabled, P4 and P5b are unreachable in every number here."
    elif not any(p4.values()):
        text += " P4 never fired, so no `skip` in this report comes from the repost branch."
    return text


def _limitations(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    sample_sizes: SampleSizes,
    systems_run: dict[str, str],
    allowed_splits: tuple[str, ...],
    holdout_runs_noted: int,
    holdout_cases: int,
    survival_note: str,
    data_quality_result: DataQuality,
    efficiency: dict[str, EfficiencyMetrics] | None = None,
    policy_branches: dict[str, dict[str, int]] | None = None,
    read_only: bool = False,
    llm_value: LlmValueComparison | None = None,
    probe_dependence_result: ProbeDependence | None = None,
    grid: GridDistribution | None = None,
    company_holdout: CompanyHoldoutCheck | None = None,
) -> tuple[str, ...]:
    """Every caveat, derived from the config and the data rather than hardcoded."""
    efficiency = efficiency or {}
    policy_branches = policy_branches or {}
    covered, total, events = _company_event_coverage(conn)
    era_total = sample_sizes.archive_era_cases + sample_sizes.live_era_cases
    items: list[str] = [
        "Archive-era cases are weak by construction. For a case whose T predates ITS "
        "company's first own board capture, the only observation available is a Wayback "
        "capture, so `board_snapshot` evidence is sparse, `source_quality='archive'`, and "
        f"often absent entirely. On this dataset {sample_sizes.archive_era_cases} of "
        f"{era_total} scoped cases are archive-era (per-company boundary; "
        f"{sample_sizes.era_companies_with_own}/{sample_sizes.era_companies_total} companies "
        "have any own capture); they are scored, and the pooled agreement figures average "
        "the two eras together.",
        _team_signal_limitation(cfg, efficiency, policy_branches),
        f"Company-event coverage: {covered}/{total} companies in this database carry any "
        f"`company_events` row ({events} event(s) in total). `company_events` is a "
        "medium-cost probe and a policy input; for a company with no row the "
        "material-negative-event and hiring-freeze inputs are False only when the probe's "
        "collection status says the company was searched, and UNKNOWN otherwise (spec.md "
        "§4: missing history never means flat hiring).",
        f"Sample sizes vs. spec.md §6 targets — built: {sample_sizes.dataset_postings} "
        f"postings, {sample_sizes.dataset_companies} companies, {sample_sizes.dataset_cases} "
        f"cases; SCORED: {sample_sizes.scored_postings} postings (target "
        f">={sample_sizes.target_postings}), {sample_sizes.scored_companies} companies (target "
        f">={sample_sizes.target_companies}), {sample_sizes.scored_cases} cases "
        f"({sample_sizes.identity_unresolved_cases} identity-unresolved, "
        f"{sample_sizes.unassigned_cases} unassigned and "
        f"{sample_sizes.excluded_holdout_cases} out-of-scope holdout case(s) excluded); "
        f"collection corpus: {sample_sizes.corpus_postings} postings, "
        f"{sample_sizes.corpus_companies} companies, {sample_sizes.corpus_closures} closure "
        f"events (target >={sample_sizes.target_closures}). The headline gate is judged on "
        "what was SCORED, and it is "
        + ("MET." if sample_sizes.headline_gate_met else "NOT MET.")
        + " The closure-event leg has no dataset-scoped equivalent (a replay dataset's "
        "unit is a (posting, T) grid point, not a closure) and is therefore corpus-wide.",
        "Probe cost POINTS and model DOLLARS are different units and are never summed. "
        "`run_steps.cost_usd` holds placeholder cost points on `component='probe'` rows "
        "and real USD on `component='model'` rows; `runs.total_cost_usd` adds them, "
        "which is why no single 'total cost' figure appears anywhere in this report.",
        SYSTEM_A_CAVEAT,
        "Latency is SUMMED STEP LATENCY (`runs.total_latency_ms`), a lower bound on "
        "wall-clock time: it excludes controller and scheduling overhead between steps.",
    ]

    if grid is not None and grid.strong_total:
        items.append(
            f"Where strong evidence sits in time: {grid.strong_at_build_time} of "
            f"{grid.strong_total} strong-evidence cases "
            f"({_pct(grid.strong_share_at_build_time)}) are BUILD-TIME cases (T at or after the "
            f"build start {grid.build_started_at}; {grid.build_time_cases} case(s) in all), and "
            f"only {grid.grid_points_with_strong} of {grid.grid_points} grid point(s) have any. "
            "apply_now at build time: "
            + ", ".join(
                f"{name} {grid.apply_now_at_build_time.get(name, 0)}/{count}"
                for name, count in sorted(grid.apply_now_total.items())
            )
            + "."
        )

    if probe_dependence_result is not None and probe_dependence_result.counterfactual_computed:
        pd = probe_dependence_result
        items.append(
            f"Only {pd.probe_dependent} of {pd.counterfactual_computed} cases "
            f"({_pct(pd.probe_dependent_share)}) are probe-dependent: on the rest, System "
            f"{pd.reference}'s action equals the action the frozen policy gives with NO "
            "dynamic-probe evidence. "
            + _outside_subset_sentence(pd)
            + " See 'Probe-dependent cases'."
        )

    # The C-vs-R verdict follows its status: "R is as good" only when the
    # comparison concluded so; an inconclusive or mixed one is stated as such.
    if llm_value is not None and llm_value.status != "not_run":
        items.append("C vs R (does the LLM add anything?): " + llm_value.verdict)

    if company_holdout is not None and company_holdout.applicable and not company_holdout.clean:
        items.insert(
            0,
            "**COMPANY HOLDOUT BREACHED.** "
            + company_holdout.describe()
            + ". Every figure in this report on this company-split dataset is therefore NOT "
            "a clean company-holdout result: those companies were available to development "
            "and tuning through the other dataset(s).",
        )

    c_status = systems_run.get("C", "")
    if C_NOT_RUN_REASON in c_status or not c_status:
        items.append(
            f"System C was {C_NOT_RUN_REASON} — the endpoint configured in "
            "`[llm].base_url` did not answer a liveness probe and no `llm`/`llm_factory` "
            "was supplied, so no model call was attempted. The spec.md §6 agent gate is "
            "therefore `not_run`, not "
            "`fail`: an ungraded candidate has not failed. Every System C figure "
            f"elsewhere in this report reads `{C_NOT_RUN_REASON}`."
        )

    if read_only:
        items.append(
            "This evaluation was READ-ONLY: no system was replayed and no holdout marker was "
            "written to the trace. Systems without runs on this dataset are absent from every "
            "figure (see 'Systems run')."
        )

    if "test" in allowed_splits and holdout_cases:
        marker = (
            f"no `{HOLDOUT_TEST_STEP}` marker was written (read-only evaluation)"
            if read_only
            else f"{holdout_runs_noted} run(s) had a `{HOLDOUT_TEST_STEP}` marker appended to "
            "their trace"
        )
        items.append(
            f"The final `test` holdout WAS read by this evaluation: {holdout_cases} scoped "
            f"case(s) fall in it (spec.md §6's final evaluation; `allow_test=True`), and "
            f"{marker}. Nothing downstream of this report may be tuned on these "
            "numbers. `rli.replay.build`'s build-time refusal of `split='test'` and "
            "`rli.eval.baseline`'s unconditional refusal are both untouched — this was "
            "a read-side permission over a dataset that already existed."
        )
    elif "test" in allowed_splits:
        items.append(
            "The read-side split filter PERMITTED the `test` holdout (`allow_test=True`), "
            "but no scoped case fell in it, so no holdout data was actually read and no "
            f"`{HOLDOUT_TEST_STEP}` marker was written. The evaluated dataset was drawn "
            "from a non-holdout split; the holdout remains untouched."
        )

    items.append(
        "The posting-behaviour (survival) section is CORPUS-WIDE, not dataset-scoped: "
        "`rli.eval.survival.behavior_report` has no dataset or split argument, so its "
        "curves describe every posting the collector has ever seen, including postings "
        "outside the evaluated split."
        # Only echo the note when it says something the sentence above does
        # not — i.e. when the summary is missing or was skipped.
        + (
            f" ({survival_note})"
            if survival_note and not survival_note.startswith("corpus-wide")
            else ""
        )
    )

    if data_quality_result.repost_match_precision is None:
        items.append(
            "Repost match precision is "
            f"{data_quality_result.repost_match_precision_note} — spec.md §4 requires it "
            "to be validated on a hand-checked sample of 50 matches, and until that file "
            "exists the repost-derived policy inputs carry an unquantified error rate."
        )

    if data_quality_result.leakage_violations:
        items.append(
            f"Future-leakage audit found {data_quality_result.leakage_violations} "
            f"violation(s) (spec.md §6 target: 0): "
            f"{_render(data_quality_result.leakage_counts)}. Every metric in this report "
            "that touches an affected run is suspect."
        )

    return tuple(items)


# ---------------------------------------------------------------------------
# 7. The orchestrator
# ---------------------------------------------------------------------------


def evaluate(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    allowed_splits: tuple[str, ...] | None = None,
    allow_test: bool = True,
    split_kind: Literal["temporal", "company"] | None = None,
    cutoff: datetime | None = None,
    validation_cutoff: datetime | None = None,
    with_c: bool = False,
    with_r: bool = False,
    llm: object | None = None,
    llm_factory: object | None = None,
    rerun: bool = False,
    read_only: bool = False,
    limit_cases: int | None = None,
    collection_status_csv: str | Path | None = None,
    match_precision_path: str | Path = DEFAULT_MATCH_PRECISION_PATH,
    include_survival: bool = True,
    ranker_config: RankerConfig | None = None,
) -> EvaluationReport:
    """Run the whole spec.md §6 evaluation over one replay dataset.

    Makes no network calls and never builds a dataset. The only execution it
    can trigger is `rli.replay.run.run_replay`, and only for a system with no
    scoped runs (or when `rerun=True`) — never under `read_only=True`.

    Arguments:
        dataset_id: the replay dataset to score. Must already exist —
            `LookupError` propagates from `run_replay` if it has no cases.
        allowed_splits: read-side split filter. `None` means
            `("dev", "validation", "test")` when `allow_test` (the default
            for a final evaluation), else `("dev", "validation")`.
        allow_test: permit reading the `test` holdout. Default `True`
            because this IS spec.md §6's final evaluation; see the module
            docstring for why the build-time interlocks are untouched.
        split_kind / cutoff / validation_cutoff: a deliberate RE-SPLIT over
            today's corpus. Left unset (the default), the split assignment is
            the one frozen on the dataset at build time (`replay_cases.split`,
            schema version 5) or, for an older dataset, reconstructed as of
            its build (`rli.eval.metrics.split_map_for_dataset`).
        with_c: also evaluate System C. Requires `llm` or `llm_factory`, or
            a reachable endpoint at `[llm].base_url`; otherwise C is recorded
            as `"skipped: not run: no LLM endpoint configured"` and no model
            call is attempted. Existing C runs are scored either way.
        with_r: replay System R (`rli.eval.system_r`, no LLM, offline) when
            the dataset has no R runs. Existing R runs are scored either way.
        rerun: force `run_replay(..., replace=True)` for every system.
        read_only: write NOTHING — no replay, no holdout marker. Systems with
            no runs are reported as `READ_ONLY_SKIP`.
        include_survival: include the corpus-wide posting-behaviour summary.
            `False` skips the lifelines import entirely.

    Never raises for corrupt stored data; a failing survival fit degrades to
    `survival=None` plus a `survival_note`.
    """
    if read_only and rerun:
        raise ValueError("rerun=True replays systems, which read_only=True forbids")

    resolved_splits = resolve_allowed_splits(allowed_splits, allow_test=allow_test)

    dataset_row = _dataset_row(conn, dataset_id)
    split_name = str(dataset_row["split_name"]) if dataset_row is not None else "(unknown)"

    splits, split_kind_used = split_map_for_dataset(
        conn,
        dataset_id=dataset_id,
        split_kind=split_kind,
        cutoff=cutoff,
        validation_cutoff=validation_cutoff,
    )
    frozen_splits = (
        split_kind is None
        and cutoff is None
        and validation_cutoff is None
        and _frozen_split_count(conn, dataset_id) > 0
    )

    # --- systems: run what is missing, reuse what is there ------------------
    systems_run: dict[str, str] = {}
    for system in ("A", "B"):
        systems_run[system] = _run_or_reuse(
            conn,
            cfg,
            dataset_id=dataset_id,
            system=system,
            runner=None,
            rerun=rerun,
            limit_cases=limit_cases,
            collection_status_csv=collection_status_csv,
            read_only=read_only,
        )

    has_c_runs = bool(_scoped_run_ids(conn, dataset_id=dataset_id, system="C"))
    if not with_c:
        # Even when C was not asked for, say whether it COULD have run: a
        # reader looking at an empty C column needs the reason, and "no API
        # key" is the reason that matters here. Never probed under
        # read_only (the probe is a network GET, and nothing would run).
        if has_c_runs:
            systems_run["C"] = "reused"
        else:
            reason = "" if read_only else _c_runner(cfg, llm, llm_factory)[1]
            suffix = f"; {reason}" if reason else ""
            systems_run["C"] = f"skipped: not requested (pass with_c=True / --with-c){suffix}"
    elif read_only:
        systems_run["C"] = "reused" if has_c_runs else READ_ONLY_SKIP
    else:
        c_runner, c_reason = _c_runner(cfg, llm, llm_factory)
        if c_runner is None:
            systems_run["C"] = f"skipped: {c_reason}"
        else:
            systems_run["C"] = _run_or_reuse(
                conn,
                cfg,
                dataset_id=dataset_id,
                system="C",
                runner=c_runner,
                rerun=rerun,
                limit_cases=limit_cases,
                collection_status_csv=collection_status_csv,
            )

    has_r_runs = bool(_scoped_run_ids(conn, dataset_id=dataset_id, system="R"))
    if with_r:
        systems_run["R"] = _run_or_reuse(
            conn,
            cfg,
            dataset_id=dataset_id,
            system="R",
            runner=None,
            rerun=rerun,
            limit_cases=limit_cases,
            collection_status_csv=collection_status_csv,
            read_only=read_only,
        )
    elif has_r_runs:
        systems_run["R"] = "reused"
    else:
        systems_run["R"] = (
            "skipped: not requested (pass with_r=True / --with-r; offline, no LLM) — "
            f"or `rli replay run --system R --dataset {dataset_id}`"
        )

    systems_evaluated = tuple(
        system
        for system in _SCORED_SYSTEMS
        if _scoped_run_ids(conn, dataset_id=dataset_id, system=system)
    )

    # --- case set -----------------------------------------------------------
    # The gate systems (A, B, C) are ALWAYS in `case_set.systems`, with zero
    # runs when absent. Every sub-case-set (era, probe-dependent) copies
    # `systems` verbatim, and `agent_gate` re-collects a POOLED case set
    # whenever a supplied one lacks a gate system — which would put pooled
    # baseline numbers under a subset's label whenever C has not run.
    case_set = collect_system_runs(
        conn,
        dataset_id=dataset_id,
        splits=splits,
        systems=tuple(dict.fromkeys(("A", "B", "C", *systems_evaluated))),
        allowed_splits=resolved_splits,
    )

    # --- era split (spec.md §1), PER COMPANY: informational ----------------
    eras = era_boundaries(conn, dataset_id=dataset_id, case_set=case_set)
    era_case_sets = split_case_set_by_company_era(case_set, eras)

    # --- the loud holdout marker -------------------------------------------
    holdout_runs_noted = 0
    holdout_cases = 0
    if "test" in resolved_splits:
        holdout_run_ids = [
            run.run_id
            for case in case_set.cases
            if case.split == "test"
            for run in case.runs.values()
        ]
        holdout_cases = sum(1 for case in case_set.cases if case.split == "test")
        if not read_only:
            holdout_runs_noted = _note_holdout_runs(conn, holdout_run_ids)

    # --- per-system efficiency (X vs A) ------------------------------------
    def _efficiency_over(scope: MetricsCaseSet) -> dict[str, EfficiencyMetrics]:
        return {
            system: agent_efficiency(
                conn,
                cfg,
                dataset_id=dataset_id,
                splits=splits,
                system=system,
                reference="A",
                allowed_splits=resolved_splits,
                case_set=scope,
            )
            for system in systems_evaluated
        }

    efficiency = _efficiency_over(case_set)

    # --- per-era, per-system efficiency (informational; see _era_split_section) --
    efficiency_by_era: dict[str, dict[str, EfficiencyMetrics]] = {
        era: _efficiency_over(era_case_sets[era]) for era in ("live-era", "archive-era")
    }
    era_evidence_quality: dict[str, dict[str, dict[str, int]]] = {}
    for era in ("live-era", "archive-era"):
        per_system: dict[str, dict[str, int]] = {}
        for system in systems_evaluated:
            counts: dict[str, int] = {}
            for case in era_case_sets[era].cases:
                run = case.runs.get(system)
                if run is None:
                    continue
                key = run.evidence_quality if run.evidence_quality is not None else "(missing)"
                counts[key] = counts.get(key, 0) + 1
            per_system[system] = counts
        era_evidence_quality[era] = per_system

    # --- data quality (scoped to A; see the module docstring) --------------
    dq_systems: tuple[str, ...] = (
        ("A",) if "A" in systems_evaluated else (systems_evaluated or ("A",))
    )
    dq = data_quality(
        conn,
        cfg,
        dataset_id=dataset_id,
        splits=splits,
        systems=dq_systems,
        allowed_splits=resolved_splits,
        case_set=case_set,
        match_precision_path=match_precision_path,
    )
    citation_by_system = {
        system: citation_support_for_runs(conn, case_set.run_ids.get(system, ()))
        for system in systems_evaluated
    }
    trace_stats = {
        system: agent_trace_stats(conn, system, case_set.run_ids.get(system, ()))
        for system in systems_evaluated
    }
    policy_branches = {
        system: policy_branch_counts(conn, case_set.run_ids.get(system, ()))
        for system in systems_evaluated
    }

    # --- gates --------------------------------------------------------------
    gate = agent_gate(
        conn,
        cfg,
        dataset_id=dataset_id,
        splits=splits,
        candidate="C",
        baseline="B",
        reference="A",
        allowed_splits=resolved_splits,
        candidate_metrics=efficiency.get("C"),
        baseline_metrics=efficiency.get("B"),
        case_set=case_set,
    )
    holdout_check = company_holdout_check(conn, dataset_id=dataset_id, split_kind=split_kind_used)
    extra_notes: list[str] = []
    if holdout_check.applicable and not holdout_check.clean:
        extra_notes.append(
            "COMPANY HOLDOUT BREACHED: "
            + holdout_check.describe()
            + ". This company-split result is NOT a clean company holdout."
        )
    if "C" not in efficiency:
        extra_notes.append(f"System C: {systems_run.get('C', C_NOT_RUN_REASON)}")
    if holdout_cases:
        marker = (
            "no trace marker was written (read-only evaluation)"
            if read_only
            else f"{holdout_runs_noted} run(s) were marked with `{HOLDOUT_TEST_STEP}` in the trace"
        )
        extra_notes.append(
            f"The final `test` holdout was read for this gate: {holdout_cases} scoped "
            f"case(s) are in the `test` split and {marker}. Nothing may be tuned on "
            "this verdict (spec.md §6)."
        )

    # --- does the LLM add anything? C vs R (NOT a spec gate) --------------
    r_gate: AgentGateResult | None = None
    if "R" in efficiency:
        r_gate = agent_gate(
            conn,
            cfg,
            dataset_id=dataset_id,
            splits=splits,
            candidate="R",
            baseline="B",
            reference="A",
            allowed_splits=resolved_splits,
            candidate_metrics=efficiency.get("R"),
            baseline_metrics=efficiency.get("B"),
            case_set=case_set,
            scope="System R as candidate",
        )
    c_errors = trace_stats.get("C")
    paired_cr_runs = [
        case.runs[name].run_id for case in case_set.cases_for("C", "R") for name in ("C", "R")
    ]
    llm_value = llm_value_comparison(
        case_set,
        run_costs=run_probe_costs(conn, paired_cr_runs),
        candidate_metrics=efficiency.get("C"),
        deterministic_metrics=efficiency.get("R"),
        investigator_error_run_ids=frozenset(
            c_errors.investigator_error_run_ids if c_errors is not None else ()
        ),
        candidate_gate_status=gate.status,
        deterministic_gate=r_gate,
    )
    if llm_value.status != "not_run":
        extra_notes.append(
            f"C vs R ({llm_value.status}; NOT a spec.md §6 gate): {llm_value.verdict}"
        )
    if extra_notes:
        gate = gate.model_copy(update={"notes": tuple(gate.notes) + tuple(extra_notes)})

    # --- informational live-era gate (spec.md §1 era split) -----------------
    # NOT the spec.md §6 verdict — the pooled `gate` above stays authoritative.
    live_era_gate = agent_gate(
        conn,
        cfg,
        dataset_id=dataset_id,
        splits=splits,
        candidate="C",
        baseline="B",
        reference="A",
        allowed_splits=resolved_splits,
        case_set=era_case_sets["live-era"],
        # Labels the result so `describe()` cannot echo this re-run under the
        # spec.md §6 banner.
        scope="live-era",
    )

    # --- probe-dependent cases (informational) ------------------------------
    dependence, dependent_set = probe_dependence(conn, cfg, case_set, reference="A")
    dependent_efficiency = _efficiency_over(dependent_set)
    dependent_gate = agent_gate(
        conn,
        cfg,
        dataset_id=dataset_id,
        splits=splits,
        candidate="C",
        baseline="B",
        reference="A",
        allowed_splits=resolved_splits,
        case_set=dependent_set,
        scope="probe-dependent",
    )

    outcomes_gate = product_gate(conn, splits=splits, system="A")

    # --- C2: learned probe ranking -----------------------------------------
    ranker = evaluate_ranker(
        conn,
        cfg,
        dataset_id=dataset_id,
        splits=splits,
        allowed_splits=resolved_splits,
        config=ranker_config,
        case_set=case_set,
        reference="A",
    )

    # --- survival (corpus-wide, lazily imported, never fatal) --------------
    survival: object | None = None
    survival_note = ""
    if include_survival:
        try:
            # Imported here, not at module scope: lifelines pulls pandas,
            # scipy and autograd — seconds to import, and tens of seconds to
            # fit on the real corpus.
            from rli.eval.survival import behavior_report

            survival = behavior_report(conn, cfg)
            survival_note = (
                "corpus-wide (all companies): rli.eval.survival.behavior_report takes no "
                "dataset or split scope, so these curves are NOT dataset-scoped"
            )
        except Exception as exc:  # noqa: BLE001 - one section must never cost the report
            survival = None
            survival_note = f"survival summary unavailable: {type(exc).__name__}: {exc}"
    else:
        survival_note = "survival summary not requested (include_survival=False)"

    scored = scored_sample(conn, case_set, eras)
    sample_sizes = _sample_sizes(conn, dataset_row, case_set=case_set, eras=eras, scored=scored)
    build_started_at = str(dataset_row["created_at"]) if dataset_row is not None else None
    grid = grid_distribution(case_set, systems=systems_evaluated, build_started_at=build_started_at)
    grid_by_era = {
        era: grid_distribution(
            era_case_sets[era], systems=systems_evaluated, build_started_at=build_started_at
        )
        for era in ("live-era", "archive-era")
    }

    return EvaluationReport(
        dataset_id=dataset_id,
        split_kind=split_kind_used,
        split_name=split_name,
        allowed_splits=resolved_splits,
        allow_test=allow_test,
        generated_at=to_utc_z(now_utc()),
        policy_version=policy_version(cfg),
        systems_run=systems_run,
        case_set=case_set,
        efficiency=efficiency,
        efficiency_by_era=efficiency_by_era,
        data_quality=dq,
        agent_gate=gate,
        live_era_gate=live_era_gate,
        product_gate=outcomes_gate,
        ranker=ranker,
        sample_sizes=sample_sizes,
        survival=survival,
        survival_note=survival_note,
        read_only=read_only,
        llm_value=llm_value,
        probe_dependence=dependence,
        probe_dependent_efficiency=dependent_efficiency,
        probe_dependent_gate=dependent_gate,
        citation_by_system=citation_by_system,
        trace_stats=trace_stats,
        policy_branches=policy_branches,
        grid=grid,
        grid_by_era=grid_by_era,
        era_evidence_quality=era_evidence_quality,
        split_counts=split_counts(case_set, frozen=frozen_splits),
        holdout_markers_written=holdout_runs_noted,
        company_holdout=holdout_check,
        limitations=_limitations(
            conn,
            cfg,
            sample_sizes=sample_sizes,
            systems_run=systems_run,
            allowed_splits=resolved_splits,
            holdout_runs_noted=holdout_runs_noted,
            holdout_cases=holdout_cases,
            survival_note=survival_note,
            data_quality_result=dq,
            efficiency=efficiency,
            policy_branches=policy_branches,
            read_only=read_only,
            llm_value=llm_value,
            probe_dependence_result=dependence,
            grid=grid,
            company_holdout=holdout_check,
        ),
    )


def _frozen_split_count(conn: sqlite3.Connection, dataset_id: str) -> int:
    """Cases of `dataset_id` whose split was frozen at build (`replay_cases.split`)."""
    try:
        return _count(
            conn,
            "SELECT COUNT(*) FROM replay_cases WHERE dataset_id = ? AND split IS NOT NULL",
            (dataset_id,),
        )
    except sqlite3.Error:  # pragma: no cover - pre-version-5 table
        return 0


# ---------------------------------------------------------------------------
# 8. Markdown report writer
# ---------------------------------------------------------------------------


def _headline_section(report: EvaluationReport) -> list[str]:
    sizes = report.sample_sizes
    table = _markdown_table(
        [
            "measure",
            "built (dataset row)",
            "SCORED (this evaluation)",
            "collection corpus",
            "spec §6 target",
            "met? (judged on scored)",
        ],
        [
            [
                "postings",
                str(sizes.dataset_postings),
                str(sizes.scored_postings),
                str(sizes.corpus_postings),
                f">= {sizes.target_postings}",
                _flag(sizes.meets_postings),
            ],
            [
                "companies",
                str(sizes.dataset_companies),
                str(sizes.scored_companies),
                str(sizes.corpus_companies),
                f">= {sizes.target_companies}",
                _flag(sizes.meets_companies),
            ],
            [
                "closure events",
                "n/a (a replay case is a (posting, T) grid point, not a closure)",
                "n/a",
                str(sizes.corpus_closures),
                f">= {sizes.target_closures}",
                _flag(sizes.meets_closures) + " (corpus-wide)",
            ],
            [
                "replay cases",
                str(sizes.dataset_cases),
                str(sizes.scored_cases),
                "—",
                "—",
                "—",
            ],
            [
                "excluded from scoring",
                "—",
                f"identity_unresolved={sizes.identity_unresolved_cases}, "
                f"unassigned={sizes.unassigned_cases}, "
                f"holdout not read={sizes.excluded_holdout_cases}",
                "—",
                "—",
                "—",
            ],
            [
                "live-era cases (T >= the company's first own capture)",
                "—",
                str(sizes.live_era_cases),
                "—",
                "—",
                "—",
            ],
            ["archive-era cases", "—", str(sizes.archive_era_cases), "—", "—", "—"],
            ["live-era share", "—", _pct(sizes.live_era_share), "—", "—", "—"],
        ],
    )
    verdict = "**MET**" if sizes.headline_gate_met else "**NOT MET**"
    return [
        "## Headline gate (sample sizes)",
        "",
        table,
        "",
        _era_boundary_line(sizes) + " See the 'Era split: live vs. archive' section below.",
        "",
        f"Headline gate (postings and companies judged on SCORED data): {verdict}.",
        "",
        "The size columns are NOT interchangeable. 'built' is what the replay dataset was "
        "built with; 'SCORED' is what this evaluation actually scored — cases that passed "
        "the split gate and whose posting identity resolved. The collection corpus may "
        "clear spec.md §6's targets while the scored slice does not. The closure-event leg "
        "has no dataset-scoped equivalent and is judged corpus-wide, which makes it the "
        "optimistic leg of this verdict.",
    ]


def _era_boundary_line(sizes: SampleSizes) -> str:
    if sizes.era_boundary is None:
        return "Era boundary: no own board snapshots yet — every case is archive-era."
    return (
        "Era boundary is PER COMPANY: a case is live-era only when its T is at or after "
        f"its own company's first own board capture. {sizes.era_companies_with_own} of "
        f"{sizes.era_companies_total} companies in scope have any own capture; the earliest "
        f"own capture anywhere is `{sizes.era_boundary}`."
    )


def _systems_section(report: EvaluationReport) -> list[str]:
    rows = []
    for name in _ordered_systems(report.systems_run):
        status = report.systems_run.get(name, "")
        runs = report.case_set.counts_by_system.get(name, 0)
        rows.append([name, status or "—", str(runs)])
    return [
        "## Systems run",
        "",
        _markdown_table(["system", "status", "scoped runs"], rows),
        "",
        "`reused` means the dataset already carried that system's replay runs and they "
        "were scored as-is; `ran` means `rli.replay.run.run_replay` was invoked (offline "
        "by construction — the replay net client raises on any live call). No dataset was "
        "built and no network call was made by this evaluation.",
    ]


def _case_set_section(report: EvaluationReport) -> list[str]:
    cs = report.case_set
    rows = [
        ["cases collected", str(len(cs.cases))],
        ["systems", ", ".join(cs.systems) or "(none)"],
        ["allowed splits (read-side)", ", ".join(cs.allowed_splits) or "(none)"],
        [
            "runs per system",
            _render(cs.counts_by_system),
        ],
        [
            "identity unresolved (no run resolved the posting; split from `replay_cases`)",
            f"{cs.identity_unresolved} ({_render(cs.identity_unresolved_by_split)})",
        ],
        ["unassigned (no posting / posting absent from the split map)", str(cs.unassigned)],
        ["excluded holdout (split outside the read-side filter)", str(cs.excluded_holdout)],
        ["duplicate re-runs collapsed to the latest", str(cs.duplicates_collapsed)],
    ]
    return [
        "## Case-set accounting",
        "",
        _markdown_table(["item", "value"], rows),
        "",
        "A case is `(input_url, replay_at)`. An unassignable posting is EXCLUDED rather "
        "than defaulted into a split — an unknown split could, for all this report knows, "
        "be the holdout.",
    ]


def _splits_section(report: EvaluationReport) -> list[str]:
    counts = report.split_counts
    systems = _ordered_systems(report.efficiency) or list(report.case_set.systems)
    rows = [
        [split, str(counts.cases_by_split.get(split, 0))]
        + [str(counts.runs_by_split.get(split, {}).get(name, 0)) for name in systems]
        for split in ("dev", "validation", "test")
    ]
    test_read = counts.test_cases_read
    if test_read:
        marker = (
            "no trace marker was written (read-only evaluation)"
            if report.read_only
            else f"{report.holdout_markers_written} new `{HOLDOUT_TEST_STEP}` marker(s) were "
            "written to the trace"
        )
        statement = (
            f"**The `test` holdout WAS read: {test_read} test-split case(s) are scored in this "
            f"report**, and {marker}. Nothing may be tuned on these numbers."
        )
    elif "test" in report.allowed_splits:
        statement = (
            "The `test` holdout was permitted by the read-side filter but NO test-split case "
            "is in this dataset, so no holdout data was read."
        )
    else:
        statement = (
            f"The `test` holdout was NOT read: it is outside the read-side filter, and "
            f"{counts.excluded_holdout} case(s) of this dataset were left out for that reason."
        )
    provenance = (
        "Split assignment: FROZEN per case at build time (`replay_cases.split`)."
        if counts.frozen
        else "Split assignment: RECONSTRUCTED (the dataset predates frozen splits, or a "
        "re-split was requested); see `rli.eval.metrics.split_map_for_dataset`."
    )
    check = report.company_holdout
    holdout_lines = [
        "Company holdout: " + check.describe() + ".",
        "",
        "Stable company holdout excluded at build time: "
        + (
            f"`{check.build_exclusion}`"
            if check.build_exclusion
            else "not recorded (the dataset predates schema version 6, so the stable "
            "company-hash test companies were NOT excluded from it)"
        )
        + ".",
        "",
    ]
    if check.applicable and not check.clean:
        holdout_lines.insert(0, "**COMPANY HOLDOUT BREACHED.**")
    return [
        "## Holdout and splits",
        "",
        provenance,
        "",
        *holdout_lines,
        _markdown_table(
            ["split", "cases read"] + [f"{name} runs" for name in systems],
            rows,
        ),
        "",
        f"Not read: {counts.excluded_holdout} case(s) in a split outside the filter "
        f"(`{', '.join(report.allowed_splits) or '(none)'}`), {counts.unassigned} unassigned. "
        f"Read but not scored (identity unresolved), by split: "
        f"{_render(counts.identity_unresolved_by_split)}.",
        "",
        statement,
    ]


def _era_split_section(report: EvaluationReport) -> list[str]:
    """spec.md §1's archive/live era split, per company, read standalone (informational).

    A case is live-era only when its T is at or after ITS company's first own
    board capture; before that the only observation is a Wayback capture.
    This section shows each era on its own, plus where strong evidence and
    apply_now sit on the replay grid. The pooled Agent gate section stays the
    authoritative spec.md §6 verdict; nothing here overrides it.
    """
    sizes = report.sample_sizes
    systems = _ordered_systems(report.efficiency)

    counts_line = (
        f"Cases: live-era={sizes.live_era_cases}, archive-era={sizes.archive_era_cases}, "
        f"live-era share={_pct(sizes.live_era_share)}."
    )

    lines = [
        "## Era split: live vs. archive",
        "",
        "Archive-era cases are weak by construction, not because of anything either "
        "system under test did. For any replay case whose `replay_at` predates its "
        "company's own board-snapshot collection, the only observation available is a "
        "Wayback capture, so `board_snapshot` evidence is sparse, `source_quality="
        "'archive'`, and often absent entirely (spec.md §1 classifies Wayback-only "
        "evidence as weak). The pooled agreement figures elsewhere in this report average "
        "the two eras together.",
        "",
        _era_boundary_line(sizes),
        "",
        counts_line,
        "",
    ]

    for era, subtitle in (
        ("live-era", " — product-relevant view"),
        ("archive-era", ""),
    ):
        lines.append(f"### `{era}`{subtitle}")
        lines.append("")
        if not systems:
            lines.append("(no systems evaluated)")
            lines.append("")
            continue
        for system in systems:
            metrics = report.efficiency_by_era.get(era, {}).get(system)
            quality = report.era_evidence_quality.get(era, {}).get(system, {})
            rows = [
                [
                    "action distribution",
                    _render(metrics.action_distribution) if metrics is not None else "(none)",
                ],
                ["evidence_quality distribution", _render(quality)],
                [
                    "overall agreement with A",
                    _pct(metrics.overall_agreement) if metrics is not None else _MISSING,
                ],
                [
                    "macro agreement with A",
                    _pct(metrics.macro_agreement) if metrics is not None else _MISSING,
                ],
                [
                    "medium/high probes per run",
                    _num(metrics.mean_medium_high_probes_per_run)
                    if metrics is not None
                    else _MISSING,
                ],
            ]
            lines.append(f"**{system}**")
            lines.append("")
            lines.append(_markdown_table(["measure", "value"], rows))
            lines.append("")

    lines += _grid_lines(report)

    gate = report.live_era_gate
    verdict = {
        "pass": "**PASS**",
        "fail": "**FAIL**",
        "not_run": "**NOT RUN**",
    }.get(gate.status, f"**{gate.status.upper()}**")

    lines.append("### Live-era gate (informational)")
    lines.append("")
    lines.append(
        "This is an INFORMATIONAL, NON-authoritative re-run of the spec.md §6 agent "
        "gate, scoped to live-era cases only, using the same three legs and "
        "thresholds. **The pooled 'Agent gate' section below remains the "
        "authoritative spec.md §6 verdict** — nothing here replaces it; this exists "
        "only to show whether that verdict would look different on the "
        "product-relevant (live-era) slice."
    )
    lines.append("")
    lines += _gate_leg_table(gate)
    lines.append("")
    lines.append(f"Live-era gate verdict (informational, NON-authoritative): {verdict}.")
    return lines


#: Grid points listed in full in the per-T table; the rest are summarised.
_MAX_GRID_ROWS = 40


def _grid_lines(report: EvaluationReport) -> list[str]:
    grid = report.grid
    lines = ["### Strong evidence and apply_now by grid point", ""]
    if not grid.grid_points:
        return lines + ["(no cases)", ""]
    systems = sorted(grid.apply_now_total)
    summary_rows = [
        [
            scope,
            str(dist.grid_points),
            f"{dist.build_time_cases} over {dist.build_time_grid_points} T value(s)",
            f"{dist.strong_at_build_time}/{dist.strong_total} "
            f"({_pct(dist.strong_share_at_build_time)})",
            str(dist.grid_points_with_strong),
            ", ".join(
                f"{name} {dist.apply_now_at_build_time.get(name, 0)}/"
                f"{dist.apply_now_total.get(name, 0)}"
                for name in systems
            )
            or "—",
        ]
        for scope, dist in (
            ("all cases", grid),
            ("live-era", report.grid_by_era.get("live-era", GridDistribution())),
            ("archive-era", report.grid_by_era.get("archive-era", GridDistribution())),
        )
    ]
    lines += [
        f"BUILD-TIME cases are those with T at or after the dataset's build start "
        f"(`{grid.build_started_at}`): a still-open posting's last grid point is dated by its "
        "own live observation during the build, so build-time cases carry one T per posting "
        f"({grid.build_time_cases} case(s) over {grid.build_time_grid_points} T value(s) here). "
        f"Strong = System {grid.quality_system}'s evidence quality, which every system shares "
        "on a case.",
        "",
        _markdown_table(
            [
                "scope",
                "grid points",
                "build-time cases",
                "strong at build time / all strong",
                "grid points with any strong",
                "apply_now at build time / all apply_now",
            ],
            summary_rows,
        ),
        "",
    ]
    interesting = [row for row in grid.rows if row.strong or any(row.apply_now.values())]
    shown = interesting[-_MAX_GRID_ROWS:]
    rows = [
        [row.replay_at, str(row.cases), str(row.strong)]
        + [str(row.apply_now.get(name, 0)) for name in systems]
        for row in shown
    ]
    lines += [
        "Grid points with any strong-evidence or apply_now case"
        + (
            f" (latest {_MAX_GRID_ROWS} of {len(interesting)} shown)"
            if len(interesting) > _MAX_GRID_ROWS
            else ""
        )
        + f"; {grid.grid_points - len(interesting)} grid point(s) have neither:",
        "",
        _markdown_table(
            ["T", "cases", f"strong ({grid.quality_system})"]
            + [f"apply_now ({name})" for name in systems],
            rows,
        ),
        "",
    ]
    return lines


def _gate_leg_table(gate: AgentGateResult) -> list[str]:
    return [
        _markdown_table(
            [
                "leg",
                f"candidate ({gate.candidate})",
                f"baseline ({gate.baseline})",
                "requirement",
                "pass?",
            ],
            [
                [
                    "medium/high probes per run",
                    _num(gate.candidate_medium_high_per_run),
                    _num(gate.baseline_medium_high_per_run),
                    f"ratio {_num(gate.probe_ratio)} <= {gate.probe_ratio_threshold:.2f}",
                    _flag(gate.probe_use_pass),
                ],
                [
                    f"overall agreement with {gate.reference}",
                    _pct(gate.candidate_overall_agreement),
                    _pct(gate.baseline_overall_agreement),
                    f">= {_pct(gate.overall_required)}",
                    _flag(gate.overall_pass),
                ],
                [
                    f"macro agreement with {gate.reference}",
                    _pct(gate.candidate_macro_agreement),
                    _pct(gate.baseline_macro_agreement),
                    f">= {_pct(gate.macro_required)}",
                    _flag(gate.macro_pass),
                ],
            ],
        )
    ]


def _action_distribution_section(report: EvaluationReport) -> list[str]:
    systems = _ordered_systems(report.efficiency)
    actions = sorted(
        {action for name in systems for action in report.efficiency[name].action_distribution}
    )
    rows = [
        [action]
        + [str(report.efficiency[name].action_distribution.get(action, 0)) for name in systems]
        for action in actions
    ]
    note = ""
    if "C" not in systems:
        note = (
            "\n\nSystem C has no action distribution: "
            f"{report.systems_run.get('C') or C_NOT_RUN_REASON}."
        )
    return [
        "## Action distributions",
        "",
        _markdown_table(["action"] + (systems or ["(no system)"]), rows),
        "",
        "spec.md §6 requires agreement to be read WITH the action distribution: a "
        "default-heavy policy posts high overall agreement trivially." + note,
    ]


def _agreement_section(report: EvaluationReport) -> list[str]:
    systems = _ordered_systems(report.efficiency)
    rows = [
        [
            name,
            str(report.efficiency[name].paired_cases),
            _pct(report.efficiency[name].overall_agreement),
            _pct(report.efficiency[name].macro_agreement),
        ]
        for name in systems
    ]
    lines = [
        "## Agreement with System A",
        "",
        _markdown_table(
            ["system", "paired cases (with A)", "overall agreement", "macro agreement"], rows
        ),
        "",
        "`A` compared against itself is trivially 100% and is shown so that A's own "
        "probe, cost and latency figures have a row in every table below.",
    ]
    if "C" not in systems:
        lines.append("")
        lines.append(f"System C: {C_NOT_RUN_REASON}.")

    lines += ["", "### Per-class agreement", ""]
    if not systems:
        lines.append("(no systems evaluated)")
    for name in systems:
        metrics = report.efficiency[name]
        rows = [
            [
                action,
                str(metrics.per_class_counts.get(action, 0)),
                _pct(metrics.per_class_agreement.get(action)),
            ]
            for action in sorted(metrics.per_class_agreement)
        ]
        lines.append(f"**{name} vs A** (classes are System A's actions)")
        lines.append("")
        lines.append(_markdown_table(["A action", "n (A count)", f"{name} agreement"], rows))
        lines.append("")

    lines += ["### Confusion matrices", ""]
    if not systems:
        lines.append("(no systems evaluated)")
    for name in systems:
        matrix = report.efficiency[name].confusion_matrix
        columns = sorted(set(matrix) | {other for row in matrix.values() for other in row})
        rows = [
            [ref] + [str(matrix.get(ref, {}).get(other, 0)) for other in columns] for ref in columns
        ]
        lines.append(f"**A action (row) -> {name} action (column)**")
        lines.append("")
        lines.append(_markdown_table([f"A \\ {name}"] + (columns or ["(none)"]), rows))
        lines.append("")
    if "C" not in systems:
        lines.append(f"System C has no confusion matrix: {C_NOT_RUN_REASON}.")
    return lines


def _outside_subset_sentence(pd: ProbeDependence) -> str:
    """What the non-probe-dependent cases say about agreement, from the counts."""
    outside = {name: count for name, count in pd.disagreements_outside_subset.items() if count}
    if not outside:
        return (
            f"On every case outside the subset every system agrees with {pd.reference} (no "
            "disagreement was found there), so overall/macro agreement is inflated by them."
        )
    return (
        f"Outside the subset some systems still disagree with {pd.reference} (cases, per "
        f"system: {_render(outside)}); every other case outside it is an agreement no probe "
        "could have changed, which inflates overall/macro agreement."
    )


def _probe_dependent_section(report: EvaluationReport) -> list[str]:
    pd = report.probe_dependence
    lines = [
        "## Probe-dependent cases (informative agreement)",
        "",
        "Agreement with A is only informative where a dynamic probe could have mattered. "
        "For every case the frozen policy is re-applied to System "
        f"{pd.reference}'s own ALWAYS-RUN evidence (resolver, board snapshot, archive board "
        "state, refresh match; dynamic-probe evidence removed; same T, same always-run "
        "failures). A case is PROBE-DEPENDENT when "
        f"{pd.reference}'s recorded action differs from that no-probe action. "
        + _outside_subset_sentence(pd),
        "",
        _markdown_table(
            ["measure", "value"],
            [
                [f"cases with a {pd.reference} run", str(pd.reference_cases)],
                ["no-probe action computed", str(pd.counterfactual_computed)],
                [
                    "probe-dependent cases",
                    f"{pd.probe_dependent} ({_pct(pd.probe_dependent_share)})",
                ],
                ["no-probe -> recorded action (probe-dependent)", _render(pd.transitions)],
                ["no-probe action distribution", _render(pd.counterfactual_distribution)],
                [
                    "self-check: runs with no dynamic probe reproduced",
                    f"{pd.selfcheck_matches}/{pd.selfcheck_runs}",
                ],
            ],
        ),
        "",
        f"The subset is defined from System {pd.reference}'s action ONLY. A case where "
        f"{pd.reference} equals the no-probe action but another system disagrees with "
        f"{pd.reference} is OUTSIDE the subset and is counted in the pooled figures only; "
        "such cases, per system: "
        f"{_render(pd.disagreements_outside_subset)}.",
        "",
        "The self-check re-derives the action for every scoped run (any system) that ran no "
        "dynamic probe; there the no-probe action must equal the recorded one, so anything "
        "short of all of them means the reconstruction is wrong and this section should not "
        "be trusted.",
        "",
    ]
    systems = _ordered_systems(report.probe_dependent_efficiency)
    rows = [
        [
            name,
            str(report.probe_dependent_efficiency[name].paired_cases),
            _pct(report.probe_dependent_efficiency[name].overall_agreement),
            _pct(report.probe_dependent_efficiency[name].macro_agreement),
            _pct(report.efficiency[name].overall_agreement) if name in report.efficiency else "—",
            _pct(report.efficiency[name].macro_agreement) if name in report.efficiency else "—",
        ]
        for name in systems
    ]
    lines += [
        "### Agreement with A on probe-dependent cases",
        "",
        _markdown_table(
            [
                "system",
                "paired probe-dependent cases",
                "overall (probe-dependent)",
                "macro (probe-dependent)",
                "overall (all cases)",
                "macro (all cases)",
            ],
            rows,
        ),
        "",
    ]
    gate = report.probe_dependent_gate
    if gate is not None:
        verdict = {"pass": "PASS", "fail": "FAIL", "not_run": "NOT RUN"}.get(
            gate.status, gate.status.upper()
        )
        lines += [
            "### Agent-gate legs on probe-dependent cases (informational)",
            "",
            "The spec.md §6 legs (C vs B, agreement with A) recomputed on the probe-dependent "
            "subset only. INFORMATIONAL: the pooled 'Agent gate' section stays the spec "
            "verdict.",
            "",
            *_gate_leg_table(gate),
            "",
            f"Probe-dependent legs (informational): **{verdict}**.",
        ]
    return lines


def _cost_section(report: EvaluationReport) -> list[str]:
    systems = _ordered_systems(report.efficiency)
    rows = []
    for name in systems:
        metrics = report.efficiency[name]
        cost = metrics.cost
        rows.append(
            [
                name,
                str(metrics.runs),
                str(cost.probe_steps),
                _num(cost.probe_cost_points),
                _num(cost.mean_probe_cost_points),
                str(cost.model_steps),
                f"${cost.model_cost_usd:.4f}",
                _MISSING
                if cost.mean_model_cost_usd is None
                else f"${cost.mean_model_cost_usd:.4f}",
                f"{cost.input_tokens}/{cost.output_tokens}",
                _num(metrics.total_latency_ms, 0),
                _num(metrics.mean_latency_ms, 0),
            ]
        )
    table = _markdown_table(
        [
            "system",
            "runs",
            "probe steps",
            "probe cost POINTS (total)",
            "probe cost POINTS (mean/run)",
            "model steps",
            "model cost USD (total)",
            "model cost USD (mean/run)",
            "tokens in/out",
            "latency ms (total)",
            "latency ms (mean/run)",
        ],
        rows,
    )
    lines = [
        "## Cost and latency (probe cost points and model dollars reported SEPARATELY)",
        "",
        table,
        "",
        "**These are two different units and are NEVER summed.** `run_steps.cost_usd` "
        "holds unitless placeholder cost POINTS on `component='probe'` rows (configured "
        "in `[probe_costs]`: low=1, medium=3, high=10) and REAL DOLLARS on "
        "`component='model'` rows. `runs.total_cost_usd` adds the two together, which is "
        "why it is not quoted anywhere in this report and why no combined 'total cost' "
        "column exists. A probe-heavy system and a model-heavy system are not comparable "
        "on one axis.",
        "",
        "Latency is SUMMED STEP LATENCY, a lower bound on wall-clock time: it excludes "
        "controller and scheduling overhead between steps.",
    ]
    if "C" not in systems:
        lines += ["", f"System C: {C_NOT_RUN_REASON} — no probe points, dollars or tokens."]
    return lines


def _failure_section(report: EvaluationReport) -> list[str]:
    systems = _ordered_systems(report.efficiency)
    rows = []
    for name in systems:
        metrics = report.efficiency[name]
        rows.append(
            [
                name,
                _render(metrics.probe_counts_by_tier),
                f"{metrics.medium_high_probe_steps} "
                f"({_num(metrics.mean_medium_high_probes_per_run)}/run)",
                str(metrics.repeated_calls),
                str(metrics.invalid_arguments),
                f"{metrics.recovered_runs}/{metrics.runs_with_failed_probe} "
                f"({_pct(metrics.recovery_rate)})",
                f"{metrics.early_stop_regret_cases}/{metrics.early_stop_opportunities} "
                f"({_pct(metrics.early_stop_regret_rate)})",
                f"{metrics.unnecessary_probe_steps} ({_pct(metrics.unnecessary_probe_rate)})",
                str(metrics.reference_extra_probes_no_action_change),
                str(metrics.decisions_missing),
            ]
        )
    lines = [
        "## Failure and efficiency metrics",
        "",
        _markdown_table(
            [
                "system",
                "probe steps by tier",
                "medium/high probe steps",
                "repeated calls",
                "invalid arguments",
                "recovered / runs with a failed probe",
                "early-stop regret cases / opportunities",
                "unnecessary probe steps",
                "A's extra probes that changed no action",
                "undecodable decisions",
            ],
            rows,
        ),
        "",
        "`repeated calls` and `invalid arguments` are controller-forbidden events: any "
        "nonzero value is a real finding, not noise.",
        "",
        "`unnecessary probe steps` is the LITERAL reading available in the trace — a "
        "dynamic probe execution that produced ZERO evidence rows for its own run. A "
        "probe whose evidence WAS recorded but did not move the action is not "
        "recoverable from the trace at all, so this number is a lower bound on wasted "
        "work, never an upper bound. The last column is the counterpart reading: dynamic "
        "probes the fuller reference system spent on cases where both systems ended up "
        "recommending the same action.",
    ]
    if "C" not in systems:
        lines += ["", f"System C: {C_NOT_RUN_REASON}."]
    return lines


def _citation_rows(citation: CitationSupport) -> list[str]:
    return [
        str(citation.runs_checked),
        str(citation.reasons_total),
        f"{citation.reasons_all_ids_exist} ({_pct(citation.id_existence_rate)})",
        str(citation.reasons_with_missing_ids),
        str(citation.reasons_with_no_ids),
        f"{citation.reasons_classified} / {citation.reasons_unclassified}",
        f"{citation.reasons_supported} / {citation.reasons_unsupported} "
        f"({_pct(citation.support_rate)})",
    ]


def _data_quality_section(report: EvaluationReport) -> list[str]:
    dq = report.data_quality
    rows = [
        ["runs checked", str(dq.runs_checked)],
        ["postings checked", str(dq.postings_checked)],
        [
            "ATS resolution rate",
            f"{dq.ats_resolved_runs}/{dq.runs_checked} ({_pct(dq.ats_resolution_rate)})",
        ],
        ["ATS distribution (distinct postings)", _render(dq.ats_distribution)],
        [
            "FIRST-publish coverage (dated `first_published`, ats_native/page_structured, "
            "after the first-published guard)",
            f"{dq.first_publish_runs}/{dq.runs_checked} ({_pct(dq.first_publish_coverage)}) — "
            f"{_render(dq.first_publish_by_source_quality)}",
        ],
        [
            "refresh / last-published coverage (`updated_at`, `last_published`, "
            "`refreshed_at`; says the posting moved, not when it first appeared)",
            f"{dq.refresh_runs}/{dq.runs_checked} ({_pct(dq.refresh_coverage)}) — "
            f"{_render(dq.refresh_by_claim_type)}",
        ],
        [
            "any publish-family claim (either of the above, by source quality)",
            f"{dq.publish_date_runs}/{dq.runs_checked} ({_pct(dq.publish_date_coverage)}) — "
            f"{_render(dq.publish_date_by_source_quality)}",
        ],
        [
            "repost match precision",
            (
                dq.repost_match_precision_note
                if dq.repost_match_precision is None
                else f"{_pct(dq.repost_match_precision)} ({dq.repost_match_precision_note})"
            ),
        ],
    ]
    citations = report.citation_by_system or ({dq.systems[0]: dq.citation} if dq.systems else {})
    citation_rows = [
        [name] + _citation_rows(citations[name]) for name in _ordered_systems(citations)
    ]
    lines = [
        "## Data quality",
        "",
        f"Scoped to system(s) `{', '.join(dq.systems) or '(none)'}` over splits "
        f"`{', '.join(dq.allowed_splits) or '(none)'}`. System A runs every probe, so "
        "scoping here to A measures the cached record at its fullest rather than "
        "penalising it for System B's deliberately narrower probe set.",
        "",
        _markdown_table(["measure", "value"], rows),
        "",
        "### Citation support, per system",
        "",
        _markdown_table(
            [
                "system",
                "runs",
                "reasons",
                "every cited id exists",
                "citing a missing id",
                "citing no id",
                "classified / unclassified",
                "supported / unsupported (of classified)",
            ],
            citation_rows,
        ),
        "",
        "Each system is scored over its own scoped runs. A, B and R publish the "
        "deterministic reasons (`rli.policy.explain_stub`); C publishes the LLM's reasons "
        "that survived the explanation guard, or the deterministic ones on a fallback. An "
        "UNCLASSIFIED reason matched no claim family: it is reported, never guessed at, and "
        "counts as neither supported nor unsupported.",
        "",
    ]
    lines += _trace_stats_lines(report)
    return lines


def _trace_stats_lines(report: EvaluationReport) -> list[str]:
    stats = report.trace_stats
    names = _ordered_systems(stats)
    lines = ["### Model calls, dropped citations and explanation fallbacks", ""]
    if not names:
        return lines + ["(no systems evaluated)"]
    rows = [
        [
            name,
            str(stats[name].runs),
            f"{stats[name].model_steps} ({stats[name].investigator_calls} investigator / "
            f"{stats[name].explanation_calls} explanation)",
            str(stats[name].investigator_error_runs),
            f"{stats[name].citation_invalid_runs} runs / {stats[name].citation_invalid_ids} ids",
            f"{stats[name].citation_unsupported_runs} runs / "
            f"{stats[name].citation_unsupported_reasons} reasons",
            f"{stats[name].fallback_runs} ({_render(stats[name].fallbacks_by_reason)})",
        ]
        for name in names
    ]
    lines += [
        _markdown_table(
            [
                "system",
                "runs",
                "model calls",
                "investigator-error runs",
                "LLM citations dropped: fabricated id",
                "LLM reasons dropped as unsupported",
                "explanation fallbacks (by reason)",
            ],
            rows,
        ),
        "",
        "Read from `run_steps`: `citation_invalid:<n>` / `citation_unsupported:<n>` rows "
        "(the explanation guard dropped LLM citations or whole reasons), "
        "`explanation_fallback:<why>` (the deterministic reasons were published instead; "
        "`cost_cap` / `latency_cap` mean the explanation call was never made), and "
        "`run_flag:investigator_error` (or the older "
        "`controller_decision:stop:investigator_error`). An investigator-error run still "
        "completes: the controller stops and the frozen policy decides on the evidence so "
        "far. Such runs are INCLUDED in the per-system figures (agreement with A, action "
        "distributions, probe counts, cost, citation support), in the spec.md §6 agent gate "
        "and in the era and probe-dependent views. They are EXCLUDED from the C-vs-R rates "
        "(probe use and agreement per compared case), where they are counted separately as "
        "C failures and, at 10 or more, as a material loss on investigator reliability.",
    ]
    return lines


def _leakage_section(report: EvaluationReport) -> list[str]:
    from rli.replay.leakage import VIOLATION_KINDS

    dq = report.data_quality
    kinds = list(VIOLATION_KINDS) + sorted(
        kind for kind in dq.leakage_counts if kind not in VIOLATION_KINDS
    )
    return [
        "## Future leakage",
        "",
        _markdown_table(
            ["measure", "value"],
            [
                ["violations (spec.md §6 target: 0)", str(dq.leakage_violations)],
                ["clean", _flag(dq.leakage_clean)],
            ],
        ),
        "",
        _markdown_table(
            ["violation kind", "count"],
            [[f"`{kind}`", str(dq.leakage_counts.get(kind, 0))] for kind in kinds],
        ),
        "",
        _markdown_table(
            ["reported, not counted as a violation", "count"],
            [
                [
                    "model cache misses (spec.md §6 allows live LLM calls on a miss)",
                    str(dq.leakage_model_cache_misses),
                ],
                [
                    "blob input exposures (a served `data` blob answers a policy input no "
                    "claim backs at T; needs a dataset rebuild)",
                    str(dq.leakage_blob_input_exposures),
                ],
                [
                    "pre-fix capture batches with no recorded snapshot run window "
                    "(capture_fetched_after_t cannot see them; run `rli import-run-windows`)",
                    str(dq.leakage_uncovered_capture_batches),
                ],
            ],
        ),
        "",
        "`evidence_fetched_after_t` counts evidence that passed the `available_at <= T` gate "
        "although the fetch behind it completed after T (checked against `tool_cache`); "
        "`evidence_after_t` trusts the stamp. `capture_fetched_after_t` counts cases whose T "
        "falls inside a pre-fix daily snapshot run window in which their company was captured "
        "(the capture is stamped with the run's start). A replayed system reaches the "
        "network never: "
        "`rli.replay.mode.ReplayNetClient` raises on any attempt and the violation is "
        "written into the trace, so this audit counts recorded attempts rather than "
        "inferring them.",
    ]


def _survival_section(report: EvaluationReport) -> list[str]:
    lines = [
        "## Posting behaviour (survival summary)",
        "",
        "**Corpus-wide, NOT dataset-scoped.** `rli.eval.survival.behavior_report` takes "
        "no dataset or split argument — its only scope is a single company — so these "
        "curves describe every posting the collector has ever seen, including postings "
        "outside the evaluated split. Do not read them as properties of the dataset "
        "scored above.",
        "",
    ]
    if report.survival is None:
        lines.append(f"(no survival summary: {report.survival_note or 'not included'})")
        return lines

    survival = report.survival
    rows = [
        ["total intervals", str(getattr(survival, "total_intervals", _MISSING))],
        ["closed (interval-censored)", str(getattr(survival, "closed_count", _MISSING))],
        ["right-censored (still open)", str(getattr(survival, "right_censored_count", _MISSING))],
        ["archive-only observations", str(getattr(survival, "archive_only_count", _MISSING))],
    ]
    lines.append(_markdown_table(["measure", "value"], rows))
    lines.append("")

    curve_rows = []
    for label, attribute in (
        ("right-censored (Kaplan-Meier)", "right_censored_curve"),
        ("interval-censored", "interval_censored_curve"),
    ):
        curve = getattr(survival, attribute, None)
        if curve is None:
            continue
        curve_rows.append(
            [
                label,
                str(getattr(curve, "n_observations", _MISSING)),
                str(getattr(curve, "n_events", _MISSING)),
                str(getattr(curve, "n_censored", _MISSING)),
                _num(getattr(curve, "median_days", None), 1),
                str(getattr(curve, "note", "") or ""),
            ]
        )
    lines.append(
        _markdown_table(["curve", "n", "events", "censored", "median days", "note"], curve_rows)
    )
    lines.append("")
    lines.append(
        "Closures are interval-censored by construction (spec.md §4/§5: an exact "
        "`closed_at` is never invented), so the two curves answer slightly different "
        "questions and are shown side by side rather than merged."
    )
    if report.survival_note:
        lines += ["", f"_{report.survival_note}_"]
    return lines


def _agent_gate_section(report: EvaluationReport) -> list[str]:
    gate = report.agent_gate
    rows = [
        [
            "medium/high probes per run",
            _num(gate.candidate_medium_high_per_run),
            _num(gate.baseline_medium_high_per_run),
            f"ratio {_num(gate.probe_ratio)} <= {gate.probe_ratio_threshold:.2f}",
            _flag(gate.probe_use_pass),
        ],
        [
            "overall agreement with A",
            _pct(gate.candidate_overall_agreement),
            _pct(gate.baseline_overall_agreement),
            f">= {_pct(gate.overall_required)}",
            _flag(gate.overall_pass),
        ],
        [
            "macro agreement with A",
            _pct(gate.candidate_macro_agreement),
            _pct(gate.baseline_macro_agreement),
            f">= {_pct(gate.macro_required)}",
            _flag(gate.macro_pass),
        ],
    ]
    verdict = {
        "pass": "**PASS**",
        "fail": "**FAIL**",
        "not_run": "**NOT RUN**",
    }.get(gate.status, f"**{gate.status.upper()}**")

    lines = [
        "## Agent gate",
        "",
        f"spec.md §6: System {gate.candidate} must use <= "
        f"{gate.probe_ratio_threshold:.0%} of System {gate.baseline}'s medium/high-cost "
        f"probes while staying within {gate.agreement_margin:.0%} of "
        f"{gate.baseline}'s agreement with System {gate.reference}, overall AND "
        "macro-averaged.",
        "",
        f"Verdict: {verdict} (candidate runs: {gate.candidate_runs}, baseline runs: "
        f"{gate.baseline_runs}).",
        "",
        "Whether a pass is the LLM's doing or the deterministic controller's is answered "
        "by 'Does the LLM add anything? (System C vs System R)' below"
        + (
            f" ({report.llm_value.status}): {report.llm_value.verdict}"
            if report.llm_value.status != "not_run"
            else "."
        ),
        "",
        "> **System A structural caveat** — printed here, beside the verdict rather than "
        "in a footnote, because a probe-count comparison read without it is misleading.",
        ">",
        "> " + SYSTEM_A_CAVEAT,
        "",
        _markdown_table(
            [
                "leg",
                f"candidate ({gate.candidate})",
                f"baseline ({gate.baseline})",
                "requirement",
                "pass?",
            ],
            rows,
        ),
        "",
    ]
    if gate.status == "not_run":
        lines += [
            f"The gate is `not_run`, not `fail`: System {gate.candidate} produced zero "
            f"scoped runs ({report.systems_run.get('C', C_NOT_RUN_REASON)}). An ungraded "
            f"candidate has not failed. All candidate columns above read "
            f"`{C_NOT_RUN_REASON}`.",
            "",
        ]
    lines.append("Notes:")
    lines.append("")
    for note in gate.notes:
        lines.append(f"- {note}")
    if not gate.notes:
        lines.append("- (none)")
    return lines


def _llm_value_section(report: EvaluationReport) -> list[str]:
    cmp = report.llm_value
    title = {
        "not_run": "**NOT COMPARED**",
        "inconclusive": "**INCONCLUSIVE**",
        "llm_adds_nothing": "**THE LLM ADDS NOTHING MEASURABLE**",
        "llm_better": "**C BEATS R**",
        "mixed": "**MIXED**",
    }.get(cmp.status, f"**{cmp.status.upper()}**")
    lines = [
        "## Does the LLM add anything? (System C vs System R)",
        "",
        "System R runs System C's controller, eligibility gate, ranking and budgets with no "
        "LLM: its investigator step proposes every eligible probe in rank order and its "
        "explanation is the deterministic one. Anything C does better than R is the LLM's "
        "contribution. This comparison is NOT a spec.md §6 gate (the agent gate above is "
        "kept exactly as specified); it is what spec.md §6's \"If rules are equally good and "
        'simpler, remove the agent" needs.',
        "",
        f"Result: {title}.",
        "",
        cmp.verdict or "(no verdict)",
        "",
    ]
    lines += [cmp.materiality_rule(), ""]
    if cmp.status == "not_run" and not cmp.paired_cases:
        return lines
    rows = [
        ["scoped runs (all)", str(cmp.candidate_runs), str(cmp.deterministic_runs)],
        [
            "medium/high probe steps per compared case",
            _num(cmp.candidate_medium_high_per_case),
            _num(cmp.deterministic_medium_high_per_case),
        ],
        [
            "probe cost POINTS per compared case",
            _num(cmp.candidate_probe_points_per_case),
            _num(cmp.deterministic_probe_points_per_case),
        ],
        [
            "compared cases where this system used fewer medium/high probes",
            str(cmp.candidate_cheaper_cases),
            str(cmp.deterministic_cheaper_cases),
        ],
        [
            f"overall agreement with {cmp.reference} (compared cases)",
            _pct(cmp.candidate_overall_agreement),
            _pct(cmp.deterministic_overall_agreement),
        ],
        [
            f"macro agreement with {cmp.reference} (compared cases)",
            _pct(cmp.candidate_macro_agreement),
            _pct(cmp.deterministic_macro_agreement),
        ],
        [
            f"compared cases where ONLY this system agrees with {cmp.reference}",
            str(cmp.candidate_only_agrees_cases),
            str(cmp.deterministic_only_agrees_cases),
        ],
        [
            "model calls (all runs)",
            str(cmp.candidate_model_steps),
            str(cmp.deterministic_model_steps),
        ],
        [
            "model cost USD (all runs)",
            f"${cmp.candidate_model_cost_usd:.4f}",
            f"${cmp.deterministic_model_cost_usd:.4f}",
        ],
        [
            "tokens in/out (all runs)",
            f"{cmp.candidate_input_tokens}/{cmp.candidate_output_tokens}",
            "0/0",
        ],
    ]
    lines += [
        _markdown_table(["measure", f"C ({cmp.candidate})", f"R ({cmp.deterministic})"], rows),
        "",
        f"Material wins for C: {', '.join(cmp.won) or '(none)'}. Material losses for C: "
        f"{', '.join(cmp.lost) or '(none)'}. Relative medium/high probe saving by C: "
        f"{_pct(cmp.relative_probe_saving)}.",
        "",
        _markdown_table(
            ["paired cases (C and R both ran)", "value"],
            [
                ["paired cases", str(cmp.paired_cases)],
                [
                    "C investigator-error cases (C FAILURES; excluded from the rates above)",
                    str(cmp.candidate_investigator_error_cases),
                ],
                ["compared cases (rates above)", str(cmp.compared_cases)],
                ["same dynamic-probe sequence", str(cmp.same_sequence_cases)],
                ["same action", f"{cmp.same_action_cases} ({_pct(cmp.action_agreement)})"],
                ["same sequence AND same action", str(cmp.same_sequence_and_action_cases)],
                ["differing cases", str(cmp.differing_cases_total)],
                [
                    "differing cases that are C investigator-error runs",
                    str(cmp.differing_with_investigator_error),
                ],
            ],
        ),
        "",
    ]
    r_gate = cmp.deterministic_gate
    if r_gate is not None:
        lines += [
            "### The spec.md §6 legs with R as the candidate (informational)",
            "",
            *_gate_leg_table(r_gate),
            "",
            f"R vs B on the same legs: **{r_gate.status.upper()}** (C vs B: "
            f"**{cmp.candidate_gate_status.upper()}**).",
            "",
        ]
    if cmp.differences:
        rows = [
            [
                diff.replay_at,
                diff.input_url,
                diff.split,
                " > ".join(diff.candidate_probes) or "(none)",
                " > ".join(diff.deterministic_probes) or "(none)",
                str(diff.candidate_action),
                str(diff.deterministic_action),
                str(diff.reference_action),
                "yes" if diff.candidate_investigator_error else "",
            ]
            for diff in cmp.differences
        ]
        lines += [
            "### Per-case differences"
            + (
                f" (first {len(cmp.differences)} of {cmp.differing_cases_total})"
                if cmp.differing_cases_total > len(cmp.differences)
                else ""
            ),
            "",
            _markdown_table(
                [
                    "T",
                    "url",
                    "split",
                    "C probes",
                    "R probes",
                    "C action",
                    "R action",
                    "A action",
                    "C investigator error",
                ],
                rows,
            ),
        ]
    else:
        lines.append("No paired case differs in probe sequence or action.")
    return lines


def _product_gate_section(report: EvaluationReport) -> list[str]:
    gate = report.product_gate
    verdict = {
        "pass": "**PASS**",
        "fail": "**FAIL**",
        "unproven": "**UNPROVEN**",
    }.get(gate.status, f"**{gate.status.upper()}**")
    rows = [
        ["outcomes recorded", str(gate.outcomes_total)],
        ["postings with outcomes", str(gate.postings_with_outcomes)],
        ["postings matched to a run", str(gate.postings_matched_to_a_run)],
        ["held-out postings", str(gate.held_out_postings)],
        ["outcome counts", _render(gate.outcome_counts)],
        ["minimum outcomes required", str(gate.min_outcomes_required)],
        ["recommended actions", ", ".join(gate.recommended_actions) or "(none)"],
        [
            "effort per screen (recommended vs comparison)",
            f"{_num(gate.recommended_effort_per_screen)} vs "
            f"{_num(gate.comparison_effort_per_screen)}",
        ],
        [
            "effort per interview (recommended vs comparison)",
            f"{_num(gate.recommended_effort_per_interview)} vs "
            f"{_num(gate.comparison_effort_per_interview)}",
        ],
    ]
    by_action_rows = [
        [
            action,
            str(stats.postings),
            str(stats.applied),
            str(stats.screen),
            str(stats.interview),
            str(stats.offer),
            str(stats.rejection),
            str(stats.silence),
            _num(stats.effort_per_screen),
            _num(stats.effort_per_interview),
        ]
        for action, stats in sorted(gate.by_action.items())
    ]
    return [
        "## Product gate",
        "",
        "spec.md §6: on held-out postings, following the recommended action must lower "
        "applications per screen or per interview versus the comparison group.",
        "",
        f"Verdict: {verdict}.",
        "",
        f"Reason: {gate.reason or '(none recorded)'}",
        "",
        _markdown_table(["measure", "value"], rows),
        "",
        "### Outcomes by recommended action",
        "",
        _markdown_table(
            [
                "action",
                "postings",
                "applied",
                "screen",
                "interview",
                "offer",
                "rejection",
                "silence",
                "applied/screen",
                "applied/interview",
            ],
            by_action_rows,
        ),
        "",
        "`unproven` is not `fail`. A gate with no outcome data has not been failed, it "
        "has not been run; `fail` is reserved for a gate that had enough data and lost.",
    ]


#: What each `RankerResult.status` means for a reader who did not write the
#: ranker. Kept beside the writer because the report must EXPLAIN a status,
#: not just print it — "degenerate" tells a reader nothing on its own.
_RANKER_STATUS_NOTES = {
    "insufficient_data": (
        "`insufficient_data` means the learned ranker was NOT trained: below the "
        "configured row floor, scikit-learn is never even imported. That is the honest "
        "state for a small dataset — a model fitted on a few dozen rows would be a worse "
        "artifact than no model."
    ),
    "degenerate": (
        "`degenerate` means there were enough rows to try, but one side of the temporal "
        "split carried a single label class, so AUC is undefined and no comparison "
        "against the deterministic ranking is possible. Nothing is kept. This is a "
        "property of the proxy label on this dataset, not evidence that a learned "
        "ranking cannot help."
    ),
    "trained": (
        "`trained` means the learned ranking was fitted and scored against the "
        "deterministic `-probe_cost_points` ordering on a temporal holdout. It is kept "
        "only if it beat that ordering by the configured AUC and accuracy margins — a "
        "learned model that merely ties is extra machinery for no gain."
    ),
}


def _ranker_section(report: EvaluationReport) -> list[str]:
    ranker = report.ranker
    rows = [
        ["status", ranker.status],
        ["rows (candidate probe decisions)", str(ranker.rows)],
        ["train / holdout rows", f"{ranker.train_rows} / {ranker.holdout_rows}"],
        [
            "positive rate (train / holdout)",
            f"{_pct(ranker.positive_rate)} / {_pct(ranker.holdout_positive_rate)}",
        ],
        ["features", ", ".join(ranker.feature_names) or "(none)"],
        [
            "learned AUC / accuracy",
            f"{_num(ranker.learned_auc, 3)} / {_num(ranker.learned_accuracy, 3)}",
        ],
        [
            "deterministic AUC / accuracy",
            f"{_num(ranker.deterministic_auc, 3)} / {_num(ranker.deterministic_accuracy, 3)}",
        ],
        [
            "gain (AUC / accuracy)",
            f"{_num(ranker.auc_gain, 3)} / {_num(ranker.accuracy_gain, 3)}",
        ],
        ["keep the learned ranker?", _flag(ranker.keep)],
    ]
    return [
        "## C2 — learned probe ranking",
        "",
        "spec.md §6's C2 is System C with a learned probe ranking substituted for the "
        "deterministic one. The label here is a PROXY built from the traces "
        '("did running this probe move the partial action toward the reference '
        'action?"), not a ground-truth utility; the deterministic comparison score is '
        "`-probe_cost_points`, which is exactly what "
        "`rli.agent.controller.rank_candidates` falls back to once value is tied.",
        "",
        _markdown_table(["measure", "value"], rows),
        "",
        f"Note: {ranker.note or '(none)'}",
        "",
        _RANKER_STATUS_NOTES.get(
            ranker.status,
            "A status outside {insufficient_data, degenerate, trained} is not one this "
            "writer knows how to explain; read `Note:` above.",
        ),
    ]


def _limitations_section(report: EvaluationReport) -> list[str]:
    lines = ["## Limitations", ""]
    if not report.limitations:
        lines.append("- (none recorded)")
        return lines
    lines.extend(f"- {item}" for item in report.limitations)
    return lines


def write_evaluation_report(path: str | Path, report: EvaluationReport) -> Path:
    """Write `report` as Markdown to `path`, creating parent directories as needed.

    Follows `rli.eval.baseline.write_baseline_report`'s convention (accept a
    `str | Path`, create the parent, build `lines`, one `write_text`, return
    the `Path`) and adds one hard guarantee: this function must never raise
    for ANY `EvaluationReport`, including an entirely empty one. A report
    that cannot be written is a report that cannot be reviewed, and the state
    most worth reviewing — nothing ran, nothing was gradeable — is exactly
    the state a fragile writer would fail on.
    """
    destination = Path(path)
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "# Evaluation report (spec.md §6 / PLAN.md M6)",
        "",
        f"Dataset: `{report.dataset_id}` (`split_name={report.split_name}`, "
        f"`split_kind={report.split_kind}`) · Splits read: "
        f"`{', '.join(report.allowed_splits) or '(none)'}` · "
        f"`allow_test={report.allow_test}` · Policy version: "
        f"`{report.policy_version}` · Generated: `{report.generated_at}`",
        "",
    ]

    for section in (
        _headline_section,
        _systems_section,
        _case_set_section,
        _splits_section,
        _era_split_section,
        _action_distribution_section,
        _agreement_section,
        _probe_dependent_section,
        _cost_section,
        _failure_section,
        _data_quality_section,
        _leakage_section,
        _survival_section,
        _agent_gate_section,
        _llm_value_section,
        _product_gate_section,
        _ranker_section,
        _limitations_section,
    ):
        lines.extend(section(report))
        lines.append("")

    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination
