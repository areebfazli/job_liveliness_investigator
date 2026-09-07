"""Aggregate one system's runs into the spec.md §6 baseline numbers (PLAN.md M3).

PLAN.md M3's exit condition is "A and B run on live postings; policy frozen;
B versioned"; M4 then asks for "A/B baseline metrics on development/validation
data → `reports/baseline.md`". This module is the read side that makes the
second sentence possible from the trace alone: it re-derives, per system, the
figures spec.md §6 lists under **Agent efficiency** —

    "medium/high-cost probe count, total cost, latency, repeated calls,
    invalid arguments, recovery after failures"

— together with the action distribution spec.md §6 explicitly requires to be
reported alongside any agreement number ("reported with the action
distribution so a default-heavy policy cannot pass trivially").

It is a pure reader: `summarize_runs` opens no network connection, runs no
probe, and writes nothing. Everything comes from `runs` and `run_steps`,
which spec.md §7 calls "the canonical trace".

--------------------------------------------------------------------------
What is deliberately NOT computed here
--------------------------------------------------------------------------

**Action agreement with A.** spec.md §6's headline metric compares each
system's decision against A's decision *for the same posting at the same
time*, which needs a pairing rule (same posting? same input URL? same
replay `T`?) and the holdout splits of PLAN.md M6. Guessing a pairing here
would produce an authoritative-looking number computed on the wrong join.
This module reports each system's own figures; the comparison belongs to the
evaluation report that owns the splits.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **Medium and high cost tiers are reported separately, and together.**
  spec.md §6's agent gate counts "medium/high-cost probe use", so the pair
  is what the gate reads; the split is kept because a system that trades one
  `high` probe for three `medium` ones looks identical in the sum and is not.
  The always-run pair is counted too (both `low`), under its own heading:
  every system runs it, so it belongs in the absolute cost figure spec.md §6
  also asks for, but never in the gate's numerator.
* **A probe's cost tier is read from its class, not stored per step.**
  `run_steps` records `cost_usd`, not a tier. Mapping `probe_name` back
  through `rli.probes.registry.DYNAMIC_PROBES` (plus the always-run pair)
  means a retier of a probe re-labels historical steps — acceptable, and
  visible, because `cost_usd` is stored per step and does not move. A probe
  name this checkout does not know is bucketed as `unknown` rather than
  dropped: a summary that silently omits steps would understate cost.
* **Latency is the summed step latency, in milliseconds.** That is what
  `runs.total_latency_ms` holds and what `rli.eval.runner` documents it to
  mean (a LOWER bound on user-visible latency, chosen so the figure is
  attributable per probe). `describe()` says so in the printed output rather
  than letting a reader assume wall clock.
* **A run with no `final_decision` is counted, not skipped.** A run that
  failed (or was stopped) before deciding has a NULL `final_decision`; it
  still consumed probes and cost. It is excluded only from the two
  distributions it cannot contribute to, and `decisions_missing` reports how
  many runs that was — so a summary can never quietly average over a subset
  it does not name. A `final_decision` that is present but unparseable is
  counted the same way, because a corrupt stored blob is not a reason to
  fail a report.
* **Means are per RUN, not per step**, and are `0.0` for an empty set rather
  than an error, so an empty database describes cleanly.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.eval.runner import STEP_PROBE_RUN, SystemName
from rli.probes.board_snapshot import BoardSnapshotProbe
from rli.probes.registry import DYNAMIC_PROBES
from rli.probes.resolve_posting import ResolvePostingProbe

__all__ = ["RunsSummary", "cost_tier_for", "summarize_runs"]

CostTierBucket = Literal["low", "medium", "high", "unknown"]

# probe name -> cost tier, for the four dynamic probes plus the always-run
# pair. Built from the classes so a retier or a fifth probe needs no edit.
_COST_TIERS: dict[str, str] = {
    **{name: probe_cls.cost_tier for name, probe_cls in DYNAMIC_PROBES.items()},
    ResolvePostingProbe.name: ResolvePostingProbe.cost_tier,
    BoardSnapshotProbe.name: BoardSnapshotProbe.cost_tier,
}

_ALWAYS_RUN = (ResolvePostingProbe.name, BoardSnapshotProbe.name)


def cost_tier_for(probe_name: str | None) -> CostTierBucket:
    """The cost tier of `probe_name`, or `'unknown'` for a name we do not know."""
    if probe_name is None:
        return "unknown"
    tier = _COST_TIERS.get(probe_name)
    if tier in ("low", "medium", "high"):
        return tier  # type: ignore[return-value]
    return "unknown"


class RunsSummary(BaseModel):
    """One system's runs, aggregated from `runs` + `run_steps`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str

    runs: int = 0
    completed: int = 0
    failed: int = 0
    running: int = 0
    stopped: int = 0
    decisions_missing: int = 0

    # From `runs.final_decision` (spec.md §1 shape).
    action_distribution: dict[str, int] = {}
    evidence_quality_distribution: dict[str, int] = {}

    # From `run_steps` rows with component='probe'.
    probe_counts: dict[str, int] = {}
    probe_counts_by_tier: dict[str, int] = {}
    dynamic_probe_steps: int = 0
    always_run_probe_steps: int = 0
    # spec.md §6's agent gate numerator.
    medium_high_probe_steps: int = 0

    mean_cost_per_run: float = 0.0
    mean_latency_ms_per_run: float = 0.0
    mean_medium_high_probes_per_run: float = 0.0

    # `run_steps.error IS NOT NULL`, by probe name (NULL name -> 'controller').
    failure_counts: dict[str, int] = {}
    failed_steps: int = 0

    def describe(self) -> str:
        """Human-readable multi-line summary, in the style of `DailySnapshotSummary`."""
        lines = [
            f"system {self.system}: runs={self.runs} completed={self.completed} "
            f"failed={self.failed} running={self.running} stopped={self.stopped}",
            f"  actions: {_render(self.action_distribution)}",
            f"  evidence_quality: {_render(self.evidence_quality_distribution)}",
            f"  probe steps: {_render(self.probe_counts)}",
            f"  by cost tier: {_render(self.probe_counts_by_tier)}",
            f"  always-run steps={self.always_run_probe_steps} "
            f"dynamic steps={self.dynamic_probe_steps} "
            f"medium/high steps={self.medium_high_probe_steps} "
            f"(spec.md §6 agent-gate numerator)",
            f"  mean per run: cost={self.mean_cost_per_run:.2f} cost points "
            f"(placeholder units, see rli.config.ProbeCosts) | "
            f"latency={self.mean_latency_ms_per_run:.0f} ms "
            f"(summed step latency, a lower bound on wall clock) | "
            f"medium/high probes={self.mean_medium_high_probes_per_run:.2f}",
            f"  failures: {self.failed_steps} step(s) — {_render(self.failure_counts)}",
        ]
        if self.decisions_missing:
            lines.append(
                f"  NOTE: {self.decisions_missing} run(s) have no parsable "
                "final_decision and are excluded from the two distributions above"
            )
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _render(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def summarize_runs(conn: sqlite3.Connection, system: SystemName | str) -> RunsSummary:
    """Aggregate every `runs` row for `system` (see the module docstring).

    Tolerates a NULL or unparseable `runs.final_decision` (a run that failed
    before deciding) — such runs are counted in `runs` and in
    `decisions_missing`, and contribute their probe steps and cost like any
    other, but not to the action/quality distributions.
    """
    run_rows = conn.execute(
        """
        SELECT id, status, final_decision, total_cost_usd, total_latency_ms
        FROM runs
        WHERE system = ?
        ORDER BY started_at, id
        """,
        (system,),
    ).fetchall()

    status_counts: dict[str, int] = {}
    actions: dict[str, int] = {}
    qualities: dict[str, int] = {}
    decisions_missing = 0
    total_cost = 0.0
    total_latency_ms = 0.0

    for row in run_rows:
        _bump(status_counts, str(row["status"]))
        total_cost += float(row["total_cost_usd"] or 0.0)
        total_latency_ms += float(row["total_latency_ms"] or 0)

        payload = row["final_decision"]
        decoded = _decode_decision(payload)
        if decoded is None:
            decisions_missing += 1
            continue
        action = decoded.get("recommended_action")
        quality = decoded.get("evidence_quality")
        # A stored decision always carries both (they are required fields of
        # `rli.models.decision.Decision`); anything else is a hand-written or
        # truncated row and is reported as such rather than crashing.
        _bump(actions, str(action) if action is not None else "(missing)")
        _bump(qualities, str(quality) if quality is not None else "(missing)")

    probe_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    failure_counts: dict[str, int] = {}
    failed_steps = 0
    dynamic_steps = 0
    always_run_steps = 0
    medium_high_steps = 0

    step_rows = conn.execute(
        """
        SELECT s.probe_name AS probe_name, s.component AS component,
               s.decision_type AS decision_type, s.error AS error
        FROM run_steps s
        JOIN runs r ON r.id = s.run_id
        WHERE r.system = ?
        """,
        (system,),
    ).fetchall()

    for row in step_rows:
        name = row["probe_name"]
        if row["error"] is not None:
            failed_steps += 1
            _bump(failure_counts, str(name) if name else "(controller)")

        # Only executions count as probe steps; a `probe_skipped` controller
        # row names a probe but did not run it.
        if row["component"] != "probe" or row["decision_type"] != STEP_PROBE_RUN:
            continue

        key = str(name) if name else "(unnamed)"
        _bump(probe_counts, key)
        tier = cost_tier_for(name)
        _bump(tier_counts, tier)
        if key in _ALWAYS_RUN:
            always_run_steps += 1
        else:
            dynamic_steps += 1
        if tier in ("medium", "high"):
            medium_high_steps += 1

    count = len(run_rows)
    return RunsSummary(
        system=str(system),
        runs=count,
        completed=status_counts.get("completed", 0),
        failed=status_counts.get("failed", 0),
        running=status_counts.get("running", 0),
        stopped=status_counts.get("stopped", 0),
        decisions_missing=decisions_missing,
        action_distribution=actions,
        evidence_quality_distribution=qualities,
        probe_counts=probe_counts,
        probe_counts_by_tier=tier_counts,
        dynamic_probe_steps=dynamic_steps,
        always_run_probe_steps=always_run_steps,
        medium_high_probe_steps=medium_high_steps,
        mean_cost_per_run=(total_cost / count) if count else 0.0,
        mean_latency_ms_per_run=(total_latency_ms / count) if count else 0.0,
        mean_medium_high_probes_per_run=(medium_high_steps / count) if count else 0.0,
        failure_counts=failure_counts,
        failed_steps=failed_steps,
    )


def _decode_decision(payload: object) -> dict[str, object] | None:
    """Parse a stored `runs.final_decision`, or `None` if there is nothing usable."""
    if not isinstance(payload, str) or not payload.strip():
        return None
    try:
        decoded = json.loads(payload)
    except ValueError:
        # A corrupt blob is stored data, not a caller error: degrade to
        # "missing" rather than failing the whole report.
        return None
    return decoded if isinstance(decoded, dict) else None
