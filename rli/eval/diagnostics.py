"""Evaluation diagnostics that keep the spec.md §6 report honest (PLAN.md M6).

`rli.eval.metrics` measures the figures spec.md §6 names. This module
measures the context a reader needs to know whether those figures mean
anything, each one added because a review of the 2026-09-30 report found the
report silent on it:

* **Per-company era** (`EraBoundaries`). A replay case is live-era only when
  T is at or after ITS company's first own (`source='own'`) board capture.
  One global boundary (the first own capture anywhere) put every case after
  day 0 in the live era even for companies whose own collection began weeks
  later.
* **Where the strong evidence sits** (`GridDistribution`): strong / apply_now
  cases per grid point, and the share at the dataset's last T (the
  build-time grid point), so a reader sees when all strong evidence sits at
  one T.
* **What was actually scored** (`ScoredSample`): postings, companies and
  cases after removing unassigned cases and cases whose identity never
  resolved; the headline sample-size gate is judged on these.
* **Probe dependence** (`ProbeDependence`): the action the frozen policy
  gives on the SAME case with no dynamic-probe evidence, recomputed
  deterministically from the reference run's own always-run evidence. A case
  is probe-dependent when the reference action differs from it. Agreement on
  the other cases is agreement on a decision no probe could have changed.
* **Trace facts per system** (`AgentTraceStats`): model calls, investigator
  errors, citations dropped by the explanation guard, explanation fallbacks.
* **Splits read** (`SplitCounts`) and **policy branches fired**
  (`policy_branch_counts`), which the Limitations text is derived from.

Nothing here writes to the database.

--------------------------------------------------------------------------
Why the no-probe counterfactual needs no history features
--------------------------------------------------------------------------

`rli.eval.case.build_case_state` derives `repost_pattern` and `long_lived`
from the point-in-time corpus, which this module cannot rebuild without
writing temp tables. It does not need them: both feed only policy branch P4,
which also requires `corroborating_hiring_signal is False`, and that input
comes only from `team_signal` evidence — a dynamic probe. With dynamic
evidence removed it is UNKNOWN, P4 (and P5b) cannot fire, and the action is a
function of the always-run evidence alone. `ProbeDependence.selfcheck_*`
verifies this on every scoped run that executed no dynamic probe: there the
counterfactual must equal the recorded action.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from rli.agent.explanation import (
    STEP_CITATION_INVALID,
    STEP_CITATION_UNSUPPORTED,
    STEP_EXPLANATION_FALLBACK,
)
from rli.agent.loop import (
    STEP_CONTROLLER_DECISION,
    STEP_EXPLANATION,
    STEP_INVESTIGATOR,
    STEP_RUN_FLAG_INVESTIGATOR_ERROR,
)
from rli.config import Config
from rli.eval.metrics import MetricsCase, MetricsCaseSet, subset_case_set
from rli.eval.report import cost_tier_for
from rli.eval.runner import STEP_POLICY_DECISION, STEP_PROBE_RUN, STEP_PROBE_SKIPPED
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN
from rli.models.probe import ProbeResult
from rli.models.time import parse_utc
from rli.policy.action import decide as policy_decide
from rli.policy.inputs import derive_policy_inputs, last_publish_or_refresh
from rli.policy.quality import evidence_quality_detail
from rli.probes.board_snapshot import BoardSnapshotProbe
from rli.probes.registry import DYNAMIC_PROBES
from rli.probes.resolve_posting import ResolvePostingProbe

__all__ = [
    "AgentTraceStats",
    "CompanyHoldoutCheck",
    "EraBoundaries",
    "GridDistribution",
    "GridPointRow",
    "ProbeDependence",
    "ScoredSample",
    "SplitCounts",
    "agent_trace_stats",
    "build_time_test_companies",
    "case_key",
    "company_holdout_check",
    "counterfactual_actions",
    "era_boundaries",
    "grid_distribution",
    "policy_branch_counts",
    "probe_dependence",
    "run_probe_costs",
    "scored_sample",
    "split_case_set_by_company_era",
    "split_counts",
]

_SQL_CHUNK = 400

#: `run_steps.decision_type` written when a run's identity never resolved
#: (A, B, C and R all write it; see `rli.eval.system_a` / `rli.agent.loop`).
STEP_IDENTITY_UNRESOLVED = f"{STEP_PROBE_SKIPPED}:identity_unresolved"

#: The legacy marker of an investigator failure, from before the run flag.
_LEGACY_INVESTIGATOR_ERROR = f"{STEP_CONTROLLER_DECISION}:stop:investigator_error"

_ALWAYS_RUN = (ResolvePostingProbe.name, BoardSnapshotProbe.name)


def _chunks(values: Sequence[str], size: int = _SQL_CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator <= 0 else numerator / denominator


def case_key(input_url: str, replay_at: str) -> str:
    """The string key of a replay case `(input_url, replay_at)`."""
    return f"{input_url}\n{replay_at}"


def _key(case: MetricsCase) -> str:
    return case_key(case.input_url, case.replay_at)


# ---------------------------------------------------------------------------
# 1. Case -> company, and the per-company era
# ---------------------------------------------------------------------------


def _case_companies(
    conn: sqlite3.Connection, dataset_id: str, cases: Sequence[MetricsCase]
) -> dict[str, str]:
    """`case_key -> company_id`: the dataset's own `replay_cases` row, else `postings`."""
    by_case: dict[tuple[str, str], str] = {}
    by_posting: dict[str, str] = {}
    try:
        for row in conn.execute(
            "SELECT posting_id, replay_at, canonical_url, company_id FROM replay_cases "
            "WHERE dataset_id = ?",
            (dataset_id,),
        ):
            by_case[(str(row["canonical_url"]), str(row["replay_at"]))] = str(row["company_id"])
            by_posting.setdefault(str(row["posting_id"]), str(row["company_id"]))
    except sqlite3.Error:  # pragma: no cover - pre-replay schema
        pass

    missing = sorted(
        {
            case.posting_id
            for case in cases
            if (case.input_url, case.replay_at) not in by_case and case.posting_id not in by_posting
        }
    )
    for chunk in _chunks(missing):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT posting_id, company_id FROM postings WHERE posting_id IN ({placeholders})",
            tuple(chunk),
        ):
            if row["company_id"] is not None:
                by_posting[str(row["posting_id"])] = str(row["company_id"])

    result: dict[str, str] = {}
    for case in cases:
        company = by_case.get((case.input_url, case.replay_at)) or by_posting.get(case.posting_id)
        if company is not None:
            result[_key(case)] = company
    return result


class EraBoundaries(BaseModel):
    """Per-company live-era boundaries for one case set.

    `by_company[c]` is company `c`'s first own (`source='own'`) board
    capture. A case is live-era iff its company has one and
    `replay_at >= by_company[company]`; a case whose company has none (or is
    unknown) is archive-era. `earliest` is the first own capture anywhere,
    kept for display; it no longer decides any case.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_company: dict[str, str] = {}
    case_company: dict[str, str] = {}
    earliest: str | None = None

    def company_for(self, case: MetricsCase) -> str | None:
        return self.case_company.get(_key(case))

    def boundary_for(self, case: MetricsCase) -> str | None:
        company = self.company_for(case)
        return None if company is None else self.by_company.get(company)

    def era_for_case(self, case: MetricsCase) -> str:
        boundary = self.boundary_for(case)
        if boundary is None or case.replay_at < boundary:
            return "archive-era"
        return "live-era"

    def describe(self) -> str:
        companies = len(set(self.case_company.values()))
        with_own = len({c for c in self.case_company.values() if c in self.by_company})
        return (
            f"per-company era boundaries: {with_own}/{companies} case companies have an own "
            f"board capture; earliest own capture {self.earliest or '(none)'}"
        )


def era_boundaries(
    conn: sqlite3.Connection, *, dataset_id: str, case_set: MetricsCaseSet
) -> EraBoundaries:
    """`EraBoundaries` for `case_set`: each company's first own board capture."""
    by_company: dict[str, str] = {}
    try:
        for row in conn.execute(
            "SELECT company_id, MIN(captured_at) AS first_own FROM board_snapshots "
            "WHERE source = 'own' GROUP BY company_id"
        ):
            if row["first_own"] is not None:
                by_company[str(row["company_id"])] = str(row["first_own"])
    except sqlite3.Error:  # pragma: no cover - pre-board_snapshots schema
        by_company = {}
    case_company = _case_companies(conn, dataset_id, case_set.cases)
    relevant = {
        company: by_company[company] for company in set(case_company.values()) & set(by_company)
    }
    return EraBoundaries(
        by_company=relevant,
        case_company=case_company,
        earliest=min(by_company.values()) if by_company else None,
    )


def split_case_set_by_company_era(
    case_set: MetricsCaseSet, eras: EraBoundaries
) -> dict[str, MetricsCaseSet]:
    """`{"live-era": ..., "archive-era": ...}` by the PER-COMPANY boundary."""
    buckets: dict[str, list[MetricsCase]] = {"live-era": [], "archive-era": []}
    for case in case_set.cases:
        buckets[eras.era_for_case(case)].append(case)
    return {era: subset_case_set(case_set, cases) for era, cases in buckets.items()}


# ---------------------------------------------------------------------------
# 2. Strong / apply_now by grid point
# ---------------------------------------------------------------------------


class GridPointRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    replay_at: str
    cases: int = 0
    strong: int = 0
    apply_now: dict[str, int] = {}


class GridDistribution(BaseModel):
    """Where strong-evidence and apply_now cases sit on the replay grid.

    `strong` is counted on `quality_system`'s runs (the reference, A, by
    default: evidence quality is a property of the always-run evidence and
    is shared by every system on a case).

    BUILD-TIME cases are those with `replay_at >= build_started_at` (the
    dataset's `created_at`, i.e. when the build began). Since the replay
    builder dates each still-open posting's last grid point by its OWN live
    observation, build-time cases carry many different T values (one per
    posting), all at or after the build start; an older dataset put them all
    at the build start itself. Either way, this is the slice whose evidence
    came from the live resolver rather than from captures available at an
    earlier T.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    quality_system: str = "A"
    grid_points: int = 0
    last_replay_at: str | None = None
    build_started_at: str | None = None
    build_time_cases: int = 0
    build_time_grid_points: int = 0
    strong_total: int = 0
    strong_at_build_time: int = 0
    strong_share_at_build_time: float | None = None
    grid_points_with_strong: int = 0
    apply_now_total: dict[str, int] = {}
    apply_now_at_build_time: dict[str, int] = {}
    apply_now_share_at_build_time: dict[str, float | None] = {}
    rows: tuple[GridPointRow, ...] = ()

    def describe(self) -> str:
        return (
            f"grid: {self.grid_points} point(s); build-time cases (T >= build start "
            f"{self.build_started_at}): {self.build_time_cases} over "
            f"{self.build_time_grid_points} T value(s); strong ({self.quality_system}) "
            f"{self.strong_at_build_time}/{self.strong_total} at build time over "
            f"{self.grid_points_with_strong} grid point(s) with any; apply_now at build time: "
            + " ".join(
                f"{name}={self.apply_now_at_build_time.get(name, 0)}/{total}"
                for name, total in sorted(self.apply_now_total.items())
            )
        )


def _at_or_after(replay_at: str, boundary: datetime | None) -> bool:
    if boundary is None:
        return False
    try:
        return parse_utc(replay_at) >= boundary
    except (TypeError, ValueError):
        return False


def grid_distribution(
    case_set: MetricsCaseSet,
    *,
    systems: Sequence[str],
    build_started_at: str | None = None,
    quality_system: str = "A",
) -> GridDistribution:
    """Strong / apply_now counts per grid point (`replay_at`) and at build time.

    `build_started_at` is the dataset's `created_at`; `None` falls back to
    the last grid point (every case at the latest T is build-time).
    """
    rows: dict[str, dict[str, object]] = {}
    for case in case_set.cases:
        row = rows.setdefault(case.replay_at, {"cases": 0, "strong": 0, "apply_now": {}})
        row["cases"] = int(row["cases"]) + 1  # type: ignore[arg-type]
        reference = case.runs.get(quality_system)
        if reference is not None and reference.evidence_quality == "strong":
            row["strong"] = int(row["strong"]) + 1  # type: ignore[arg-type]
        apply_now: dict[str, int] = row["apply_now"]  # type: ignore[assignment]
        for name in systems:
            run = case.runs.get(name)
            if run is not None and run.action == "apply_now":
                apply_now[name] = apply_now.get(name, 0) + 1

    ordered = sorted(rows)
    point_rows = tuple(
        GridPointRow(
            replay_at=replay_at,
            cases=int(rows[replay_at]["cases"]),  # type: ignore[arg-type]
            strong=int(rows[replay_at]["strong"]),  # type: ignore[arg-type]
            apply_now=dict(rows[replay_at]["apply_now"]),  # type: ignore[arg-type]
        )
        for replay_at in ordered
    )
    last = point_rows[-1].replay_at if point_rows else None
    boundary_text = build_started_at if build_started_at is not None else last
    boundary = _parsed_or_none(boundary_text) if boundary_text is not None else None
    build_rows = [row for row in point_rows if _at_or_after(row.replay_at, boundary)]

    strong_total = sum(row.strong for row in point_rows)
    strong_build = sum(row.strong for row in build_rows)
    apply_now_total = {
        name: sum(row.apply_now.get(name, 0) for row in point_rows)
        for name in systems
        if any(name in case.runs for case in case_set.cases)
    }
    apply_now_build = {
        name: sum(row.apply_now.get(name, 0) for row in build_rows) for name in apply_now_total
    }
    return GridDistribution(
        quality_system=quality_system,
        grid_points=len(point_rows),
        last_replay_at=last,
        build_started_at=boundary_text,
        build_time_cases=sum(row.cases for row in build_rows),
        build_time_grid_points=len(build_rows),
        strong_total=strong_total,
        strong_at_build_time=strong_build,
        strong_share_at_build_time=_ratio(strong_build, strong_total),
        grid_points_with_strong=sum(1 for row in point_rows if row.strong),
        apply_now_total=apply_now_total,
        apply_now_at_build_time=apply_now_build,
        apply_now_share_at_build_time={
            name: _ratio(apply_now_build[name], total) for name, total in apply_now_total.items()
        },
        rows=point_rows,
    )


def _parsed_or_none(value: str) -> datetime | None:
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 3. What was actually scored
# ---------------------------------------------------------------------------


def _runs_with_step(
    conn: sqlite3.Connection, run_ids: Sequence[str], decision_types: Sequence[str]
) -> set[str]:
    found: set[str] = set()
    type_placeholders = ",".join("?" for _ in decision_types)
    for chunk in _chunks(list(run_ids)):
        placeholders = ",".join("?" for _ in chunk)
        found.update(
            str(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT run_id FROM run_steps WHERE run_id IN ({placeholders}) "
                f"AND decision_type IN ({type_placeholders})",
                (*chunk, *decision_types),
            )
        )
    return found


class ScoredSample(BaseModel):
    """Postings / companies / cases that were actually SCORED.

    A case is scored when it passed the split gate (so it is neither
    `unassigned` nor an excluded holdout) and at least one of its runs
    resolved the posting's identity (no `probe_skipped:identity_unresolved`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    postings: int = 0
    companies: int = 0
    cases: int = 0
    identity_unresolved_cases: int = 0
    unassigned_cases: int = 0
    excluded_holdout_cases: int = 0
    cases_without_company: int = 0


def scored_sample(
    conn: sqlite3.Connection, case_set: MetricsCaseSet, eras: EraBoundaries
) -> ScoredSample:
    run_ids = [run.run_id for case in case_set.cases for run in case.runs.values()]
    unresolved_runs = _runs_with_step(conn, run_ids, (STEP_IDENTITY_UNRESOLVED,))
    scored: list[MetricsCase] = []
    unresolved_cases = 0
    for case in case_set.cases:
        if case.runs and all(run.run_id in unresolved_runs for run in case.runs.values()):
            unresolved_cases += 1
            continue
        scored.append(case)
    companies = {eras.company_for(case) for case in scored}
    return ScoredSample(
        postings=len({case.posting_id for case in scored}),
        companies=len(companies - {None}),
        cases=len(scored),
        # Both kinds: runs with no posting id at all (excluded by the case
        # collector, which knows their split from `replay_cases`) and runs
        # with an id but an unresolved company (excluded here).
        identity_unresolved_cases=unresolved_cases + case_set.identity_unresolved,
        unassigned_cases=case_set.unassigned,
        excluded_holdout_cases=case_set.excluded_holdout,
        cases_without_company=sum(1 for case in scored if eras.company_for(case) is None),
    )


# ---------------------------------------------------------------------------
# 4. Probe dependence: the no-dynamic-evidence counterfactual
# ---------------------------------------------------------------------------


def _evidence_items(
    conn: sqlite3.Connection, run_ids: Sequence[str]
) -> dict[str, list[EvidenceItem]]:
    items: dict[str, list[EvidenceItem]] = {}
    for chunk in _chunks(list(run_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT id, run_id, probe, claim_type, value, source_url, raw_excerpt,
                   source_quality, source_event_at, available_at, fetched_at
            FROM evidence WHERE run_id IN ({placeholders})
            """,
            tuple(chunk),
        ):
            if str(row["probe"]) in DYNAMIC_PROBES:
                continue
            try:
                item = EvidenceItem(
                    id=str(row["id"]),
                    run_id=str(row["run_id"]),
                    probe=str(row["probe"]),
                    claim_type=str(row["claim_type"]),
                    value=str(row["value"]),
                    source_url=str(row["source_url"]),
                    raw_excerpt=row["raw_excerpt"],
                    source_quality=row["source_quality"],
                    source_event_at=row["source_event_at"],
                    available_at=row["available_at"],
                    fetched_at=row["fetched_at"],
                )
            except ValueError:
                continue
            items.setdefault(str(row["run_id"]), []).append(item)
    return items


def _always_run_failures(
    conn: sqlite3.Connection, run_ids: Sequence[str]
) -> tuple[dict[str, list[ProbeResult]], set[str]]:
    """`(run -> failed always-run results, runs whose resolver failed)`."""
    failures: dict[str, list[ProbeResult]] = {}
    resolver_failed: set[str] = set()
    for chunk in _chunks(list(run_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT run_id, probe_name, error FROM run_steps
            WHERE run_id IN ({placeholders}) AND component = 'probe'
                  AND decision_type = ? AND probe_name IN (?, ?) AND error IS NOT NULL
            """,
            (*chunk, STEP_PROBE_RUN, *_ALWAYS_RUN),
        ):
            run_id = str(row["run_id"])
            failures.setdefault(run_id, []).append(ProbeResult(ok=False, error=str(row["error"])))
            if row["probe_name"] == ResolvePostingProbe.name:
                resolver_failed.add(run_id)
    return failures, resolver_failed


def counterfactual_actions(
    conn: sqlite3.Connection, cfg: Config, runs: Mapping[str, str]
) -> dict[str, str]:
    """`run_id -> action` the frozen policy gives on that run's ALWAYS-RUN evidence only.

    `runs` maps `run_id -> replay_at`. Dynamic-probe evidence is dropped,
    always-run failures are re-read from the trace, and the policy is
    called with no history features (see the module docstring for why they
    cannot matter here). A run whose evidence cannot be decoded is omitted.
    """
    run_ids = sorted(runs)
    evidence = _evidence_items(conn, run_ids)
    failures, resolver_failed = _always_run_failures(conn, run_ids)
    actions: dict[str, str] = {}
    for run_id in run_ids:
        try:
            moment: datetime = parse_utc(runs[run_id])
        except (TypeError, ValueError):
            continue
        items = evidence.get(run_id, [])
        inputs = derive_policy_inputs(
            items, None, moment, cfg=cfg, resolver_ok=run_id not in resolver_failed
        )
        quality = evidence_quality_detail(items, inputs, failures.get(run_id, []), cfg)
        outcome = policy_decide(
            inputs,
            quality.quality,
            moment,
            cfg,
            long_lived=UNKNOWN,
            last_refreshed_at=last_publish_or_refresh(items),
        )
        actions[run_id] = outcome.recommended_action
    return actions


class ProbeDependence(BaseModel):
    """How many cases could a dynamic probe have changed at all?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str = "A"
    reference_cases: int = 0
    counterfactual_computed: int = 0
    probe_dependent: int = 0
    probe_dependent_share: float | None = None
    #: `"<no-probe action> -> <reference action>"` counts over the probe-dependent cases.
    transitions: dict[str, int] = {}
    counterfactual_distribution: dict[str, int] = {}
    #: Self-check: scoped runs (any system) that executed NO dynamic probe,
    #: and how many of them the counterfactual reproduces exactly.
    selfcheck_runs: int = 0
    selfcheck_matches: int = 0
    probe_dependent_keys: tuple[str, ...] = ()
    #: The subset is defined from the REFERENCE's action only. Cases OUTSIDE
    #: it where another system's action differs from the reference's are
    #: counted here, per system: they are in every pooled figure but not in
    #: the probe-dependent one.
    disagreements_outside_subset: dict[str, int] = {}

    def describe(self) -> str:
        return (
            f"probe dependence vs {self.reference}: {self.probe_dependent}/"
            f"{self.counterfactual_computed} case(s) where {self.reference}'s action differs from "
            f"the no-dynamic-probe action; self-check {self.selfcheck_matches}/"
            f"{self.selfcheck_runs} no-probe runs reproduced"
        )


def probe_dependence(
    conn: sqlite3.Connection, cfg: Config, case_set: MetricsCaseSet, *, reference: str = "A"
) -> tuple[ProbeDependence, MetricsCaseSet]:
    """`(summary, the probe-dependent sub-case-set)` for `case_set`."""
    reference_runs = {
        case.runs[reference].run_id: case.replay_at
        for case in case_set.cases
        if reference in case.runs
    }
    # Self-check population: every scoped run that executed no dynamic probe.
    selfcheck = {
        run.run_id: case.replay_at
        for case in case_set.cases
        for run in case.runs.values()
        if not run.dynamic_probes() and run.action is not None
    }
    counterfactual = counterfactual_actions(conn, cfg, {**reference_runs, **selfcheck})

    recorded = {run.run_id: run.action for case in case_set.cases for run in case.runs.values()}
    selfcheck_matches = sum(
        1
        for run_id in selfcheck
        if run_id in counterfactual and counterfactual[run_id] == recorded.get(run_id)
    )

    dependent: list[MetricsCase] = []
    transitions: dict[str, int] = {}
    distribution: dict[str, int] = {}
    computed = 0
    outside: dict[str, int] = {}
    for case in case_set.cases:
        run = case.runs.get(reference)
        if run is None or run.run_id not in counterfactual:
            continue
        computed += 1
        no_probe = counterfactual[run.run_id]
        distribution[no_probe] = distribution.get(no_probe, 0) + 1
        if run.action != no_probe:
            dependent.append(case)
            label = f"{no_probe} -> {run.action}"
            transitions[label] = transitions.get(label, 0) + 1
        else:
            for name, other in case.runs.items():
                if name != reference and other.action != run.action:
                    outside[name] = outside.get(name, 0) + 1

    summary = ProbeDependence(
        reference=reference,
        reference_cases=len(reference_runs),
        counterfactual_computed=computed,
        probe_dependent=len(dependent),
        probe_dependent_share=_ratio(len(dependent), computed),
        transitions=transitions,
        counterfactual_distribution=distribution,
        selfcheck_runs=len(selfcheck),
        selfcheck_matches=selfcheck_matches,
        probe_dependent_keys=tuple(_key(case) for case in dependent),
        disagreements_outside_subset=outside,
    )
    return summary, subset_case_set(case_set, dependent)


# ---------------------------------------------------------------------------
# 5. Trace facts per system
# ---------------------------------------------------------------------------


class AgentTraceStats(BaseModel):
    """What one system's traces say about its model calls and explanation guard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    runs: int = 0
    model_steps: int = 0
    runs_with_model_calls: int = 0
    investigator_calls: int = 0
    explanation_calls: int = 0
    investigator_error_runs: int = 0
    citation_invalid_runs: int = 0
    citation_invalid_ids: int = 0
    citation_unsupported_runs: int = 0
    citation_unsupported_reasons: int = 0
    fallback_runs: int = 0
    fallbacks_by_reason: dict[str, int] = {}
    investigator_error_run_ids: tuple[str, ...] = ()


def _suffix_count(decision_type: str, prefix: str) -> int:
    tail = decision_type[len(prefix) + 1 :]
    try:
        return int(tail)
    except ValueError:
        return 0


def agent_trace_stats(
    conn: sqlite3.Connection, system: str, run_ids: Sequence[str]
) -> AgentTraceStats:
    """Count model calls, investigator errors, dropped citations and fallbacks."""
    model_steps = investigator_calls = explanation_calls = 0
    model_runs: set[str] = set()
    error_runs: set[str] = set()
    invalid_runs: set[str] = set()
    unsupported_runs: set[str] = set()
    fallback_runs: set[str] = set()
    invalid_ids = unsupported_reasons = 0
    fallbacks: dict[str, int] = {}
    for chunk in _chunks(list(run_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT run_id, component, decision_type FROM run_steps "
            f"WHERE run_id IN ({placeholders})",
            tuple(chunk),
        ):
            run_id = str(row["run_id"])
            decision_type = str(row["decision_type"])
            if row["component"] == "model":
                model_steps += 1
                model_runs.add(run_id)
                if decision_type.startswith(STEP_INVESTIGATOR):
                    investigator_calls += 1
                elif decision_type.startswith(STEP_EXPLANATION):
                    explanation_calls += 1
                continue
            if decision_type in (STEP_RUN_FLAG_INVESTIGATOR_ERROR, _LEGACY_INVESTIGATOR_ERROR):
                error_runs.add(run_id)
            elif decision_type.startswith(f"{STEP_CITATION_INVALID}:"):
                invalid_runs.add(run_id)
                invalid_ids += _suffix_count(decision_type, STEP_CITATION_INVALID)
            elif decision_type.startswith(f"{STEP_CITATION_UNSUPPORTED}:"):
                unsupported_runs.add(run_id)
                unsupported_reasons += _suffix_count(decision_type, STEP_CITATION_UNSUPPORTED)
            elif decision_type.startswith(f"{STEP_EXPLANATION_FALLBACK}:"):
                fallback_runs.add(run_id)
                why = decision_type[len(STEP_EXPLANATION_FALLBACK) + 1 :] or "(unspecified)"
                fallbacks[why] = fallbacks.get(why, 0) + 1
    return AgentTraceStats(
        system=system,
        runs=len(run_ids),
        model_steps=model_steps,
        runs_with_model_calls=len(model_runs),
        investigator_calls=investigator_calls,
        explanation_calls=explanation_calls,
        investigator_error_runs=len(error_runs),
        citation_invalid_runs=len(invalid_runs),
        citation_invalid_ids=invalid_ids,
        citation_unsupported_runs=len(unsupported_runs),
        citation_unsupported_reasons=unsupported_reasons,
        fallback_runs=len(fallback_runs),
        fallbacks_by_reason=fallbacks,
        investigator_error_run_ids=tuple(sorted(error_runs)),
    )


def run_probe_costs(
    conn: sqlite3.Connection, run_ids: Sequence[str]
) -> dict[str, tuple[int, float]]:
    """`run_id -> (medium/high probe executions, probe cost points)` from the trace.

    Counts `component='probe'`, `decision_type='probe_run'` rows exactly as
    `rli.eval.metrics.agent_efficiency` does (retries included), so a
    per-case comparison and the per-system totals use one definition.
    """
    costs: dict[str, tuple[int, float]] = {run_id: (0, 0.0) for run_id in run_ids}
    for chunk in _chunks(list(run_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT run_id, probe_name, cost_usd FROM run_steps WHERE run_id IN ({placeholders}) "
            "AND component = 'probe' AND decision_type = ?",
            (*chunk, STEP_PROBE_RUN),
        ):
            run_id = str(row["run_id"])
            mh, points = costs.get(run_id, (0, 0.0))
            tier = cost_tier_for(row["probe_name"])
            costs[run_id] = (
                mh + (1 if tier in ("medium", "high") else 0),
                points + float(row["cost_usd"] or 0.0),
            )
    return costs


def policy_branch_counts(conn: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, int]:
    """`policy_decision:<branch>:<rule>` rows over `run_ids`, counted by branch."""
    counts: dict[str, int] = {}
    prefix = f"{STEP_POLICY_DECISION}:"
    for chunk in _chunks(list(run_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT decision_type FROM run_steps WHERE run_id IN ({placeholders}) "
            "AND decision_type LIKE 'policy_decision:%'",
            tuple(chunk),
        ):
            decision_type = str(row["decision_type"])
            if not decision_type.startswith(prefix):
                continue
            branch = decision_type[len(prefix) :].split(":", 1)[0]
            counts[branch] = counts.get(branch, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 6. Splits read
# ---------------------------------------------------------------------------


class SplitCounts(BaseModel):
    """Cases and runs read, per split, plus what the split gate left out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_splits: tuple[str, ...] = ()
    cases_by_split: dict[str, int] = {}
    runs_by_split: dict[str, dict[str, int]] = {}
    excluded_holdout: int = 0
    unassigned: int = 0
    identity_unresolved_by_split: dict[str, int] = {}
    frozen: bool = False

    @property
    def test_cases_read(self) -> int:
        return self.cases_by_split.get("test", 0)


def split_counts(case_set: MetricsCaseSet, *, frozen: bool) -> SplitCounts:
    cases: dict[str, int] = {}
    runs: dict[str, dict[str, int]] = {}
    for case in case_set.cases:
        cases[case.split] = cases.get(case.split, 0) + 1
        per_system = runs.setdefault(case.split, {})
        for name in case.runs:
            per_system[name] = per_system.get(name, 0) + 1
    return SplitCounts(
        allowed_splits=case_set.allowed_splits,
        cases_by_split=cases,
        runs_by_split=runs,
        excluded_holdout=case_set.excluded_holdout,
        unassigned=case_set.unassigned,
        identity_unresolved_by_split=dict(case_set.identity_unresolved_by_split),
        frozen=frozen,
    )


# ---------------------------------------------------------------------------
# 7. Company holdout disjointness
# ---------------------------------------------------------------------------


class CompanyHoldoutCheck(BaseModel):
    """Do the evaluated company-split dataset's TEST companies appear elsewhere?

    `test_companies` are the companies the dataset's BUILD-TIME company split
    assigns to `test` (`build_time_test_companies`): the stable hash for a
    hash-split dataset, or — for a greedy split, whose assignment drifts as
    the corpus grows — the greedy split RECONSTRUCTED over only the postings
    that existed at the dataset's `created_at` (`reconstructed=True`). Using
    today's assignment instead would call a company "test" because of
    postings first seen after the build.

    A company-holdout result is only clean if none of those companies
    appears in any OTHER non-test replay dataset in the database
    (`breaches`: dataset -> `{"companies": n, "cases": m}`). The evaluated
    dataset's own overlap is reported apart (`self_overlap`): it means the
    dataset's cases and its holdout disagree, which is a different defect.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    applicable: bool = False
    reconstructed: bool = False
    split_method: str | None = None
    built_at: str | None = None
    test_companies: int = 0
    datasets_checked: int = 0
    breaches: dict[str, dict[str, int]] = {}
    self_overlap: dict[str, int] = {}
    #: The evaluated dataset's recorded build-time exclusion (schema v6).
    build_exclusion: str | None = None

    @property
    def clean(self) -> bool:
        return not self.breaches and not self.self_overlap

    def basis(self) -> str:
        if self.reconstructed:
            return (
                f"test companies from the {self.split_method or 'greedy'} company split "
                f"RECONSTRUCTED as of the dataset's build ({self.built_at}), over the postings "
                "that existed then"
            )
        return f"test companies from the stable {self.split_method or 'company-hash'} split"

    def describe(self) -> str:
        if not self.applicable:
            return "company holdout check: n/a (not a company-split dataset)"
        parts = [
            f"company holdout check ({self.basis()}; {self.test_companies} test companies, "
            f"{self.datasets_checked} other non-test dataset(s) checked): "
            + ("CLEAN" if self.clean else "BREACHED")
        ]
        if self.breaches:
            parts.append(
                "test companies appear in other non-test datasets: "
                + ", ".join(
                    f"{name} ({entry['companies']} companies, {entry['cases']} cases)"
                    for name, entry in sorted(self.breaches.items())
                )
            )
        if self.self_overlap:
            parts.append(
                f"the evaluated dataset's OWN non-test cases include "
                f"{self.self_overlap['companies']} of its test companies "
                f"({self.self_overlap['cases']} cases)"
            )
        return " - ".join(parts)


def build_time_test_companies(
    conn: sqlite3.Connection, *, dataset_id: str
) -> tuple[set[str], bool, str | None, str | None]:
    """`(test companies, reconstructed?, split method, created_at)` at the dataset's build."""
    from rli.eval.baseline import load_split_map
    from rli.policy.splits import DEFAULT_SEED, SPLIT_METHOD_COMPANY_HASH

    try:
        header = conn.execute(
            "SELECT * FROM replay_datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - pre-replay schema
        header = None
    if header is None:
        return set(), False, None, None
    keys = set(header.keys())
    method = header["split_method"] if "split_method" in keys else None
    seed = (
        header["split_seed"]
        if "split_seed" in keys and header["split_seed"] is not None
        else DEFAULT_SEED
    )
    created_text = str(header["created_at"])
    try:
        created = parse_utc(created_text)
    except ValueError:  # pragma: no cover - corrupt header
        return set(), False, method, created_text
    hashed = method == SPLIT_METHOD_COMPANY_HASH
    mapping = load_split_map(
        conn,
        cutoff=created,
        seed=int(seed),
        split_kind="company",
        company_method="hash" if hashed else "greedy",
        as_of=None if hashed else created,
    )
    test_postings = {posting for posting, split in mapping.items() if split == "test"}
    companies: set[str] = set()
    for row in conn.execute("SELECT posting_id, company_id FROM postings"):
        if row["posting_id"] in test_postings and row["company_id"] is not None:
            companies.add(str(row["company_id"]))
    return companies, not hashed, method or "company-greedy", created_text


def company_holdout_check(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    split_kind: str,
) -> CompanyHoldoutCheck:
    """The company-holdout disjointness check `rli.eval.evaluate` reports."""
    try:
        header = conn.execute(
            "SELECT * FROM replay_datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - pre-replay schema
        header = None
    build_exclusion = None
    if header is not None and "company_holdout" in header.keys():
        build_exclusion = header["company_holdout"]
    if split_kind != "company":
        return CompanyHoldoutCheck(applicable=False, build_exclusion=build_exclusion)

    test_companies, reconstructed, method, built_at = build_time_test_companies(
        conn, dataset_id=dataset_id
    )

    breaches: dict[str, dict[str, int]] = {}
    self_overlap: dict[str, int] = {}
    datasets = 0
    try:
        rows = conn.execute(
            """
            SELECT c.dataset_id AS dataset_id, c.company_id AS company_id, COUNT(*) AS n
            FROM replay_cases AS c
            JOIN replay_datasets AS d ON d.dataset_id = c.dataset_id
            WHERE d.split_name != 'test' AND (c.split IS NULL OR c.split != 'test')
            GROUP BY c.dataset_id, c.company_id
            """
        ).fetchall()
        datasets = int(
            conn.execute(
                "SELECT COUNT(*) FROM replay_datasets WHERE split_name != 'test' "
                "AND dataset_id != ?",
                (dataset_id,),
            ).fetchone()[0]
        )
    except sqlite3.Error:  # pragma: no cover - pre-replay schema
        rows = []
    for row in rows:
        if str(row["company_id"]) not in test_companies:
            continue
        if str(row["dataset_id"]) == dataset_id:
            self_overlap["companies"] = self_overlap.get("companies", 0) + 1
            self_overlap["cases"] = self_overlap.get("cases", 0) + int(row["n"])
            continue
        entry = breaches.setdefault(str(row["dataset_id"]), {"companies": 0, "cases": 0})
        entry["companies"] += 1
        entry["cases"] += int(row["n"])
    return CompanyHoldoutCheck(
        applicable=True,
        reconstructed=reconstructed,
        split_method=method,
        built_at=built_at,
        test_companies=len(test_companies),
        datasets_checked=datasets,
        breaches=breaches,
        self_overlap=self_overlap,
        build_exclusion=build_exclusion,
    )
