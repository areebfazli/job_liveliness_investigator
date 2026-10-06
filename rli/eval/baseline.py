"""A/B baseline metrics over a replay dataset (spec.md §6; PLAN.md M4).

PLAN.md M4's last bullet is "A/B baseline metrics on development/validation
data -> `reports/baseline.md`; keep final holdouts untouched until M6". This
module is the read side that produces that report: given a replay dataset
already built by `rli.replay` (a set of `runs` rows with `mode='replay'`) and
a split assignment from `rli.policy.splits`, it pairs System A and System B's
decisions case-by-case, computes spec.md §6's "Agent efficiency" figures for
each system, and computes the pairwise comparison spec.md §6 asks for —

    "action agreement with A (overall and macro-averaged per action class,
    reported with the action distribution so a default-heavy policy cannot
    pass trivially), medium/high-cost probe count, total cost, latency"

— while refusing, by construction, to ever look at the `test` split before
PLAN.md M6.

--------------------------------------------------------------------------
Contract with the replay layer (fixed; see the caller's docstring)
--------------------------------------------------------------------------

A replay run is a `runs` row with `mode='replay'`, a `replay_at` timestamp,
`system` in `('A','B','C','C2')`, and a `config_hash` ENDING in the literal
suffix `|dataset:<dataset_id>`. A **replay case** is identified by
`(input_url, replay_at)`; System A and System B each produce at most one
*live* run per case for a given dataset (a re-run is a duplicate, handled
below). This module never assumes `rli.replay`'s `replay_cases` /
`replay_datasets` tables exist — it reads only `runs`, `run_steps`, and
`postings`, which is also why the tests in `tests/test_eval_baseline.py`
build synthetic rows directly rather than depending on another in-flight
migration.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **The split guard is a hard interlock, not a default.** `ALLOWED_SPLITS =
  ("dev", "validation")` is what every top-level entry point defaults to,
  but the guard in `_check_allowed_splits` fires on ANY caller-supplied
  `allowed_splits` that contains `"test"` — there is no override flag. This
  is deliberate: PLAN.md M4 says "keep final holdouts untouched until M6",
  and a report function that quietly accepted `allowed_splits=("test",)`
  because a caller made a typo would silently spend the one holdout this
  project has. If M6 genuinely needs the test split, it calls the split
  functions directly rather than through this module's guarded entry
  points.

* **Pairing key is `(input_url, replay_at)`, exactly as the replay contract
  defines a case** — not `posting_id`, because `posting_id` can be NULL (an
  unresolved URL) and two different input URLs can resolve to the same
  posting over time (a repost). Keying on the resolver's output rather than
  its input would silently merge distinct replay cases.

* **Duplicate handling: take the latest by `(started_at, id)`.** A dataset
  should have exactly one run per `(system, input_url, replay_at)`, but a
  re-run (e.g. after a bugfix) can leave more than one. Silently summing or
  averaging duplicates would double-count a case's cost/latency and — worse
  — could pick an arbitrary one of two *different* decisions for the
  agreement metric. Taking the row with the greatest `(started_at, id)`
  (id is monotonically increasing on insert, so it also breaks a
  same-instant tie) is the same "latest wins" rule `rli.eval.report` and
  `rli.eval.runner` use elsewhere for reproducibility, and every duplicate
  collapsed is counted in `CaseSet.duplicates_collapsed` rather than
  disappearing.

* **The `unassigned` bucket is a strict exclusion, not a default split.** A
  run with no `posting_id`, or a `posting_id` absent from the caller's
  split map (e.g. the posting has no `first_observed` so
  `rli.policy.splits.assign_splits` never saw it), is counted in
  `unassigned` and EXCLUDED from every other bucket — never folded into
  `paired`/`a_only`/`b_only` by assuming it is safe. An unassignable
  posting could, for all this module knows, belong to the held-out test
  split; treating "unknown" as "safe to include" is exactly the leak
  PLAN.md M4/M6 exists to prevent.

* **A case whose split is valid but not in `allowed_splits` (e.g. `"test"`
  under the default dev/validation scope) is counted separately, in
  `excluded_holdout`, not lumped into `unassigned`.** The two exclusions
  have different causes (unknown split vs. deliberately out-of-scope split)
  and a reader auditing "did this report touch the holdout" needs to be
  able to see zero in `excluded_holdout` and know that a nonzero
  `unassigned` count reflects a data-quality gap, not a leak.

* **The split gate applies uniformly to paired, A-only, and B-only cases.**
  spec.md §6 only defines agreement over pairs, so an unpaired case cannot
  affect any pairwise metric — but its cost/latency/probe figures still
  feed the per-system blocks (item 4 below), and those blocks must respect
  the same holdout boundary. A single-sided case whose posting turns out to
  be in the `test` split is therefore excluded (`excluded_holdout`) exactly
  like a paired one, rather than being let through because "it's not part
  of the comparison anyway".

* **Per-system metrics are scoped to the case set, not to the whole `runs`
  table.** `rli.eval.report.summarize_runs(conn, "A")` aggregates every A
  run ever recorded — live traffic, other datasets, the test split,
  everything. That is the right tool for "how is System A doing overall",
  and the wrong one for a baseline report scoped to one dataset and two
  splits. This module reimplements the same aggregation shape (reusing
  `rli.eval.report.cost_tier_for` for the tier table, per the instruction
  not to restate it) but scopes every query to the exact run ids that
  survived the case-collection gate.

* **`medium_high_ratio` is computed for B (not "B vs A" as a difference)**
  because spec.md §6's agent gate shape is `C medium/high-cost probe use <=
  70% of B` — a ratio of the *candidate* system to B, not of B to A. This
  module produces that same shape one system early (`B / A`) so that when
  System C exists, `C / B` is a one-line change reusing this exact
  function. It is `None` when A's medium/high count is `0` (division is
  undefined, not infinite — a report that printed `inf` would look like a
  bug).

* **Macro agreement's classes come from A's distribution, because A is the
  reference system** (spec.md §6: "A — Full probes" is the system every
  other system is compared against). A class A never produced contributes
  no term to the macro average; this is intentional, not a gap — macro
  agreement asks "for each action A actually recommends, how often does B
  agree", and a class outside A's output has no such question to ask.
  `per_class_counts` records each class's denominator so the macro figure
  is never read without knowing how many cases backed each term (spec.md
  §6: "reported with the action distribution so a default-heavy policy
  cannot pass trivially" — the same principle applied to the per-class
  table, not just the headline distribution).

* **Cost is unitless placeholder cost POINTS, not dollars**, and latency is
  SUMMED STEP LATENCY, a lower bound on wall-clock time — both exactly as
  `rli.eval.runner` documents `runs.total_cost_usd` /
  `runs.total_latency_ms` to mean. Every place this module prints either
  figure says so again, because a number labeled "cost" or "latency" reads
  as authoritative even when the docstring three files away says otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.report import cost_tier_for
from rli.eval.runner import STEP_PROBE_RUN
from rli.models.time import now_utc, parse_utc, to_utc_z
from rli.policy.action import policy_version
from rli.policy.splits import (
    DEFAULT_SEED,
    CompanySplitMethod,
    Split,
    SplitRow,
    assign_splits,
)
from rli.probes.board_snapshot import BoardSnapshotProbe
from rli.probes.resolve_posting import ResolvePostingProbe

__all__ = [
    "ALLOWED_SPLITS",
    "BaselineReport",
    "CaseSet",
    "Comparison",
    "HoldoutSplitRequestedError",
    "PairedCase",
    "SystemMetrics",
    "baseline_report",
    "collect_cases",
    "load_split_map",
    "write_baseline_report",
]

#: The only splits an M4 baseline report may touch. `"test"` is deliberately
#: absent: PLAN.md M4 says "keep final holdouts untouched until M6", and
#: `_check_allowed_splits` refuses any call that names it. See the module
#: docstring's first judgment call.
ALLOWED_SPLITS: tuple[str, ...] = ("dev", "validation")

_ALWAYS_RUN_PROBES = (ResolvePostingProbe.name, BoardSnapshotProbe.name)

_ACTION_CLASSES = ("apply_now", "quick_apply", "wait", "skip")

_MISSING = "(missing)"


class HoldoutSplitRequestedError(ValueError):
    """Raised when a caller asks this module to touch the `test` split.

    This is the hard interlock PLAN.md M4 requires ("keep final holdouts
    untouched until M6"). It fires the instant `"test"` (or any split not in
    `ALLOWED_SPLITS`'s superset check — see `_check_allowed_splits`) appears
    in a caller-supplied `allowed_splits`, before any query runs, so a typo
    cannot spend the holdout even once.
    """


def _check_allowed_splits(allowed_splits: tuple[str, ...]) -> None:
    if "test" in allowed_splits:
        raise HoldoutSplitRequestedError(
            "refusing to include the 'test' split in a baseline report: "
            "PLAN.md M4 requires final holdouts to stay untouched until M6. "
            f"got allowed_splits={allowed_splits!r}"
        )
    unknown = [s for s in allowed_splits if s not in ("dev", "validation", "test")]
    if unknown:
        raise ValueError(f"unknown split name(s) in allowed_splits: {unknown!r}")


def _render(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _decode_decision(payload: object) -> dict[str, object] | None:
    """Parse a stored `runs.final_decision`, or `None` if there is nothing usable.

    Mirrors `rli.eval.report._decode_decision` (a corrupt or NULL blob
    degrades to "missing" rather than raising) but is reimplemented locally
    since that helper is private to `rli.eval.report`.
    """
    if not isinstance(payload, str) or not payload.strip():
        return None
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


# ---------------------------------------------------------------------------
# 2. Split map
# ---------------------------------------------------------------------------


def load_split_map(
    conn: sqlite3.Connection,
    *,
    cutoff: datetime,
    validation_cutoff: datetime | None = None,
    seed: int = DEFAULT_SEED,
    fractions: tuple[float, float, float] = (0.6, 0.2, 0.2),
    split_kind: Literal["temporal", "company"],
    company_method: CompanySplitMethod = "greedy",
    as_of: datetime | None = None,
) -> dict[str, Split]:
    """Read every posting's `(company_id, first_observed)` and split it.

    Delegates entirely to `rli.policy.splits.assign_splits` — this function
    only supplies the rows and picks which of the two computed splits
    (`temporal_split` or `company_split` / `company_split_stable`, by
    `company_method`) the caller asked for. A posting with a NULL
    `first_observed` cannot be assigned (there is no cutoff to compare
    against) and is simply omitted from the returned mapping; a caller that
    looks it up gets a missing key, which `collect_cases` then correctly
    treats as `unassigned` rather than guessing a split for it.

    `as_of` restricts the rows to postings that already existed then (row
    `created_at` and `first_observed` both at or before it). The greedy
    company split is a function of the whole row set, so this is what
    reproduces an assignment computed at an earlier moment
    (`rli.eval.metrics.split_map_for_dataset` for a dataset built before
    splits were frozen). `postings` rows are never deleted, so the
    reconstruction is exact except where a later history rebuild moved a
    posting's `first_observed` earlier.
    """
    if split_kind not in ("temporal", "company"):
        raise ValueError(f"split_kind must be 'temporal' or 'company', got {split_kind!r}")

    if as_of is None:
        rows = conn.execute(
            """
            SELECT posting_id, company_id, first_observed
            FROM postings
            WHERE first_observed IS NOT NULL
            """
        ).fetchall()
    else:
        stamp = to_utc_z(as_of)
        rows = conn.execute(
            """
            SELECT posting_id, company_id, first_observed
            FROM postings
            WHERE first_observed IS NOT NULL AND first_observed <= ? AND created_at <= ?
            """,
            (stamp, stamp),
        ).fetchall()

    split_rows = [
        SplitRow(
            posting_id=row["posting_id"],
            company_id=row["company_id"],
            first_observed=parse_utc(row["first_observed"]),
        )
        for row in rows
    ]
    assignments = assign_splits(
        split_rows,
        cutoff=cutoff,
        validation_cutoff=validation_cutoff,
        seed=seed,
        fractions=fractions,
        company_method=company_method,
    )
    if split_kind == "temporal":
        return {a.posting_id: a.temporal_split for a in assignments}
    return {a.posting_id: a.company_split for a in assignments}


# ---------------------------------------------------------------------------
# 3. Case collection
# ---------------------------------------------------------------------------


class PairedCase(BaseModel):
    """One replay case where both System A and System B produced a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_url: str
    replay_at: str
    posting_id: str
    split: str
    a_run_id: str
    b_run_id: str
    a_action: str | None
    b_action: str | None
    a_posting_state: str | None
    b_posting_state: str | None
    a_evidence_quality: str | None
    b_evidence_quality: str | None


class CaseSet(BaseModel):
    """The result of pairing System A and System B's replay runs for a dataset.

    `a_run_ids` / `b_run_ids` are every run id (paired or single-sided) that
    passed the split gate for that system — the scope `SystemMetrics` below
    aggregates over. `cases` holds only the PAIRED cases, since those are
    what `Comparison` needs; an unpaired case contributes to a system's
    metrics but has no counterpart to compare against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    allowed_splits: tuple[str, ...]

    paired: int = 0
    a_only: int = 0
    b_only: int = 0
    unassigned: int = 0
    excluded_holdout: int = 0
    duplicates_collapsed: int = 0

    cases: tuple[PairedCase, ...] = ()
    a_run_ids: tuple[str, ...] = ()
    b_run_ids: tuple[str, ...] = ()

    def describe(self) -> str:
        return (
            f"dataset {self.dataset_id!r} (allowed splits: "
            f"{', '.join(self.allowed_splits) or '(none)'}):\n"
            f"  paired={self.paired} a_only={self.a_only} b_only={self.b_only} "
            f"unassigned={self.unassigned} excluded_holdout={self.excluded_holdout} "
            f"duplicates_collapsed={self.duplicates_collapsed}"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _dataset_suffix(dataset_id: str) -> str:
    return f"|dataset:{dataset_id}"


def _latest_by_case(
    rows: list[sqlite3.Row],
) -> tuple[dict[tuple[str, str], sqlite3.Row], int]:
    """Group `rows` by `(input_url, replay_at)`, keep the latest, count duplicates.

    "Latest" is the greatest `(started_at, id)` pair — see the module
    docstring's duplicate-handling judgment call.
    """
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (row["input_url"], row["replay_at"])
        groups.setdefault(key, []).append(row)

    latest: dict[tuple[str, str], sqlite3.Row] = {}
    duplicates = 0
    for key, group in groups.items():
        group.sort(key=lambda r: (r["started_at"], r["id"]))
        latest[key] = group[-1]
        duplicates += len(group) - 1
    return latest, duplicates


def collect_cases(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    splits: dict[str, Split] | dict[str, str],
    allowed_splits: tuple[str, ...] = ALLOWED_SPLITS,
) -> CaseSet:
    """Pair System A and System B's replay runs for `dataset_id`, split-gated.

    See the module docstring for the pairing key, duplicate handling, and
    the `unassigned` / `excluded_holdout` exclusion policy. Raises
    `HoldoutSplitRequestedError` if `allowed_splits` contains `"test"`.
    """
    _check_allowed_splits(allowed_splits)
    suffix = _dataset_suffix(dataset_id)

    rows = conn.execute(
        """
        SELECT id, input_url, replay_at, posting_id, system, status,
               final_decision, started_at, config_hash
        FROM runs
        WHERE mode = 'replay' AND system IN ('A', 'B') AND replay_at IS NOT NULL
              AND config_hash IS NOT NULL
        ORDER BY started_at, id
        """
    ).fetchall()
    rows = [row for row in rows if str(row["config_hash"]).endswith(suffix)]

    a_rows = [row for row in rows if row["system"] == "A"]
    b_rows = [row for row in rows if row["system"] == "B"]

    a_latest, a_dups = _latest_by_case(a_rows)
    b_latest, b_dups = _latest_by_case(b_rows)

    paired = 0
    a_only = 0
    b_only = 0
    unassigned = 0
    excluded_holdout = 0
    cases: list[PairedCase] = []
    a_run_ids: list[str] = []
    b_run_ids: list[str] = []

    all_keys = sorted(set(a_latest) | set(b_latest))
    for key in all_keys:
        a_row = a_latest.get(key)
        b_row = b_latest.get(key)

        posting_id = None
        if a_row is not None and a_row["posting_id"] is not None:
            posting_id = a_row["posting_id"]
        elif b_row is not None and b_row["posting_id"] is not None:
            posting_id = b_row["posting_id"]

        split = None if posting_id is None else splits.get(posting_id)

        if posting_id is None or split is None:
            unassigned += 1
            continue
        if split not in allowed_splits:
            excluded_holdout += 1
            continue

        if a_row is not None:
            a_run_ids.append(a_row["id"])
        if b_row is not None:
            b_run_ids.append(b_row["id"])

        if a_row is not None and b_row is not None:
            paired += 1
            a_decision = _decode_decision(a_row["final_decision"])
            b_decision = _decode_decision(b_row["final_decision"])
            input_url, replay_at = key
            cases.append(
                PairedCase(
                    input_url=input_url,
                    replay_at=replay_at,
                    posting_id=posting_id,
                    split=split,
                    a_run_id=a_row["id"],
                    b_run_id=b_row["id"],
                    a_action=(a_decision or {}).get("recommended_action"),
                    b_action=(b_decision or {}).get("recommended_action"),
                    a_posting_state=(a_decision or {}).get("posting_state"),
                    b_posting_state=(b_decision or {}).get("posting_state"),
                    a_evidence_quality=(a_decision or {}).get("evidence_quality"),
                    b_evidence_quality=(b_decision or {}).get("evidence_quality"),
                )
            )
        elif a_row is not None:
            a_only += 1
        else:
            b_only += 1

    return CaseSet(
        dataset_id=dataset_id,
        allowed_splits=tuple(allowed_splits),
        paired=paired,
        a_only=a_only,
        b_only=b_only,
        unassigned=unassigned,
        excluded_holdout=excluded_holdout,
        duplicates_collapsed=a_dups + b_dups,
        cases=tuple(cases),
        a_run_ids=tuple(a_run_ids),
        b_run_ids=tuple(b_run_ids),
    )


# ---------------------------------------------------------------------------
# 4. Per-system metrics
# ---------------------------------------------------------------------------


class SystemMetrics(BaseModel):
    """One system's spec.md §6 "Agent efficiency" figures, scoped to a case set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    runs: int = 0
    completed: int = 0
    failed: int = 0
    running: int = 0
    stopped: int = 0
    decisions_missing: int = 0

    action_distribution: dict[str, int] = {}
    evidence_quality_distribution: dict[str, int] = {}
    posting_state_distribution: dict[str, int] = {}

    probe_counts: dict[str, int] = {}
    probe_counts_by_tier: dict[str, int] = {}
    medium_high_probe_steps: int = 0
    mean_medium_high_probes_per_run: float = 0.0

    total_cost_usd: float = 0.0
    mean_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    mean_latency_ms: float = 0.0

    failure_counts: dict[str, int] = {}
    failed_steps: int = 0

    def describe(self) -> str:
        lines = [
            f"system {self.system}: runs={self.runs} completed={self.completed} "
            f"failed={self.failed} running={self.running} stopped={self.stopped}",
            f"  actions: {_render(self.action_distribution)}",
            f"  evidence_quality: {_render(self.evidence_quality_distribution)}",
            f"  posting_state: {_render(self.posting_state_distribution)}",
            f"  probe steps: {_render(self.probe_counts)}",
            f"  by cost tier: {_render(self.probe_counts_by_tier)}",
            f"  medium/high steps={self.medium_high_probe_steps} "
            f"(mean {self.mean_medium_high_probes_per_run:.2f}/run) "
            "(spec.md §6 agent-gate numerator)",
            f"  cost: total={self.total_cost_usd:.2f} mean={self.mean_cost_usd:.2f} "
            "cost points (placeholder units, NOT dollars)",
            f"  latency: total={self.total_latency_ms:.0f} mean={self.mean_latency_ms:.0f} ms "
            "(summed step latency, a lower bound on wall clock)",
            f"  failures: {self.failed_steps} step(s) — {_render(self.failure_counts)}",
        ]
        if self.decisions_missing:
            lines.append(
                f"  NOTE: {self.decisions_missing} run(s) have no parsable "
                "final_decision and are excluded from the distributions above"
            )
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _system_metrics(
    conn: sqlite3.Connection, system: str, run_ids: tuple[str, ...]
) -> SystemMetrics:
    """Aggregate `runs` + `run_steps` over exactly `run_ids` (see the module docstring).

    Shaped like `rli.eval.report.summarize_runs` but scoped to a caller-given
    run id set rather than "every row for this system", and reusing
    `cost_tier_for` for the tier table rather than restating it.
    """
    if not run_ids:
        return SystemMetrics(system=system)

    placeholders = ",".join("?" for _ in run_ids)
    run_rows = conn.execute(
        f"""
        SELECT id, status, final_decision, total_cost_usd, total_latency_ms
        FROM runs
        WHERE id IN ({placeholders})
        """,
        run_ids,
    ).fetchall()

    status_counts: dict[str, int] = {}
    actions: dict[str, int] = {}
    qualities: dict[str, int] = {}
    posting_states: dict[str, int] = {}
    decisions_missing = 0
    total_cost = 0.0
    total_latency_ms = 0.0

    for row in run_rows:
        _bump(status_counts, str(row["status"]))
        total_cost += float(row["total_cost_usd"] or 0.0)
        total_latency_ms += float(row["total_latency_ms"] or 0)

        decoded = _decode_decision(row["final_decision"])
        if decoded is None:
            decisions_missing += 1
            continue
        action = decoded.get("recommended_action")
        quality = decoded.get("evidence_quality")
        state = decoded.get("posting_state")
        _bump(actions, str(action) if action is not None else _MISSING)
        _bump(qualities, str(quality) if quality is not None else _MISSING)
        _bump(posting_states, str(state) if state is not None else _MISSING)

    probe_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    failure_counts: dict[str, int] = {}
    failed_steps = 0
    medium_high_steps = 0

    step_rows = conn.execute(
        f"""
        SELECT probe_name, component, decision_type, error
        FROM run_steps
        WHERE run_id IN ({placeholders})
        """,
        run_ids,
    ).fetchall()

    for row in step_rows:
        name = row["probe_name"]
        if row["error"] is not None:
            failed_steps += 1
            _bump(failure_counts, str(name) if name else "(controller)")

        if row["component"] != "probe" or row["decision_type"] != STEP_PROBE_RUN:
            continue

        key = str(name) if name else "(unnamed)"
        _bump(probe_counts, key)
        tier = cost_tier_for(name)
        _bump(tier_counts, tier)
        if tier in ("medium", "high"):
            medium_high_steps += 1

    count = len(run_ids)
    return SystemMetrics(
        system=system,
        runs=count,
        completed=status_counts.get("completed", 0),
        failed=status_counts.get("failed", 0),
        running=status_counts.get("running", 0),
        stopped=status_counts.get("stopped", 0),
        decisions_missing=decisions_missing,
        action_distribution=actions,
        evidence_quality_distribution=qualities,
        posting_state_distribution=posting_states,
        probe_counts=probe_counts,
        probe_counts_by_tier=tier_counts,
        medium_high_probe_steps=medium_high_steps,
        mean_medium_high_probes_per_run=(medium_high_steps / count) if count else 0.0,
        total_cost_usd=total_cost,
        mean_cost_usd=(total_cost / count) if count else 0.0,
        total_latency_ms=total_latency_ms,
        mean_latency_ms=(total_latency_ms / count) if count else 0.0,
        failure_counts=failure_counts,
        failed_steps=failed_steps,
    )


# ---------------------------------------------------------------------------
# Comparison (paired cases only)
# ---------------------------------------------------------------------------


class Comparison(BaseModel):
    """Action agreement between System A and System B, over the paired cases.

    `overall_agreement` and `macro_agreement` are `None` (not `0.0`) when
    `paired_cases == 0` — an undefined ratio must never print as if it were
    a measured zero. See the module docstring for the exact definitions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    paired_cases: int = 0
    overall_agreement: float | None = None
    macro_agreement: float | None = None
    per_class_agreement: dict[str, float] = {}
    per_class_counts: dict[str, int] = {}
    confusion_matrix: dict[str, dict[str, int]] = {}
    medium_high_ratio: float | None = None
    posting_state_agreement: float | None = None
    evidence_quality_agreement: float | None = None

    def describe(self) -> str:
        lines = [f"comparison over {self.paired_cases} paired case(s):"]
        if self.paired_cases == 0:
            lines.append("  (no paired cases; agreement is undefined)")
            return "\n".join(lines)

        overall = self.overall_agreement or 0.0
        macro = self.macro_agreement or 0.0
        lines.append(f"  overall_agreement={overall:.1%}  macro_agreement={macro:.1%}")
        per_class = " ".join(
            f"{action}={value:.1%}(n={self.per_class_counts.get(action, 0)})"
            for action, value in sorted(self.per_class_agreement.items())
        )
        lines.append(f"  per_class_agreement: {per_class or '(none)'}")
        ratio = "n/a (A has 0 medium/high probe steps)"
        if self.medium_high_ratio is not None:
            ratio = f"{self.medium_high_ratio:.2f} (B/A)"
        lines.append(f"  medium_high_ratio: {ratio}")
        if self.posting_state_agreement is not None:
            lines.append(f"  posting_state_agreement={self.posting_state_agreement:.1%}")
        if self.evidence_quality_agreement is not None:
            lines.append(f"  evidence_quality_agreement={self.evidence_quality_agreement:.1%}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _field_agreement(cases: tuple[PairedCase, ...], a_attr: str, b_attr: str) -> float | None:
    if not cases:
        return None
    matches = sum(
        1
        for case in cases
        if getattr(case, a_attr) is not None and getattr(case, a_attr) == getattr(case, b_attr)
    )
    return matches / len(cases)


def _compare(
    cases: tuple[PairedCase, ...], a_metrics: SystemMetrics, b_metrics: SystemMetrics
) -> Comparison:
    if not cases:
        return Comparison(paired_cases=0)

    overall = _field_agreement(cases, "a_action", "b_action")

    a_classes = sorted({c.a_action for c in cases if c.a_action is not None})
    per_class_agreement: dict[str, float] = {}
    per_class_counts: dict[str, int] = {}
    for action in a_classes:
        in_class = [c for c in cases if c.a_action == action]
        matches = sum(1 for c in in_class if c.b_action == action)
        per_class_counts[action] = len(in_class)
        per_class_agreement[action] = matches / len(in_class)
    macro = (
        sum(per_class_agreement.values()) / len(per_class_agreement)
        if per_class_agreement
        else None
    )

    confusion: dict[str, dict[str, int]] = {}
    for case in cases:
        a_key = case.a_action if case.a_action is not None else _MISSING
        b_key = case.b_action if case.b_action is not None else _MISSING
        confusion.setdefault(a_key, {})
        confusion[a_key][b_key] = confusion[a_key].get(b_key, 0) + 1

    medium_high_ratio = None
    if a_metrics.medium_high_probe_steps > 0:
        medium_high_ratio = b_metrics.medium_high_probe_steps / a_metrics.medium_high_probe_steps

    return Comparison(
        paired_cases=len(cases),
        overall_agreement=overall,
        macro_agreement=macro,
        per_class_agreement=per_class_agreement,
        per_class_counts=per_class_counts,
        confusion_matrix=confusion,
        medium_high_ratio=medium_high_ratio,
        posting_state_agreement=_field_agreement(cases, "a_posting_state", "b_posting_state"),
        evidence_quality_agreement=_field_agreement(
            cases, "a_evidence_quality", "b_evidence_quality"
        ),
    )


# ---------------------------------------------------------------------------
# 5. Top-level report
# ---------------------------------------------------------------------------


class BaselineReport(BaseModel):
    """The complete A/B baseline report for one replay dataset (PLAN.md M4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    allowed_splits: tuple[str, ...]
    generated_at: str
    policy_version: str
    case_set: CaseSet
    systems: dict[str, SystemMetrics] = {}
    comparison: Comparison

    def describe(self) -> str:
        lines = [
            f"baseline report: dataset={self.dataset_id!r} "
            f"policy_version={self.policy_version} generated_at={self.generated_at}",
            self.case_set.describe(),
        ]
        for name in sorted(self.systems):
            lines.append(self.systems[name].describe())
        lines.append(self.comparison.describe())
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def baseline_report(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    splits: dict[str, Split] | dict[str, str],
    allowed_splits: tuple[str, ...] = ALLOWED_SPLITS,
    systems: tuple[str, ...] = ("A", "B"),
) -> BaselineReport:
    """Build the A/B baseline report for `dataset_id` (PLAN.md M4).

    `systems` selects which per-system `SystemMetrics` blocks appear in
    `BaselineReport.systems` (both are always computed internally, since the
    A/B comparison needs both); it does not change which systems are
    paired — `collect_cases` always pairs A and B, matching spec.md §6's
    "All systems use the same frozen action policy" A vs. B baseline this
    milestone asks for. Raises `HoldoutSplitRequestedError` if
    `allowed_splits` contains `"test"` (see `ALLOWED_SPLITS`).
    """
    _check_allowed_splits(allowed_splits)

    case_set = collect_cases(
        conn, dataset_id=dataset_id, splits=splits, allowed_splits=allowed_splits
    )
    a_metrics = _system_metrics(conn, "A", case_set.a_run_ids)
    b_metrics = _system_metrics(conn, "B", case_set.b_run_ids)
    comparison = _compare(case_set.cases, a_metrics, b_metrics)

    all_metrics = {"A": a_metrics, "B": b_metrics}
    selected = {name: all_metrics[name] for name in systems if name in all_metrics}

    return BaselineReport(
        dataset_id=dataset_id,
        allowed_splits=tuple(allowed_splits),
        generated_at=to_utc_z(now_utc()),
        policy_version=policy_version(cfg),
        case_set=case_set,
        systems=selected,
        comparison=comparison,
    )


# ---------------------------------------------------------------------------
# 6. Markdown report writer
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def write_baseline_report(path: str | Path, report: BaselineReport) -> Path:
    """Write `report` as Markdown to `path`, creating parent directories as needed.

    Mirrors `rli.policy.splits.write_splits_csv`'s "create parent
    directories, always produce a valid file" convention. See the module
    docstring for the required LIMITATIONS content.
    """
    destination = Path(path)
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)

    a = report.systems.get("A")
    b = report.systems.get("B")
    comparison = report.comparison
    cs = report.case_set

    all_actions = sorted(
        set((a.action_distribution if a else {}).keys())
        | set((b.action_distribution if b else {}).keys())
    )
    action_table = _markdown_table(
        ["action", "A", "B"],
        [
            [
                action,
                str((a.action_distribution if a else {}).get(action, 0)),
                str((b.action_distribution if b else {}).get(action, 0)),
            ]
            for action in all_actions
        ],
    )

    per_class_rows = [
        [
            action,
            str(comparison.per_class_counts.get(action, 0)),
            _pct(comparison.per_class_agreement.get(action)),
        ]
        for action in sorted(comparison.per_class_agreement)
    ]
    per_class_table = _markdown_table(["A action", "n (A count)", "B agreement"], per_class_rows)

    confusion_actions = sorted(
        set(comparison.confusion_matrix.keys())
        | {b_action for row in comparison.confusion_matrix.values() for b_action in row}
    )
    confusion_rows = [
        [
            a_action,
            *[
                str(comparison.confusion_matrix.get(a_action, {}).get(b_action, 0))
                for b_action in confusion_actions
            ],
        ]
        for a_action in confusion_actions
    ]
    confusion_table = _markdown_table(
        ["A \\ B"] + confusion_actions, confusion_rows if confusion_rows else [["(none)"]]
    )

    def probe_cost_table(metrics: SystemMetrics | None) -> str:
        if metrics is None:
            return "(system not included in this report)"
        return _markdown_table(
            ["metric", "value"],
            [
                ["runs", str(metrics.runs)],
                [
                    "completed / failed / running / stopped",
                    f"{metrics.completed} / {metrics.failed} / "
                    f"{metrics.running} / {metrics.stopped}",
                ],
                ["probe steps (by name)", _render(metrics.probe_counts)],
                ["probe steps (by cost tier)", _render(metrics.probe_counts_by_tier)],
                [
                    "medium/high probe steps",
                    f"{metrics.medium_high_probe_steps} total, "
                    f"{metrics.mean_medium_high_probes_per_run:.2f} mean/run",
                ],
                [
                    "cost points (placeholder units, NOT dollars)",
                    f"{metrics.total_cost_usd:.2f} total, {metrics.mean_cost_usd:.2f} mean/run",
                ],
                [
                    "latency (summed step latency, ms; a lower bound on wall clock)",
                    f"{metrics.total_latency_ms:.0f} total, {metrics.mean_latency_ms:.0f} mean/run",
                ],
                ["failed steps", f"{metrics.failed_steps} — {_render(metrics.failure_counts)}"],
            ],
        )

    lines = [
        "# A/B baseline report",
        "",
        f"Dataset: `{report.dataset_id}` · Policy version: `{report.policy_version}` · "
        f"Generated: `{report.generated_at}`",
        "",
        "## Case-set accounting",
        "",
        f"- Paired (A and B both ran): **{cs.paired}**",
        f"- A only: **{cs.a_only}**",
        f"- B only: **{cs.b_only}**",
        f"- Unassigned (no posting, or posting absent from the split map): **{cs.unassigned}**",
        f"- Excluded holdout (split not in {list(cs.allowed_splits)}): **{cs.excluded_holdout}**",
        f"- Duplicate re-runs collapsed to the latest: **{cs.duplicates_collapsed}**",
        "",
        "## Action distribution (both systems)",
        "",
        action_table,
        "",
        "## Agreement",
        "",
        f"- Overall agreement: **{_pct(comparison.overall_agreement)}** "
        f"(over {comparison.paired_cases} paired case(s))",
        f"- Macro-averaged agreement (per action class): **{_pct(comparison.macro_agreement)}**",
        f"- posting_state agreement: {_pct(comparison.posting_state_agreement)}",
        f"- evidence_quality agreement: {_pct(comparison.evidence_quality_agreement)}",
        "- medium/high probe ratio (B/A): "
        + (
            "n/a (A has 0 medium/high probe steps)"
            if comparison.medium_high_ratio is None
            else f"{comparison.medium_high_ratio:.2f}"
        ),
        "",
        "### Per-class agreement (classes defined by System A's output)",
        "",
        per_class_table,
        "",
        "### Confusion matrix (A action -> B action -> count)",
        "",
        confusion_table,
        "",
        "## System A — probes, cost, latency, failures",
        "",
        probe_cost_table(a),
        "",
        "## System B — probes, cost, latency, failures",
        "",
        probe_cost_table(b),
        "",
        "## Limitations and interpretation",
        "",
        "- **Cost is unitless placeholder cost POINTS, not dollars.** "
        "`total_cost_usd` on `runs` is a configured per-probe placeholder cost "
        "(see `rli.config.ProbeCosts`), not a real dollar figure; treat the "
        "numbers above as relative, not absolute, spend.",
        "- **Latency is summed step latency, a lower bound on wall-clock time.** "
        "`runs.total_latency_ms` sums each traced step's measured latency; it "
        "does not include controller/scheduling overhead between steps, so "
        "true wall-clock latency is at least this large, never smaller.",
        f"- **Splits included: {', '.join(cs.allowed_splits) or '(none)'}.** The final "
        "`test` holdout was deliberately excluded from this report "
        '(PLAN.md M4: "keep final holdouts untouched until M6"); '
        f"{cs.excluded_holdout} case(s) were dropped for being in an "
        "out-of-scope split, and this report never queried their decisions.",
        "- **Do not read agreement without the action distribution next to it.** "
        "spec.md §6 warns that a default-heavy policy can post high overall "
        "agreement trivially by recommending the same action almost always; "
        "the action distribution and the per-class agreement table above are "
        "what make that visible, and the overall figure should never be quoted "
        "without them.",
        "",
    ]

    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination
