"""Replay a system over a dataset (spec.md §6 "Replay"; PLAN.md M4).

`rli.replay.build` writes the cached full-probe record; this module spends
it. Given a dataset id and a system, it walks every `(posting, T)` case and
runs that system exactly as a live run would — same case-state builder, same
frozen action policy, same trace — with four substitutions, none of which the
system itself can see:

1. the probe results come from `replay_probe_results`, not the network
   (`rli.replay.mode.ReplayProbeRunner`);
2. every claim with `available_at > T` is dropped before it becomes evidence
   (the same class's `save_evidence`);
3. the corpus the system reads is restricted to observations made at or
   before `T` (`rli.replay.pit`);
4. the network is structurally unreachable — any attempt raises
   `ReplayViolation` and is written into the trace
   (`rli.replay.mode.ReplayNetClient`).

The system under test is passed in, not branched on: `SYSTEM_RUNNERS` maps
`'A'` and `'B'` to `rli.eval.system_a` / `rli.eval.system_b`, and any callable
with the same keyword contract can be handed in as `runner=` — which is how
System C (PLAN.md M5) is replayed on this dataset without this module
learning anything about it.

--------------------------------------------------------------------------
Cases are walked in `T` order, one point-in-time corpus per `T`
--------------------------------------------------------------------------

Installing `rli.replay.pit`'s view set is cheap; re-deriving each company's
interval-censored lifecycle inside it is not. So the walk is ordered by `T`
(which `rli.replay.build.dataset_case_rows` already guarantees) and one
context is opened per distinct `T`, shared by every case at that instant.
The alternative — a context per case — would re-derive the same lifecycles
once per posting instead of once per `T`.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **A re-run REPLACES the previous replay runs of the same
  `(dataset, system)` by default.** Replaying is the thing you do repeatedly
  while a system is being changed, and `rli.eval.baseline` pairs cases by
  `(input_url, replay_at)` and collapses duplicates by taking the greatest
  `(started_at, id)`. Every replay run of one `T` has the SAME `started_at`
  (it is `T`), so that tiebreak degenerates to "greatest uuid" — i.e. a
  re-run would leave the report picking between two decisions essentially at
  random. Deleting the previous set makes a re-run idempotent and the
  pairing unambiguous. `replace=False` keeps them for a caller that really
  wants both, and accepts that ambiguity knowingly.

* **A `ReplayViolation` fails ONE case, not the whole run.** The exception
  has already been recorded in that run's trace (a `replay_violation:` step)
  and the run has already been marked `'failed'` by `Run.__exit__` before it
  reaches here, so the evidence a leakage audit needs is durable. Stopping
  the whole walk would hide every other violation behind the first one, and
  `rli.replay.leakage` is specifically a COUNTING tool ("future-leakage
  violations (`0` target)" — spec.md §6). Every violation is counted and its
  message kept; a summary with a nonzero `violations` is a failed replay and
  the CLI exits nonzero on it.

* **The dataset's own `posting_id` keys the record lookup, not the one the
  running system re-derives from the URL.** The two can legitimately differ:
  `rli.history.closures` creates `"archive:{company}:{job}"` postings that
  `rli.eval.case._resolve_identity` cannot reconstruct from a job URL. See
  `rli.replay.mode.ReplayProbeRunner`'s docstring — this module is the caller
  that supplies it.

* **`replay_at` is passed as the system's `now`.** A replayed run's decision
  clock IS `T` (spec.md §6), which is what makes `recheck_after_days`,
  `publish_recency` and every trace timestamp a function of the dataset
  rather than of when the replay happened to be executed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.runner import ReplayHook, RunResult, SystemName
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.models.time import parse_utc, to_utc_z
from rli.replay.build import archive_state_args_hash, dataset_case_rows
from rli.replay.mode import (
    ARCHIVE_BOARD_STATE_PROBE,
    ReplayContext,
    ReplayProbeStore,
    ReplayViolation,
    replay_hook,
)
from rli.replay.pit import point_in_time

__all__ = [
    "SYSTEM_RUNNERS",
    "CaseOutcome",
    "ReplayRunSummary",
    "SystemRunner",
    "clear_replay_runs",
    "run_replay",
]


class SystemRunner(Protocol):
    """The call shape every replayable system exposes.

    `rli.eval.system_a.run_system_a` and `rli.eval.system_b.run_system_b`
    satisfy it as written; System C (PLAN.md M5) is replayed by passing any
    callable that does. Keeping this a `Protocol` rather than a base class is
    what lets `rli.replay` stay unaware of `rli.agent`.
    """

    def __call__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        url: str,
        *,
        now: datetime | None = ...,
        replay: ReplayHook | None = ...,
        collection_status_csv: str | Path | None = ...,
    ) -> RunResult: ...


#: The systems this module can replay without being told how (spec.md §6).
SYSTEM_RUNNERS: dict[str, Callable[..., RunResult]] = {
    "A": run_system_a,
    "B": run_system_b,
}


class CaseOutcome(BaseModel):
    """What one `(posting, T)` case produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    posting_id: str
    replay_at: str
    input_url: str
    run_id: str | None = None
    action: str | None = None
    posting_state: str | None = None
    evidence_quality: str | None = None
    probes_run: tuple[str, ...] = ()
    exposed_probes: tuple[str, ...] = ()
    archive_state_claims: int = 0
    error: str | None = None


class ReplayRunSummary(BaseModel):
    """One `run_replay` call, aggregated (spec.md §6 agent-efficiency inputs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    system: str
    cases: int = 0
    completed: int = 0
    violations: int = 0
    errors: int = 0
    replaced_runs: int = 0

    action_distribution: dict[str, int] = {}
    posting_state_distribution: dict[str, int] = {}
    evidence_quality_distribution: dict[str, int] = {}
    probe_counts: dict[str, int] = {}
    cases_with_archive_state: int = 0

    outcomes: tuple[CaseOutcome, ...] = ()

    def describe(self) -> str:
        lines = [
            f"replay {self.system} over dataset {self.dataset_id!r}: "
            f"cases={self.cases} completed={self.completed} "
            f"violations={self.violations} errors={self.errors} "
            f"(replaced {self.replaced_runs} previous run(s))",
            f"  actions: {_render(self.action_distribution)}",
            f"  posting_state: {_render(self.posting_state_distribution)}",
            f"  evidence_quality: {_render(self.evidence_quality_distribution)}",
            f"  dynamic probes run: {_render(self.probe_counts)}",
            f"  cases with an observable archive board state: "
            f"{self.cases_with_archive_state}/{self.cases}",
        ]
        for outcome in self.outcomes:
            if outcome.error is not None:
                lines.append(f"    ! {outcome.posting_id} @ {outcome.replay_at}: {outcome.error}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _render(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def clear_replay_runs(conn: sqlite3.Connection, *, dataset_id: str, system: str) -> int:
    """Delete this dataset's previous replay runs for `system`. Returns the count.

    Removes `evidence` and `run_steps` first (both reference `runs`), then the
    `runs` rows themselves. Only rows that are `mode='replay'`, this system,
    and whose `config_hash` ends in this dataset's `|dataset:<id>` suffix —
    the same identity `rli.eval.baseline.collect_cases` uses, so "what this
    deletes" and "what the report would have counted" are the same set by
    construction. Live runs, other systems and other datasets are untouched.
    """
    suffix = f"|dataset:{dataset_id}"
    run_ids = [
        row["id"]
        for row in conn.execute(
            """
            SELECT id, config_hash FROM runs
            WHERE mode = 'replay' AND system = ? AND config_hash IS NOT NULL
            """,
            (system,),
        ).fetchall()
        if str(row["config_hash"]).endswith(suffix)
    ]
    if not run_ids:
        return 0

    placeholders = ",".join("?" for _ in run_ids)
    conn.execute(f"DELETE FROM evidence WHERE run_id IN ({placeholders})", run_ids)
    conn.execute(f"DELETE FROM run_steps WHERE run_id IN ({placeholders})", run_ids)
    conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", run_ids)
    conn.commit()
    return len(run_ids)


def _group_by_replay_at(rows: Sequence[sqlite3.Row]) -> list[tuple[str, list[sqlite3.Row]]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["replay_at"], []).append(row)
    # `replay_at` is a `to_utc_z` string, so lexical order is chronological
    # order (`rli.models.time`); no parsing needed to walk the grid in order.
    return sorted(groups.items())


def run_replay(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    system: SystemName | str,
    runner: Callable[..., RunResult] | None = None,
    limit_cases: int | None = None,
    collection_status_csv: str | Path | None = None,
    replace: bool = True,
) -> ReplayRunSummary:
    """Replay `system` over every case of `dataset_id` (spec.md §6).

    Arguments:
        system: `'A'`, `'B'`, or any `runs.system` value when `runner` is
            given (`'C'` / `'C2'` for PLAN.md M5). The value is what lands in
            `runs.system`, so it must be one the schema's CHECK allows.
        runner: the callable to replay. Defaults to `SYSTEM_RUNNERS[system]`;
            required for a system this module does not ship.
        limit_cases: stop after this many cases. For a smoke run; the
            selection is the dataset's own `(T, posting_id)` order, so a
            truncated replay is a prefix of the grid, not a sample of it —
            do not report metrics from one.
        collection_status_csv: pinned company-event collection state
            (`rli.eval.case._event_signals`). Pass the same value the build
            used, or a replay reads whatever the working checkout holds today.
        replace: delete this dataset's previous replay runs for `system`
            first (see the module docstring).

    Raises `LookupError` if the dataset has no cases, and `ValueError` for a
    system with no runner.
    """
    rows = dataset_case_rows(conn, dataset_id)
    if not rows:
        raise LookupError(
            f"replay dataset {dataset_id!r} has no cases; build it first (`rli replay build`)"
        )

    system_name = str(system)
    execute = runner if runner is not None else SYSTEM_RUNNERS.get(system_name)
    if execute is None:
        raise ValueError(
            f"no runner for system {system_name!r}; pass runner= explicitly "
            f"(this module ships {sorted(SYSTEM_RUNNERS)})"
        )

    replaced = clear_replay_runs(conn, dataset_id=dataset_id, system=system_name) if replace else 0

    outcomes: list[CaseOutcome] = []
    actions: dict[str, int] = {}
    states: dict[str, int] = {}
    qualities: dict[str, int] = {}
    probe_counts: dict[str, int] = {}
    completed = 0
    violations = 0
    errors = 0
    archive_cases = 0
    seen = 0

    for stamp, group in _group_by_replay_at(rows):
        if limit_cases is not None and seen >= limit_cases:
            break
        replay_at = parse_utc(stamp)
        # Only the companies that actually have a case at this `T`: the
        # lifecycle re-derivation is the expensive half of `point_in_time`,
        # and a case only ever reads its own company's history. On a grid
        # whose points are per-posting (each starts at that posting's
        # `first_observed`) this is usually a single company per `T`.
        with point_in_time(
            conn, replay_at, company_ids=sorted({row["company_id"] for row in group})
        ):
            for row in group:
                if limit_cases is not None and seen >= limit_cases:
                    break
                seen += 1
                outcome = _replay_one(
                    conn,
                    cfg,
                    execute,
                    dataset_id=dataset_id,
                    posting_id=row["posting_id"],
                    url=row["canonical_url"],
                    replay_at=replay_at,
                    collection_status_csv=collection_status_csv,
                )
                outcomes.append(outcome)
                if outcome.archive_state_claims:
                    archive_cases += 1
                if outcome.error is not None:
                    errors += 1
                    if "ReplayViolation" in outcome.error:
                        violations += 1
                    continue
                completed += 1
                _bump(actions, outcome.action or "(missing)")
                _bump(states, outcome.posting_state or "(missing)")
                _bump(qualities, outcome.evidence_quality or "(missing)")
                for name in outcome.probes_run:
                    _bump(probe_counts, name)

    return ReplayRunSummary(
        dataset_id=dataset_id,
        system=system_name,
        cases=seen,
        completed=completed,
        violations=violations,
        errors=errors,
        replaced_runs=replaced,
        action_distribution=actions,
        posting_state_distribution=states,
        evidence_quality_distribution=qualities,
        probe_counts=probe_counts,
        cases_with_archive_state=archive_cases,
        outcomes=tuple(outcomes),
    )


def _replay_one(
    conn: sqlite3.Connection,
    cfg: Config,
    execute: Callable[..., RunResult],
    *,
    dataset_id: str,
    posting_id: str,
    url: str,
    replay_at: datetime,
    collection_status_csv: str | Path | None,
) -> CaseOutcome:
    """Replay one `(posting, T)` case. Never raises for a case-level failure."""
    store = ReplayProbeStore()
    archive_claims = store.claims(
        conn,
        dataset_id=dataset_id,
        posting_id=posting_id,
        replay_at=replay_at,
        probe_name=ARCHIVE_BOARD_STATE_PROBE,
        args_hash=archive_state_args_hash(posting_id, replay_at),
    )
    hook = replay_hook(
        replay=ReplayContext(T=replay_at, dataset_id=dataset_id),
        store=store,
        posting_id=posting_id,
        archive_claims=archive_claims,
    )

    try:
        result = execute(
            conn,
            cfg,
            url,
            now=replay_at,
            replay=hook,
            collection_status_csv=collection_status_csv,
        )
    except ReplayViolation as exc:
        return CaseOutcome(
            posting_id=posting_id,
            replay_at=to_utc_z(replay_at),
            input_url=url,
            archive_state_claims=len(archive_claims),
            exposed_probes=tuple(name for name, _ in store.exposed),
            error=f"ReplayViolation: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - one case must not stop the walk
        return CaseOutcome(
            posting_id=posting_id,
            replay_at=to_utc_z(replay_at),
            input_url=url,
            archive_state_claims=len(archive_claims),
            exposed_probes=tuple(name for name, _ in store.exposed),
            error=f"{type(exc).__name__}: {exc}",
        )

    return CaseOutcome(
        posting_id=posting_id,
        replay_at=to_utc_z(replay_at),
        input_url=url,
        run_id=result.run_id,
        action=result.decision.recommended_action,
        posting_state=result.decision.posting_state,
        evidence_quality=result.decision.evidence_quality,
        probes_run=result.probes_run,
        exposed_probes=tuple(name for name, _ in store.exposed),
        archive_state_claims=len(archive_claims),
    )
