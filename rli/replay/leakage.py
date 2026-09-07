"""The future-leakage checker (spec.md §6 "Metrics"; PLAN.md M4).

spec.md §6 lists, under **Data quality**, "future-leakage violations (`0`
target)". This module is how that number is produced: it audits the canonical
trace (spec.md §7 — `runs` + `run_steps` + `evidence`) of a replay dataset and
reports every way a replayed run could have seen something it must not have.

It is a pure READER. It runs no probe, opens no connection of its own, and
writes nothing. That matters: a checker that could modify state could mask
the thing it is checking for, and this is the one component whose output is
allowed to gate a milestone.

--------------------------------------------------------------------------
The four checks, and why each is the right question
--------------------------------------------------------------------------

* **`evidence_after_t`** — an `evidence` row of a replay run whose
  `available_at` is strictly after that run's `replay_at`. This is spec.md
  §6's first replay rule read back out of the database: "expose only evidence
  with `available_at <= T`". It is checked against STORED rows rather than
  against the in-memory list the gate filtered, because the gate and the
  store are different code paths and only the store is what a later report
  reads.

* **`cache_miss`** — a `run_steps` row of a replay run with
  `cache_status = 'miss'`. `rli.eval.runner` defines `'miss'` as "at least
  one call reached the network", and `rli.replay.mode` writes `'hit'`
  unconditionally for a served record precisely so that a `'miss'` in a
  replay run can mean exactly one thing: a probe executed through the LIVE
  path. That is the third replay rule ("live tool calls are forbidden in
  replay") caught structurally, from the trace, without trusting any
  in-process bookkeeping.

* **`net_call`** — a `replay_violation:net_call` controller step. The
  forbidden-network client (`rli.replay.mode.ReplayNetClient`) writes one
  before it raises, so an attempt is durable even though the call never
  happened. `cache_miss` and `net_call` are deliberately both kept: the first
  catches a probe that reached the network *successfully* through a
  misconfigured runner, the second catches one that tried and was stopped.
  Zero of the first and nonzero of the second means the guard is working.

* **`missing_probe_result`** — a `replay_violation:missing_probe_result`
  step. Not a leak; a dataset GAP. It is reported here because it is the
  other way a replay's numbers can be silently wrong, and because
  `rli.replay.mode` is explicit that a gap must be loud rather than degrade
  into a plausible-looking metric. A run that hit one is `'failed'`, so it
  would otherwise only show up as a slightly smaller denominator.

A fifth counter, `failed_runs`, is reported but is NOT a violation: a run can
fail for reasons that have nothing to do with leakage, and conflating the two
would make the `0` target unreachable for the wrong reason. It is surfaced so
a clean report can never hide a broken replay.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **Scope is `(mode='replay', config_hash ends with '|dataset:<id>')`**, the
  same identity `rli.eval.baseline.collect_cases` and
  `rli.replay.run.clear_replay_runs` use. Three modules agreeing on one
  definition of "this dataset's runs" is what makes "leakage 0" and "the
  baseline report" statements about the same set of rows.

* **`available_at > replay_at` is a STRING comparison, done in SQL.** Both
  columns are `rli.models.time.to_utc_z` values, whose fixed-width
  microsecond fraction makes lexical order chronological order (that module
  documents exactly this invariant, and exists for it). Parsing 100k rows in
  Python to compare them would be slower and would introduce a second
  timestamp semantics.

* **A replay run with a NULL `replay_at` is itself a violation**
  (`missing_replay_at`), not a row to skip. Without `T` there is no window,
  so nothing about that run can be audited — and an unauditable run inside an
  audited dataset is the failure mode this checker exists to prevent.

* **`net_call_count` takes a live `ReplayNetPool`, not the database.** It is
  the in-process counterpart used by tests and by a caller that wants to
  assert "zero network attempts" for one run without waiting for it to
  commit. `rli.replay.mode` names it in its own docstring; the durable
  version of the same fact is the `net_call` check above.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from rli.replay.mode import STEP_REPLAY_VIOLATION

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.replay.mode import ReplayNetPool

__all__ = [
    "VIOLATION_KINDS",
    "LeakageReport",
    "Violation",
    "ViolationKind",
    "check_dataset",
    "net_call_count",
]

ViolationKind = str

#: Every violation kind this module can report. Exported so a caller can
#: assert on the set rather than restate the strings.
VIOLATION_KINDS: tuple[str, ...] = (
    "evidence_after_t",
    "cache_miss",
    "net_call",
    "missing_probe_result",
    "missing_replay_at",
)


class Violation(BaseModel):
    """One leakage finding, with enough context to reproduce it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ViolationKind
    run_id: str
    system: str
    replay_at: str | None = None
    posting_id: str | None = None
    detail: str = ""

    def describe(self) -> str:
        where = f"{self.system} run {self.run_id}"
        if self.replay_at is not None:
            where += f" @ T={self.replay_at}"
        return f"[{self.kind}] {where}: {self.detail}"


class LeakageReport(BaseModel):
    """The spec.md §6 "future-leakage violations (0 target)" figure, itemized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    systems: tuple[str, ...] = ()
    runs_checked: int = 0
    evidence_checked: int = 0
    steps_checked: int = 0
    failed_runs: int = 0

    counts: dict[str, int] = {}
    violations: tuple[Violation, ...] = ()
    # Violations are capped in the itemized list so one systematic bug cannot
    # produce a million-line report; the COUNTS above are never capped.
    truncated: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def clean(self) -> bool:
        """True when spec.md §6's `0` target is met for this dataset."""
        return self.total == 0

    def describe(self) -> str:
        verdict = "CLEAN (0 violations)" if self.clean else f"{self.total} VIOLATION(S)"
        lines = [
            f"leakage check: dataset={self.dataset_id!r} "
            f"systems={', '.join(self.systems) or '(none)'} -> {verdict}",
            f"  runs checked={self.runs_checked} evidence rows={self.evidence_checked} "
            f"trace steps={self.steps_checked} failed runs={self.failed_runs}",
        ]
        if self.counts:
            lines.append(
                "  by kind: "
                + " ".join(f"{kind}={count}" for kind, count in sorted(self.counts.items()))
            )
        lines.extend(f"  {violation.describe()}" for violation in self.violations)
        if self.truncated:
            lines.append(f"  ... and {self.truncated} more (itemization capped)")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def net_call_count(pool: ReplayNetPool) -> int:
    """How many forbidden live tool calls this replay pool refused.

    The in-process counterpart of the `net_call` trace check: `> 0` means a
    probe tried to reach the network from replay mode. It counts ATTEMPTS,
    not successes — `rli.replay.mode.ReplayNetClient` cannot make a request
    at all — so a nonzero value is a wiring bug in the system under test, not
    a leak that already happened.
    """
    return len(pool.attempts)


def _dataset_runs(
    conn: sqlite3.Connection, dataset_id: str
) -> list[sqlite3.Row]:
    suffix = f"|dataset:{dataset_id}"
    rows = conn.execute(
        """
        SELECT id, system, replay_at, posting_id, status, config_hash
        FROM runs
        WHERE mode = 'replay' AND config_hash IS NOT NULL
        ORDER BY replay_at, system, id
        """
    ).fetchall()
    return [row for row in rows if str(row["config_hash"]).endswith(suffix)]


def check_dataset(
    conn: sqlite3.Connection, dataset_id: str, *, max_items: int = 50
) -> LeakageReport:
    """Audit every replay run of `dataset_id` (see the module docstring).

    `max_items` caps the ITEMIZED violation list only; `counts` and `total`
    are always complete, so `report.clean` is never an artefact of the cap.
    """
    runs = _dataset_runs(conn, dataset_id)
    by_id = {row["id"]: row for row in runs}
    counts: dict[str, int] = {}
    found: list[Violation] = []
    evidence_checked = 0
    steps_checked = 0
    failed_runs = sum(1 for row in runs if row["status"] == "failed")

    def record(kind: str, run: sqlite3.Row, detail: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        if len(found) < max_items:
            found.append(
                Violation(
                    kind=kind,
                    run_id=run["id"],
                    system=str(run["system"]),
                    replay_at=run["replay_at"],
                    posting_id=run["posting_id"],
                    detail=detail,
                )
            )

    for run in runs:
        if run["replay_at"] is None:
            record(
                "missing_replay_at",
                run,
                "a replay run with no replay_at defines no `available_at <= T` "
                "window and cannot be audited at all",
            )

    if not by_id:
        return LeakageReport(dataset_id=dataset_id, counts=counts, violations=tuple(found))

    placeholders = ",".join("?" for _ in by_id)
    run_ids = list(by_id)

    # -- rule 1: available_at <= T ----------------------------------------
    evidence_checked = int(
        conn.execute(
            f"SELECT COUNT(*) FROM evidence WHERE run_id IN ({placeholders})", run_ids
        ).fetchone()[0]
    )
    leaked = conn.execute(
        f"""
        SELECT e.run_id AS run_id, e.id AS evidence_id, e.probe AS probe,
               e.claim_type AS claim_type, e.available_at AS available_at,
               r.replay_at AS replay_at
        FROM evidence AS e
        JOIN runs AS r ON r.id = e.run_id
        WHERE e.run_id IN ({placeholders})
          AND r.replay_at IS NOT NULL
          AND e.available_at > r.replay_at
        ORDER BY e.run_id, e.id
        """,
        run_ids,
    ).fetchall()
    for row in leaked:
        record(
            "evidence_after_t",
            by_id[row["run_id"]],
            f"evidence {row['evidence_id']} from probe {row['probe']!r} "
            f"({row['claim_type']}) has available_at={row['available_at']} > "
            f"T={row['replay_at']}",
        )

    # -- rules 3 and the dataset-gap check --------------------------------
    steps_checked = int(
        conn.execute(
            f"SELECT COUNT(*) FROM run_steps WHERE run_id IN ({placeholders})", run_ids
        ).fetchone()[0]
    )
    for row in conn.execute(
        f"""
        SELECT run_id, step_index, probe_name, cache_status, decision_type, error
        FROM run_steps
        WHERE run_id IN ({placeholders})
          AND (cache_status = 'miss' OR decision_type LIKE ?)
        ORDER BY run_id, step_index
        """,
        (*run_ids, f"{STEP_REPLAY_VIOLATION}:%"),
    ).fetchall():
        run = by_id[row["run_id"]]
        if row["cache_status"] == "miss":
            record(
                "cache_miss",
                run,
                f"step {row['step_index']} (probe={row['probe_name']!r}) recorded "
                "cache_status='miss', i.e. at least one call reached the network",
            )
        decision_type = str(row["decision_type"])
        if decision_type == f"{STEP_REPLAY_VIOLATION}:net_call":
            record(
                "net_call",
                run,
                f"step {row['step_index']}: {row['error']}",
            )
        elif decision_type == f"{STEP_REPLAY_VIOLATION}:missing_probe_result":
            record(
                "missing_probe_result",
                run,
                f"step {row['step_index']} (probe={row['probe_name']!r}): "
                "the dataset has no cached result for this probe at this T",
            )

    total = sum(counts.values())
    return LeakageReport(
        dataset_id=dataset_id,
        systems=tuple(sorted({str(row["system"]) for row in runs})),
        runs_checked=len(runs),
        evidence_checked=evidence_checked,
        steps_checked=steps_checked,
        failed_runs=failed_runs,
        counts=counts,
        violations=tuple(found),
        truncated=max(0, total - len(found)),
    )
