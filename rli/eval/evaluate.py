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

* **"System C was not run" is a first-class state, not a gap.** There is no
  `ANTHROPIC_API_KEY` in this environment, and this module will not attempt a
  live model call to discover that. When `with_c` is requested without an
  explicit `llm` / `llm_factory` and without a key in the environment, C's
  status is `"skipped: not run: no API key"`, the agent gate's status is
  `"not_run"` (not `"fail"` — an ungraded candidate has not failed), and the
  literal phrase `not run: no API key` is printed next to every place a C
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
  systems do, so an A-less dataset still produces a report.

* **Two sample-size answers are reported, and the headline gate is judged on
  the smaller one.** spec.md §6's targets (>=300 postings, >=40 companies,
  >=100 closure events) are about the evidence behind the conclusions. The
  collection corpus can clear them comfortably while the replay dataset that
  was actually scored is a far smaller slice of it, and quoting the corpus
  number as though the evaluation had that much backing would be the single
  most misleading thing this report could do. So `SampleSizes` carries both,
  the writer puts them in adjacent columns, and `meets_postings` /
  `meets_companies` are computed on the EVALUATED dataset.
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

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.gates import AgentGateResult, ProductGateResult, agent_gate, product_gate
from rli.eval.metrics import (
    DEFAULT_MATCH_PRECISION_PATH,
    SYSTEM_A_CAVEAT,
    DataQuality,
    EfficiencyMetrics,
    MetricsCaseSet,
    agent_efficiency,
    collect_system_runs,
    data_quality,
    resolve_allowed_splits,
    split_map_for_dataset,
)
from rli.eval.ranker import RankerConfig, RankerResult, evaluate_ranker
from rli.models.time import now_utc, to_utc_z
from rli.policy.action import policy_version

__all__ = [
    "C_NOT_RUN_REASON",
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
C_NOT_RUN_REASON = "not run: no API key"

_SYSTEM_ORDER = ("A", "B", "C", "C2")

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
    """spec.md §6's sample-size targets, answered twice over.

    `dataset_*` describes the replay dataset that was ACTUALLY scored;
    `corpus_*` describes the whole collection corpus the dataset was drawn
    from. The two routinely differ by an order of magnitude, and the
    `meets_*` flags follow the evaluated dataset wherever a dataset-scoped
    figure exists — see the module docstring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_postings: int = 0
    dataset_companies: int = 0
    dataset_cases: int = 0

    corpus_postings: int = 0
    corpus_companies: int = 0
    corpus_closures: int = 0

    target_postings: int = SPEC_TARGET_POSTINGS
    target_companies: int = SPEC_TARGET_COMPANIES
    target_closures: int = SPEC_TARGET_CLOSURES

    meets_postings: bool = False
    meets_companies: bool = False
    meets_closures: bool = False

    @property
    def headline_gate_met(self) -> bool:
        """All three spec.md §6 sample-size legs cleared.

        Note that `meets_closures` is a corpus-wide leg (a replay dataset has
        no closure-event count of its own), so a `True` here still means
        "the evaluated dataset carries the postings/companies, and the corpus
        behind it carries the closures".
        """
        return self.meets_postings and self.meets_companies and self.meets_closures

    def describe(self) -> str:
        lines = [
            "sample sizes (spec.md §6 targets):",
            f"  evaluated dataset: postings={self.dataset_postings} "
            f"companies={self.dataset_companies} cases={self.dataset_cases}",
            f"  collection corpus: postings={self.corpus_postings} "
            f"companies={self.corpus_companies} closure_events={self.corpus_closures}",
            f"  targets: postings>={self.target_postings} "
            f"companies>={self.target_companies} closures>={self.target_closures}",
            f"  met (dataset-scoped where possible): postings={_flag(self.meets_postings)} "
            f"companies={_flag(self.meets_companies)} "
            f"closures(corpus-wide)={_flag(self.meets_closures)}",
            f"  headline gate met: {_flag(self.headline_gate_met)}",
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
    data_quality: DataQuality
    agent_gate: AgentGateResult
    product_gate: ProductGateResult
    ranker: RankerResult
    sample_sizes: SampleSizes

    survival: object | None = None
    survival_note: str = ""
    limitations: tuple[str, ...] = ()

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
        lines.append("  " + self.data_quality.describe().replace("\n", "\n  "))
        lines.append("  " + self.agent_gate.describe().replace("\n", "\n  "))
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


def _sample_sizes(conn: sqlite3.Connection, dataset_row: sqlite3.Row | None) -> SampleSizes:
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

    return SampleSizes(
        dataset_postings=dataset_postings,
        dataset_companies=dataset_companies,
        dataset_cases=dataset_cases,
        corpus_postings=corpus_postings,
        corpus_companies=corpus_companies,
        corpus_closures=corpus_closures,
        meets_postings=dataset_postings >= SPEC_TARGET_POSTINGS,
        meets_companies=dataset_companies >= SPEC_TARGET_COMPANIES,
        meets_closures=corpus_closures >= SPEC_TARGET_CLOSURES,
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


def _c_runner(llm: object | None, llm_factory: object | None) -> tuple[object | None, str]:
    """`(runner, reason)` for System C — never touching the network to decide.

    A live model call to "check" whether a key works would be exactly the
    thing this function exists to avoid. Availability is decided from what
    the caller passed and from `os.environ`, and nothing else.
    """
    if llm is None and llm_factory is None and not os.environ.get("ANTHROPIC_API_KEY"):
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
) -> str:
    """Replay `system` when it has no scoped runs (or `rerun`); else reuse.

    Returns the `systems_run` status string. `LookupError` from
    `run_replay` (a dataset with no cases) is allowed to propagate: it means
    the caller named a dataset that does not exist or was never built, and
    silently reporting zeros for it would be worse than failing.
    """
    existing = _scoped_run_ids(conn, dataset_id=dataset_id, system=system)
    if existing and not rerun:
        return "reused"

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


def _company_event_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    covered = _count(conn, "SELECT COUNT(DISTINCT company_id) FROM company_events")
    total = _count(conn, "SELECT COUNT(*) FROM companies")
    return covered, total


def _limitations(
    conn: sqlite3.Connection,
    *,
    sample_sizes: SampleSizes,
    systems_run: dict[str, str],
    allowed_splits: tuple[str, ...],
    holdout_runs_noted: int,
    holdout_cases: int,
    survival_note: str,
    data_quality_result: DataQuality,
) -> tuple[str, ...]:
    covered, total = _company_event_coverage(conn)
    items: list[str] = [
        "Archive-era cases are weak by construction. For any replay case whose T "
        "predates this project's own daily snapshots, the only observation "
        "available is a Wayback capture, so `board_snapshot` evidence is "
        "sparse, `source_quality='archive'`, and often absent entirely. Those "
        "cases are scored, but a decision made on an archive-only corpus is a "
        "decision made on much less evidence than a present-day one, and the "
        "agreement figures average the two together.",
        "`team_signal` is unlicensed and disabled (`[team_signal].enabled = false`; "
        "spec.md §4 records that there is no licensed enrichment source). It is the "
        "only probe that populates the team-shrink input, so the action policy's P4 "
        "branch is UNREACHABLE in every number in this report. No system is penalised "
        "or credited for it, and the high-cost tier is effectively empty.",
        f"Company-event coverage is partial: {covered}/{total} companies in this database "
        "carry any `company_events` row at all (the M6 collection reached 15/78 companies "
        "at its fullest). `company_events` is a medium-cost probe and a "
        "policy input, so for the uncovered majority the material-negative-event and "
        "hiring-freeze inputs are the UNKNOWN sentinel rather than a negative finding "
        "(spec.md §4: missing history never means flat hiring).",
        f"Sample sizes vs. spec.md §6 targets — evaluated dataset: "
        f"{sample_sizes.dataset_postings} postings (target "
        f">={sample_sizes.target_postings}), {sample_sizes.dataset_companies} companies "
        f"(target >={sample_sizes.target_companies}), {sample_sizes.dataset_cases} "
        f"replay cases; collection corpus: {sample_sizes.corpus_postings} postings, "
        f"{sample_sizes.corpus_companies} companies, {sample_sizes.corpus_closures} "
        f"closure events (target >={sample_sizes.target_closures}). The corpus may "
        "clear the targets while the evaluated replay dataset is a far smaller slice "
        "of it; the headline gate is judged on what was ACTUALLY evaluated, and it is "
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

    c_status = systems_run.get("C", "")
    if C_NOT_RUN_REASON in c_status or not c_status:
        items.append(
            f"System C was {C_NOT_RUN_REASON} — there is no ANTHROPIC_API_KEY in this "
            "environment and no `llm`/`llm_factory` was supplied, so no live model call "
            "was attempted. The spec.md §6 agent gate is therefore `not_run`, not "
            "`fail`: an ungraded candidate has not failed. Every System C figure "
            f"elsewhere in this report reads `{C_NOT_RUN_REASON}`."
        )

    if "test" in allowed_splits and holdout_cases:
        items.append(
            f"The final `test` holdout WAS read by this evaluation: {holdout_cases} scoped "
            "case(s) fall in it (spec.md §6's final evaluation; `allow_test=True`), and "
            f"{holdout_runs_noted} run(s) had a `{HOLDOUT_TEST_STEP}` marker appended to "
            "their trace. Nothing downstream of this report may be tuned on these "
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
            "violation(s) (spec.md §6 target: 0). Every metric in this report that "
            "touches an affected run is suspect."
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
    llm: object | None = None,
    llm_factory: object | None = None,
    rerun: bool = False,
    limit_cases: int | None = None,
    collection_status_csv: str | Path | None = None,
    match_precision_path: str | Path = DEFAULT_MATCH_PRECISION_PATH,
    include_survival: bool = True,
    ranker_config: RankerConfig | None = None,
) -> EvaluationReport:
    """Run the whole spec.md §6 evaluation over one replay dataset.

    Makes no network calls and never builds a dataset. The only execution it
    can trigger is `rli.replay.run.run_replay`, and only for a system with no
    scoped runs (or when `rerun=True`).

    Arguments:
        dataset_id: the replay dataset to score. Must already exist —
            `LookupError` propagates from `run_replay` if it has no cases.
        allowed_splits: read-side split filter. `None` means
            `("dev", "validation", "test")` when `allow_test` (the default
            for a final evaluation), else `("dev", "validation")`.
        allow_test: permit reading the `test` holdout. Default `True`
            because this IS spec.md §6's final evaluation; see the module
            docstring for why the build-time interlocks are untouched.
        split_kind / cutoff / validation_cutoff: override the split
            assignment. Default to the dataset row's own `split_kind` and
            `created_at`, reproducing the assignment the build used.
        with_c: also evaluate System C. Requires `llm` or `llm_factory` or an
            `ANTHROPIC_API_KEY`; otherwise C is recorded as
            `"skipped: not run: no API key"` and no call is attempted.
        rerun: force `run_replay(..., replace=True)` for every system.
        include_survival: include the corpus-wide posting-behaviour summary.
            `False` skips the lifelines import entirely.

    Never raises for corrupt stored data; a failing survival fit degrades to
    `survival=None` plus a `survival_note`.
    """
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
        )

    c_runner, c_reason = _c_runner(llm, llm_factory)
    if not with_c:
        # Even when C was not asked for, say whether it COULD have run: a
        # reader looking at an empty C column needs the reason, and "no API
        # key" is the reason that matters here.
        suffix = f"; {c_reason}" if c_reason else ""
        systems_run["C"] = f"skipped: not requested (pass with_c=True / --with-c){suffix}"
    elif c_runner is None:
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

    systems_evaluated = tuple(
        system
        for system in ("A", "B", "C")
        if _scoped_run_ids(conn, dataset_id=dataset_id, system=system)
    )

    # --- case set -----------------------------------------------------------
    case_set = collect_system_runs(
        conn,
        dataset_id=dataset_id,
        splits=splits,
        systems=systems_evaluated or ("A", "B"),
        allowed_splits=resolved_splits,
    )

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
        holdout_runs_noted = _note_holdout_runs(conn, holdout_run_ids)

    # --- per-system efficiency (X vs A) ------------------------------------
    efficiency: dict[str, EfficiencyMetrics] = {}
    for system in systems_evaluated:
        efficiency[system] = agent_efficiency(
            conn,
            cfg,
            dataset_id=dataset_id,
            splits=splits,
            system=system,
            reference="A",
            allowed_splits=resolved_splits,
            case_set=case_set,
        )

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
    extra_notes: list[str] = []
    if "C" not in efficiency:
        extra_notes.append(f"System C: {systems_run.get('C', C_NOT_RUN_REASON)}")
    if holdout_cases:
        extra_notes.append(
            f"The final `test` holdout was read for this gate: {holdout_cases} scoped "
            f"case(s) are in the `test` split and {holdout_runs_noted} run(s) were "
            f"marked with `{HOLDOUT_TEST_STEP}` in the trace. Nothing may be tuned on "
            "this verdict (spec.md §6)."
        )
    if extra_notes:
        gate = gate.model_copy(update={"notes": tuple(gate.notes) + tuple(extra_notes)})

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

    sample_sizes = _sample_sizes(conn, dataset_row)

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
        data_quality=dq,
        agent_gate=gate,
        product_gate=outcomes_gate,
        ranker=ranker,
        sample_sizes=sample_sizes,
        survival=survival,
        survival_note=survival_note,
        limitations=_limitations(
            conn,
            sample_sizes=sample_sizes,
            systems_run=systems_run,
            allowed_splits=resolved_splits,
            holdout_runs_noted=holdout_runs_noted,
            holdout_cases=holdout_cases,
            survival_note=survival_note,
            data_quality_result=dq,
        ),
    )


# ---------------------------------------------------------------------------
# 8. Markdown report writer
# ---------------------------------------------------------------------------


def _headline_section(report: EvaluationReport) -> list[str]:
    sizes = report.sample_sizes
    table = _markdown_table(
        ["measure", "evaluated dataset", "collection corpus", "spec §6 target", "met?"],
        [
            [
                "postings",
                str(sizes.dataset_postings),
                str(sizes.corpus_postings),
                f">= {sizes.target_postings}",
                _flag(sizes.meets_postings),
            ],
            [
                "companies",
                str(sizes.dataset_companies),
                str(sizes.corpus_companies),
                f">= {sizes.target_companies}",
                _flag(sizes.meets_companies),
            ],
            [
                "closure events",
                "n/a (a replay case is a (posting, T) grid point, not a closure)",
                str(sizes.corpus_closures),
                f">= {sizes.target_closures}",
                _flag(sizes.meets_closures) + " (corpus-wide)",
            ],
            ["replay cases scored", str(sizes.dataset_cases), "—", "—", "—"],
        ],
    )
    verdict = "**MET**" if sizes.headline_gate_met else "**NOT MET**"
    return [
        "## Headline gate (sample sizes)",
        "",
        table,
        "",
        f"Headline gate: {verdict}.",
        "",
        "The two size columns are NOT interchangeable. The collection corpus may clear "
        "spec.md §6's targets while the replay dataset that was actually evaluated is a "
        "far smaller slice of it. Postings and companies are judged on the EVALUATED "
        "dataset, because that is the evidence behind every number below. The "
        "closure-event leg has no dataset-scoped equivalent and is judged corpus-wide, "
        "which makes it the optimistic leg of this verdict.",
    ]


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


def _action_distribution_section(report: EvaluationReport) -> list[str]:
    systems = _ordered_systems(report.efficiency)
    actions = sorted(
        {
            action
            for name in systems
            for action in report.efficiency[name].action_distribution
        }
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
        columns = sorted(
            set(matrix)
            | {other for row in matrix.values() for other in row}
        )
        rows = [
            [ref] + [str(matrix.get(ref, {}).get(other, 0)) for other in columns]
            for ref in columns
        ]
        lines.append(f"**A action (row) -> {name} action (column)**")
        lines.append("")
        lines.append(_markdown_table([f"A \\ {name}"] + (columns or ["(none)"]), rows))
        lines.append("")
    if "C" not in systems:
        lines.append(f"System C has no confusion matrix: {C_NOT_RUN_REASON}.")
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


def _data_quality_section(report: EvaluationReport) -> list[str]:
    dq = report.data_quality
    citation = dq.citation
    rows = [
        ["runs checked", str(dq.runs_checked)],
        ["postings checked", str(dq.postings_checked)],
        [
            "ATS resolution rate",
            f"{dq.ats_resolved_runs}/{dq.runs_checked} ({_pct(dq.ats_resolution_rate)})",
        ],
        ["ATS distribution (distinct postings)", _render(dq.ats_distribution)],
        [
            "publish-date coverage",
            f"{dq.publish_date_runs}/{dq.runs_checked} ({_pct(dq.publish_date_coverage)})",
        ],
        [
            "publish evidence by source quality (distinct runs)",
            _render(dq.publish_date_by_source_quality),
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
    citation_rows = [
        ["reasons total", str(citation.reasons_total)],
        ["runs with reasons", f"{citation.runs_with_reasons}/{citation.runs_checked}"],
        [
            "every cited evidence id exists",
            f"{citation.reasons_all_ids_exist} ({_pct(citation.id_existence_rate)})",
        ],
        ["reasons citing a missing id", str(citation.reasons_with_missing_ids)],
        ["reasons citing no id at all", str(citation.reasons_with_no_ids)],
        [
            "classified / unclassified reason text",
            f"{citation.reasons_classified} / {citation.reasons_unclassified}",
        ],
        [
            "supported / unsupported (of classified)",
            f"{citation.reasons_supported} / {citation.reasons_unsupported} "
            f"({_pct(citation.support_rate)})",
        ],
        ["claim families seen", _render(citation.families_seen)],
    ]
    return [
        "## Data quality",
        "",
        f"Scoped to system(s) `{', '.join(dq.systems) or '(none)'}` over splits "
        f"`{', '.join(dq.allowed_splits) or '(none)'}`. System A runs every probe, so "
        "scoping here to A measures the cached record at its fullest rather than "
        "penalising it for System B's deliberately narrower probe set.",
        "",
        _markdown_table(["measure", "value"], rows),
        "",
        "### Citation support",
        "",
        _markdown_table(["measure", "value"], citation_rows),
        "",
        "An UNCLASSIFIED reason is one whose text matched no claim family: it is "
        "reported, never guessed at, and counts as neither supported nor unsupported. "
        "A reason citing no evidence id at all does not count as 'all ids exist'.",
    ]


def _leakage_section(report: EvaluationReport) -> list[str]:
    dq = report.data_quality
    return [
        "## Future leakage",
        "",
        _markdown_table(
            ["measure", "value"],
            [
                ["violations (spec.md §6 target: 0)", str(dq.leakage_violations)],
                ["clean", _flag(dq.leakage_clean)],
                ["violation kinds", _render(dq.leakage_counts)],
            ],
        ),
        "",
        "A replayed system reaches the network never: `rli.replay.mode.ReplayNetClient` "
        "raises on any attempt and the violation is written into the trace, so this "
        "audit counts recorded attempts rather than inferring them.",
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
        _markdown_table(
            ["curve", "n", "events", "censored", "median days", "note"], curve_rows
        )
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
        "(\"did running this probe move the partial action toward the reference "
        "action?\"), not a ground-truth utility; the deterministic comparison score is "
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
        _action_distribution_section,
        _agreement_section,
        _cost_section,
        _failure_section,
        _data_quality_section,
        _leakage_section,
        _survival_section,
        _agent_gate_section,
        _product_gate_section,
        _ranker_section,
        _limitations_section,
    ):
        lines.extend(section(report))
        lines.append("")

    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination
