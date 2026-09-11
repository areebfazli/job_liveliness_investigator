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

* **A DATABASE failure around one case fails that case too, and the class
  that says so is `sqlite3.Error` — not `Exception`.** `_replay_one` already
  turns anything the system under test raises into a failed `CaseOutcome`,
  but the walk does database work of its own OUTSIDE that handler: the
  `resume` path reads prior runs and deletes the stale ones, and the
  `stop_on_quota` path deletes the quota-cut run. Those calls are writes, and
  a write on a connection that is holding a WAL read snapshot fails with
  `SQLITE_BUSY_SNAPSHOT` as soon as anything else has committed to `main`
  (`rli.replay.pit` explains the mechanism it now prevents). Uncaught, one
  such failure ended the WHOLE replay — which for System C means a multi-day,
  quota-limited walk, with `resume=True` on by default, aborting on a case it
  had not even run yet. So the per-case body catches `sqlite3.Error`,
  `ROLLBACK`s (the wedged snapshot is released, and a `_delete_run_ids` that
  failed between its `evidence` and `runs` deletes is undone rather than left
  half applied), records the failure as that case's outcome — counted in
  `errors`, visible in `describe()` — and moves to the next case. It
  deliberately does NOT catch `Exception`: a `LookupError`, `ValueError`,
  `TypeError` or `KeyError` in this module is a bug in the walk itself, not a
  case-level fact, and the CLI exits nonzero on `violations` but not on
  `errors`, so burying a programming error in the error count would make it
  silent. `PointInTimeError` and failures INSTALLING a `T`'s corpus are
  outside the per-case handler for the same reason: a corpus that cannot be
  built is not one case's problem, and continuing would replay the remaining
  cases of that `T` against the wrong view.

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

* **`resume=True` and `stop_on_quota=True` are the one exception to
  "replace by default", and they exist for one reason: System C is replayed
  against Google Gemini's free tier, which allows 15 requests/minute and
  500/day per model. A full pass over either dataset needs several times
  that many calls, so it necessarily spans multiple days and multiple
  process restarts. Deleting yesterday's progress on every invocation (the
  normal `replace` behavior) would make no forward progress at all — so
  `resume` instead skips cases with a prior completed, non-quota-affected
  run, and `stop_on_quota` detects the run whose `run_steps` shows daily
  quota exhaustion, deletes just that one run (so it looks "never attempted"
  to the next `resume=True` call), and returns immediately rather than
  burning the rest of the grid against an exhausted quota.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.runner import ReplayHook, RunResult, SystemName
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.models.time import now_utc, parse_utc, to_utc_z
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
    "ReplayDatasetStatus",
    "ReplayRunSummary",
    "SystemCaseStatus",
    "SystemRunner",
    "clear_replay_runs",
    "dataset_status",
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
    #: Cases skipped this call because a prior non-quota-affected completed
    #: run already existed (`resume=True` only; see the module docstring).
    skipped: int = 0
    #: `"quota_exhausted"` when `stop_on_quota` cut this call short; `None`
    #: for a normal (possibly partial, via `limit_cases`) completion.
    stopped_reason: str | None = None
    #: The `run_steps.error` text that triggered a quota stop.
    quota_detail: str | None = None
    #: `to_utc_z` of the next moment a daily quota is expected to have reset,
    #: set only alongside `stopped_reason`.
    resume_after: str | None = None

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
            f"violations={self.violations} errors={self.errors} skipped={self.skipped} "
            f"(replaced {self.replaced_runs} previous run(s))",
            f"  actions: {_render(self.action_distribution)}",
            f"  posting_state: {_render(self.posting_state_distribution)}",
            f"  evidence_quality: {_render(self.evidence_quality_distribution)}",
            f"  dynamic probes run: {_render(self.probe_counts)}",
            f"  cases with an observable archive board state: "
            f"{self.cases_with_archive_state}/{self.cases}",
        ]
        if self.stopped_reason is not None:
            lines.append(
                f"  STOPPED: {self.stopped_reason} — {self.quota_detail} "
                f"(resume after {self.resume_after})"
            )
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


def _sqlite_error_text(exc: sqlite3.Error) -> str:
    """A `sqlite3` failure, with the SQLite error NAME kept.

    `SQLITE_BUSY` and `SQLITE_BUSY_SNAPSHOT` both stringify as "database is
    locked" and mean opposite things: the first is contention, which
    `rli.db.connect`'s 5 s `busy_timeout` retries, and the second is a
    read-snapshot conflict, which fails in ~0 ms and is never retried. A
    recorded per-case error that says only "database is locked" sends the
    reader after the wrong problem, so the name goes in the text.
    """
    name = getattr(exc, "sqlite_errorname", None)
    text = f"{type(exc).__name__}: {exc}"
    return f"{text} [{name}]" if name else text


def _failed_case(row: sqlite3.Row, error: str, outcome: CaseOutcome | None) -> CaseOutcome:
    """This case's outcome when the database work around it failed.

    `outcome` is whatever `_replay_one` had already returned, if it got that
    far: a case whose run completed and whose post-run quota bookkeeping then
    failed is recorded as what it actually produced, with the error stamped
    on it, rather than as a case that never ran. `error` makes it count in
    `ReplayRunSummary.errors` either way.
    """
    if outcome is not None:
        return outcome.model_copy(update={"error": error})
    return CaseOutcome(
        posting_id=row["posting_id"],
        replay_at=row["replay_at"],
        input_url=row["canonical_url"],
        error=error,
    )


def _delete_run_ids(conn: sqlite3.Connection, run_ids: Sequence[str]) -> int:
    """Delete `evidence`, `run_steps` and `runs` rows for exactly these run ids."""
    if not run_ids:
        return 0
    placeholders = ",".join("?" for _ in run_ids)
    conn.execute(f"DELETE FROM evidence WHERE run_id IN ({placeholders})", run_ids)
    conn.execute(f"DELETE FROM run_steps WHERE run_id IN ({placeholders})", run_ids)
    conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", run_ids)
    conn.commit()
    return len(run_ids)


def _dataset_run_ids(conn: sqlite3.Connection, *, dataset_id: str, system: str) -> list[str]:
    """This dataset's replay run ids for `system` (the `clear_replay_runs` filter)."""
    suffix = f"|dataset:{dataset_id}"
    return [
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


def clear_replay_runs(conn: sqlite3.Connection, *, dataset_id: str, system: str) -> int:
    """Delete this dataset's previous replay runs for `system`. Returns the count.

    Removes `evidence` and `run_steps` first (both reference `runs`), then the
    `runs` rows themselves. Only rows that are `mode='replay'`, this system,
    and whose `config_hash` ends in this dataset's `|dataset:<id>` suffix —
    the same identity `rli.eval.baseline.collect_cases` uses, so "what this
    deletes" and "what the report would have counted" are the same set by
    construction. Live runs, other systems and other datasets are untouched.
    """
    return _delete_run_ids(conn, _dataset_run_ids(conn, dataset_id=dataset_id, system=system))


def _case_run_rows(
    conn: sqlite3.Connection, *, dataset_id: str, system: str, input_url: str, replay_at: str
) -> list[sqlite3.Row]:
    """Prior replay runs for exactly this `(dataset, system, input_url, replay_at)` case.

    Same `(input_url, replay_at)` case identity `rli.eval.evaluate` pairs
    cases by (module docstring) — not the dataset's own `posting_id`, which a
    replayed system can legitimately re-derive differently.
    """
    suffix = f"|dataset:{dataset_id}"
    return [
        row
        for row in conn.execute(
            """
            SELECT id, status, config_hash FROM runs
            WHERE mode = 'replay' AND system = ? AND config_hash IS NOT NULL
              AND input_url = ? AND replay_at = ?
            """,
            (system, input_url, replay_at),
        ).fetchall()
        if str(row["config_hash"]).endswith(suffix)
    ]


#: Substrings that mark a `run_steps.error` as daily (not per-minute) LLM
#: quota exhaustion. The first two are Gemini's own quota-metric wording
#: (`rli.llm.client._extract_quota_lines`); "429"+"llmtransporterror" is the
#: fallback for a quota message this list doesn't otherwise recognize.
_QUOTA_TEXT_SIGNATURES = ("requestsperday", "perdayperprojectpermodel", "per day")


def _looks_like_quota_exhaustion(text: str) -> bool:
    lowered = text.lower()
    if any(sig in lowered for sig in _QUOTA_TEXT_SIGNATURES):
        return True
    return "429" in lowered and "llmtransporterror" in lowered


def _run_quota_detail(conn: sqlite3.Connection, run_id: str) -> str | None:
    """First `run_steps.error` on this run that looks like daily-quota exhaustion, else `None`.

    Scans every non-null error on the run regardless of which step produced
    it (`rli.agent.loop`'s investigator call and `rli.agent.explanation`'s
    call both persist the same `LLMTransportError` text shape on failure —
    see the module docstring), so this needs no special case for which
    `decision_type` carried the error.
    """
    rows = conn.execute(
        "SELECT error FROM run_steps WHERE run_id = ? AND error IS NOT NULL ORDER BY step_index",
        (run_id,),
    ).fetchall()
    for row in rows:
        text = row["error"]
        if text and _looks_like_quota_exhaustion(text):
            return text
    return None


_QUOTA_RESET_HOUR_UTC = 8  # 08:00 UTC covers Pacific midnight both in and out of DST
# (07:00 UTC during PDT, 08:00 UTC during PST); picking the
# later of the two is the safe choice — it never returns a
# time before the quota has actually reset.


def _next_quota_reset(after: datetime | None = None) -> datetime:
    moment = after if after is not None else now_utc()
    candidate = moment.replace(hour=_QUOTA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate


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
    resume: bool = False,
    stop_on_quota: bool = False,
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
        collection_status_csv: pinned company-event collection state, for the
            `company_events` probe (`rli.probes.company_events`). Pass the
            same value the build used, or a replay reads whatever the working
            checkout holds today — and would then answer "had we searched
            this company by T?" from a different corpus than the one the
            dataset record was written from.
        replace: delete this dataset's previous replay runs for `system`
            first (see the module docstring). Ignored (treated as `False` for
            the bulk pass) when `resume=True`: resuming needs last call's
            completed runs kept, and per-case cleanup below handles the
            cases that actually get rerun.
        resume: skip any case that already has a completed, non-quota-
            affected replay run for this `(dataset, system)`; rerun (after
            deleting the stale row) any case whose prior run failed or was
            itself cut short by quota exhaustion. See the module docstring's
            "resume/stop_on_quota" judgment call — this is what makes a
            multi-day replay against a free-tier LLM API make forward
            progress instead of restarting from zero every day.
        stop_on_quota: stop the walk (returning immediately, `cases`
            reflecting only what was visited so far) the moment a case's run
            shows `run_steps` evidence of daily LLM-quota exhaustion. That
            case's run is deleted first, so a later `resume=True` call redoes
            exactly it rather than skipping it as "completed".

    Raises `LookupError` if the dataset has no cases, and `ValueError` for a
    system with no runner — both before any case runs. Once the walk starts,
    a per-case failure (the system raising, a `ReplayViolation`, or a
    `sqlite3.Error` in the resume/quota bookkeeping around it) is recorded in
    `outcomes` and counted in `errors`; it never ends the walk. Anything else
    escaping this loop is a bug in this module and is left to propagate (see
    the module docstring's judgment call).
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

    replaced = (
        clear_replay_runs(conn, dataset_id=dataset_id, system=system_name)
        if replace and not resume
        else 0
    )

    outcomes: list[CaseOutcome] = []
    actions: dict[str, int] = {}
    states: dict[str, int] = {}
    qualities: dict[str, int] = {}
    probe_counts: dict[str, int] = {}
    completed = 0
    violations = 0
    errors = 0
    skipped = 0
    archive_cases = 0
    seen = 0
    # The `run_steps.error` text of the case that exhausted the daily LLM
    # quota, set only under `stop_on_quota`. It doubles as the walk's stop
    # flag: the case loop breaks on it, then the `T` loop does, so the walk
    # leaves the `point_in_time` block the ordinary way and there is exactly
    # one place a `ReplayRunSummary` is built.
    quota_stop: str | None = None

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

                outcome: CaseOutcome | None = None
                try:
                    if resume:
                        prior = _case_run_rows(
                            conn,
                            dataset_id=dataset_id,
                            system=system_name,
                            input_url=row["canonical_url"],
                            replay_at=row["replay_at"],
                        )
                        done = [r for r in prior if r["status"] == "completed"]
                        if done and not any(
                            _run_quota_detail(conn, r["id"]) is not None for r in done
                        ):
                            skipped += 1
                            continue
                        if prior:
                            _delete_run_ids(conn, [r["id"] for r in prior])

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

                    if stop_on_quota and outcome.run_id is not None:
                        quota_text = _run_quota_detail(conn, outcome.run_id)
                        if quota_text is not None:
                            _delete_run_ids(conn, [outcome.run_id])
                            outcomes.append(outcome)
                            quota_stop = quota_text
                            break
                except sqlite3.Error as exc:
                    # Everything the SYSTEM can raise is already a case
                    # outcome by the time it gets here (`_replay_one`). What
                    # is left is this module's own database work AROUND that
                    # call — the resume path's `_case_run_rows` /
                    # `_delete_run_ids`, the quota check, and the archive
                    # claim lookup `_replay_one` makes before its own
                    # handler — and a failure in it is this case's failure,
                    # not the walk's. See the module docstring's judgment
                    # call for why the class is `sqlite3.Error` and not
                    # `Exception`.
                    conn.rollback()
                    outcomes.append(_failed_case(row, _sqlite_error_text(exc), outcome))
                    errors += 1
                    continue

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

        if quota_stop is not None:
            break

    return ReplayRunSummary(
        dataset_id=dataset_id,
        system=system_name,
        cases=seen,
        completed=completed,
        violations=violations,
        errors=errors,
        replaced_runs=replaced,
        skipped=skipped,
        action_distribution=actions,
        posting_state_distribution=states,
        evidence_quality_distribution=qualities,
        probe_counts=probe_counts,
        cases_with_archive_state=archive_cases,
        outcomes=tuple(outcomes),
        stopped_reason=None if quota_stop is None else "quota_exhausted",
        quota_detail=quota_stop,
        resume_after=None if quota_stop is None else to_utc_z(_next_quota_reset()),
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
    # The pinned path goes into the HOOK, because that is what builds the
    # replayed run's `ProbeContext` (`ReplayHook.open_runner`'s signature is
    # fixed at `(conn, cfg, run, now)`, so a system cannot forward its own
    # copy into it — see `rli.replay.mode.replay_hook`). It is ALSO passed to
    # the system below: `SystemRunner` declares that keyword, a system that
    # cannot know whether it is replaying passes it on unconditionally, and
    # `rli.eval.runner.open_system_runner` ignores it in the replay branch.
    # Both are the same value here, which is the only sane way to hold it.
    hook = replay_hook(
        replay=ReplayContext(T=replay_at, dataset_id=dataset_id),
        store=store,
        posting_id=posting_id,
        archive_claims=archive_claims,
        collection_status_csv=collection_status_csv,
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


# ---------------------------------------------------------------------------
# Dataset status (for `rli replay status` / a multi-day System C replay)
# ---------------------------------------------------------------------------


class SystemCaseStatus(BaseModel):
    """One system's completion count against one dataset's case grid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    total: int
    completed: int
    remaining: int


class ReplayDatasetStatus(BaseModel):
    """Per-system case totals for a replay dataset (spec.md §6, PLAN.md M5).

    "Completed" here means exactly what `run_replay(resume=True)` would skip:
    a completed run with no quota-exhaustion signature in its `run_steps`. A
    quota-cut run counts as still remaining, since `resume=True` reruns it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    total_cases: int
    by_system: tuple[SystemCaseStatus, ...]

    def describe(self) -> str:
        lines = [f"replay dataset {self.dataset_id!r}: {self.total_cases} case(s)"]
        for s in self.by_system:
            lines.append(
                f"  {s.system}: completed={s.completed} remaining={s.remaining} total={s.total}"
            )
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


#: systems `runs.system`'s CHECK constraint allows; status is reported for all four
#: unconditionally so a fresh, never-replayed dataset still shows "remaining = total".
_ALL_SYSTEMS = ("A", "B", "C", "C2")


def dataset_status(conn: sqlite3.Connection, *, dataset_id: str) -> ReplayDatasetStatus:
    """Per-system `(completed, remaining)` counts over `dataset_id`'s case grid.

    O(cases × systems) with one `_case_run_rows` query per case per system —
    fine for this project's dataset sizes (hundreds to ~1300 cases). Raises
    `LookupError` for an unbuilt dataset, matching `run_replay`'s message
    shape, so a caller can rely on the same exception either way.
    """
    rows = dataset_case_rows(conn, dataset_id)
    if not rows:
        raise LookupError(
            f"replay dataset {dataset_id!r} has no cases; build it first (`rli replay build`)"
        )

    total = len(rows)
    by_system = []
    for system in _ALL_SYSTEMS:
        completed = 0
        for row in rows:
            prior = _case_run_rows(
                conn,
                dataset_id=dataset_id,
                system=system,
                input_url=row["canonical_url"],
                replay_at=row["replay_at"],
            )
            done = [r for r in prior if r["status"] == "completed"]
            if done and not any(_run_quota_detail(conn, r["id"]) is not None for r in done):
                completed += 1
        by_system.append(
            SystemCaseStatus(
                system=system, total=total, completed=completed, remaining=total - completed
            )
        )
    return ReplayDatasetStatus(dataset_id=dataset_id, total_cases=total, by_system=tuple(by_system))
