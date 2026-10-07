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
`'A'`, `'B'` and `'R'` to `rli.eval.system_a` / `rli.eval.system_b` /
`rli.eval.system_r` (R makes no model call, so it is offline like A and B),
and any callable
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

* **`shard=(I, N)` splits one dataset across N concurrent processes.** A
  case belongs to shard `I` iff `shard_of(posting_id, replay_at, N) == I` — a
  sha256 of the case key, so the partition is the same in every process, on
  every machine and in every Python (the builtin `hash()` is salted per
  process and would not be). A sharded walk is always a RESUMING walk
  (`resume=False` is refused, `replace` is ignored): N workers that each
  started by clearing the dataset would delete each other's progress. Every
  deletion the walk still makes — a stale failed run, a quota-cut run, a
  half-written run after a lock failure — is per case, and is checked
  against this shard's own case keys before it is issued
  (`ShardViolation`), so no code path can delete a sibling shard's runs.

* **A case that dies on a LOCK is retried once, and its partial run is
  deleted first.** With several processes committing to one WAL database a
  write can wait out `busy_timeout` (`REPLAY_BUSY_TIMEOUT_S` for the CLI)
  and fail with "database is locked". That is contention, not a property of
  the case, so the walk rolls back, deletes whatever run the failed attempt
  created (it may even be `status='completed'` without its explanation,
  which `resume` would otherwise skip forever), waits briefly and tries
  again. A second failure is recorded as the case's error — with no run
  left behind, so the next `resume` redoes it. Every other failure keeps
  its run, exactly as before (a `ReplayViolation` trace is audit evidence).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.runner import ReplayHook, RunResult, SystemName
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.eval.system_r import run_system_r
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
from rli.replay.retire import ensure_not_retired

__all__ = [
    "REPLAY_BUSY_TIMEOUT_S",
    "SYSTEM_RUNNERS",
    "CaseOutcome",
    "ReplayDatasetStatus",
    "ReplayRunSummary",
    "ShardCaseStatus",
    "ShardViolation",
    "SystemCaseStatus",
    "SystemRunner",
    "clear_replay_runs",
    "dataset_status",
    "format_shard",
    "parse_shard",
    "run_replay",
    "shard_of",
]

#: `busy_timeout` (seconds) the `rli replay run` CLI opens its connection
#: with. Long on purpose: a sharded replay has up to N sibling processes
#: committing short transactions to the same WAL database, and a write that
#: gives up after `rli.db.BUSY_TIMEOUT_MS` (5 s) costs a whole case.
REPLAY_BUSY_TIMEOUT_S = 60.0

#: Attempts per case when the failure is a database LOCK (module docstring).
_CASE_ATTEMPTS = 2

#: Pause before the retry of a lock-failed case. Module-level so a test can
#: set it to zero.
_LOCK_RETRY_DELAY_S = 2.0


class ShardViolation(RuntimeError):
    """A sharded walk tried to delete a run that belongs to another shard.

    Never a case-level failure: it means this module computed a deletion set
    wrongly, so it propagates and ends the walk rather than being counted.
    """


def shard_of(posting_id: str, replay_at: str, shards: int) -> int:
    """The shard (`0 <= I < shards`) that owns case `(posting_id, replay_at)`.

    sha256, not `hash()`: Python salts `str` hashes per process, and N worker
    processes must agree on the partition without talking to each other.
    """
    if shards < 1:
        raise ValueError(f"shard count must be >= 1, got {shards}")
    digest = hashlib.sha256(f"{posting_id}\n{replay_at}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % shards


_SHARD_SPEC = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def _checked_shard(shard: tuple[int, int]) -> tuple[int, int]:
    index, count = (int(shard[0]), int(shard[1]))
    if count < 1 or not 0 <= index < count:
        raise ValueError(
            f"invalid shard {index}/{count}: expected I/N with N >= 1 and 0 <= I < N (0-based I)"
        )
    return index, count


def parse_shard(text: str) -> tuple[int, int]:
    """Parse `"I/N"` (0-based `I`) into `(I, N)`; `ValueError` if malformed."""
    match = _SHARD_SPEC.match(text)
    if match is None:
        raise ValueError(f"invalid shard {text!r}: expected I/N, e.g. 0/4 (0-based I)")
    return _checked_shard((int(match.group(1)), int(match.group(2))))


def format_shard(shard: tuple[int, int]) -> str:
    return f"{shard[0]}/{shard[1]}"


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
    # No LLM: offline like A and B (rli.eval.system_r).
    "R": run_system_r,
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
    #: `"I/N"` when this call walked one shard of the dataset, else `None`.
    shard: str | None = None
    #: Cases retried after a database-lock failure (module docstring).
    lock_retries: int = 0
    #: `"<posting_id> @ <T>: <error>"` for each of those retries, so a log
    #: shows what the contention was even when the retry succeeded.
    lock_retry_errors: tuple[str, ...] = ()

    action_distribution: dict[str, int] = {}
    posting_state_distribution: dict[str, int] = {}
    evidence_quality_distribution: dict[str, int] = {}
    probe_counts: dict[str, int] = {}
    cases_with_archive_state: int = 0

    outcomes: tuple[CaseOutcome, ...] = ()

    def describe(self) -> str:
        shard = f" [shard {self.shard}]" if self.shard is not None else ""
        retries = f" lock_retries={self.lock_retries}" if self.lock_retries else ""
        lines = [
            f"replay {self.system} over dataset {self.dataset_id!r}{shard}: "
            f"cases={self.cases} completed={self.completed} "
            f"violations={self.violations} errors={self.errors} skipped={self.skipped}"
            f"{retries} (replaced {self.replaced_runs} previous run(s))",
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
        for retried in self.lock_retry_errors:
            lines.append(f"    ~ retried {retried}")
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
    locked" and mean opposite things: the first is contention, which the
    connection's `busy_timeout` (`rli.db.connect`) retries, and the second is
    a read-snapshot conflict, which fails in ~0 ms and is never retried by
    SQLite. (The walk retries both once, after a `ROLLBACK` that releases any
    snapshot — see `_CaseAttempt`.) A recorded per-case error that says only
    "database is locked" sends the reader after the wrong problem, so the
    name goes in the text.
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


def _is_lock_error(exc: sqlite3.Error) -> bool:
    """SQLITE_BUSY / SQLITE_LOCKED (and their extended codes), i.e. contention."""
    name = getattr(exc, "sqlite_errorname", None) or ""
    if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return True
    return _is_lock_error_text(str(exc))


def _is_lock_error_text(text: str) -> bool:
    lowered = text.lower()
    return "database is locked" in lowered or "database table is locked" in lowered


def _assert_deletable(
    conn: sqlite3.Connection, run_ids: Sequence[str], allowed: frozenset[tuple[str, str]] | None
) -> None:
    """Refuse (`ShardViolation`) to delete any run outside this shard's case keys.

    `allowed` is `None` for an unsharded walk, which may delete anything its
    own filters select. For a shard it is the set of `(input_url, replay_at)`
    keys of the cases this shard owns — the same identity `_case_run_rows`
    selects by — so a deletion set computed wrongly anywhere in this module
    stops the walk instead of erasing a sibling worker's progress.
    """
    if allowed is None or not run_ids:
        return
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT id, input_url, replay_at FROM runs WHERE id IN ({placeholders})", list(run_ids)
    ).fetchall()
    foreign = [row["id"] for row in rows if (row["input_url"], row["replay_at"]) not in allowed]
    if foreign:
        raise ShardViolation(
            f"refusing to delete {len(foreign)} run(s) that belong to another shard's cases: "
            f"{foreign[:5]}"
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
    shard: tuple[int, int] | None = None,
    allow_retired: bool = False,
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
            do not report metrics from one. With `shard`, the prefix is of
            this shard's cases.
        collection_status_csv: pinned company-event collection state, for the
            `company_events` probe (`rli.probes.company_events`). Pass the
            same value the build used, or a replay reads whatever the working
            checkout holds today — and would then answer "had we searched
            this company by T?" from a different corpus than the one the
            dataset record was written from.
        replace: delete this dataset's previous replay runs for `system`
            first (see the module docstring). Ignored (treated as `False` for
            the bulk pass) when `resume=True` or `shard` is given: resuming
            needs last call's completed runs kept, and per-case cleanup below
            handles the cases that actually get rerun.
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
        shard: `(I, N)` — walk only the cases `shard_of` assigns to shard `I`
            of `N` (0-based), so N processes can replay one dataset
            concurrently against one database. Requires `resume=True`
            (`ValueError` otherwise); see the module docstring.
        allow_retired: replay a dataset retired with `rli replay retire`
            anyway. Without it a retired dataset raises
            `rli.replay.retire.RetiredDatasetError` before anything runs.

    Raises `LookupError` if the dataset has no cases, `RetiredDatasetError`
    for a retired dataset (see `allow_retired`), and `ValueError` for a
    system with no runner or an invalid/unsafe `shard` — all before any case
    runs. Once the walk starts, a per-case failure (the system raising, a
    `ReplayViolation`, or a `sqlite3.Error` in the resume/quota bookkeeping
    around it) is recorded in `outcomes` and counted in `errors`; it never
    ends the walk. Anything else escaping this loop is a bug in this module
    and is left to propagate (see the module docstring's judgment call).
    """
    rows = dataset_case_rows(conn, dataset_id)
    if not rows:
        raise LookupError(
            f"replay dataset {dataset_id!r} has no cases; build it first (`rli replay build`)"
        )
    ensure_not_retired(conn, dataset_id, allow_retired=allow_retired)

    system_name = str(system)
    execute = runner if runner is not None else SYSTEM_RUNNERS.get(system_name)
    if execute is None:
        raise ValueError(
            f"no runner for system {system_name!r}; pass runner= explicitly "
            f"(this module ships {sorted(SYSTEM_RUNNERS)})"
        )

    # The `(input_url, replay_at)` keys this call may delete runs for, or
    # `None` for an unsharded walk (see `_assert_deletable`).
    shard_keys: frozenset[tuple[str, str]] | None = None
    if shard is not None:
        shard = _checked_shard(shard)
        if not resume:
            raise ValueError(
                "a sharded replay must resume (resume=True / no --no-resume): workers that "
                "replace or re-run completed cases would delete each other's progress"
            )
        rows = _shard_rows(rows, shard)
        shard_keys = frozenset((row["canonical_url"], row["replay_at"]) for row in rows)

    replaced = (
        clear_replay_runs(conn, dataset_id=dataset_id, system=system_name)
        if replace and not resume and shard is None
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
    lock_retries: list[str] = []
    seen = 0
    # The `run_steps.error` text of the case that exhausted the daily LLM
    # quota, set only under `stop_on_quota`. It doubles as the walk's stop
    # flag: the case loop breaks on it, then the `T` loop does, so the walk
    # leaves the `point_in_time` block the ordinary way and there is exactly
    # one place a `ReplayRunSummary` is built.
    quota_stop: str | None = None
    # Runs made before this call are either all gone (`replace`) or are
    # deleted per case by `resume` before the case runs; only a
    # `replace=False, resume=False` call has to snapshot a case's earlier
    # runs so a lock-failed attempt deletes only what IT wrote (`_CaseAttempt`).
    fresh_case_state = resume or replace
    # Cases `resume` would skip, read once up front in one scan of `runs`.
    # Skipping them BEFORE `point_in_time` is what keeps a resumed walk from
    # paying a corpus install (~0.4 s on the real database) for every case
    # it already finished; a case not in this set still gets the per-case
    # check in `_CaseAttempt`, which reads the database afresh.
    done_at_start = (
        _completed_case_keys(conn, dataset_id=dataset_id, system=system_name) if resume else set()
    )

    for stamp, group in _group_by_replay_at(rows):
        if limit_cases is not None and seen >= limit_cases:
            break
        replay_at = parse_utc(stamp)
        # Only the companies that actually have a case at this `T`: the
        # lifecycle re-derivation is the expensive half of `point_in_time`,
        # and a case only ever reads its own company's history. On a grid
        # whose points are per-posting (each starts at that posting's
        # `first_observed`) this is usually a single company per `T`. The
        # context is entered lazily, on the first case of this `T` that is
        # not already done, and left with the group.
        with ExitStack() as corpus:
            corpus_open = False
            for row in group:
                if limit_cases is not None and seen >= limit_cases:
                    break
                seen += 1
                if (row["canonical_url"], row["replay_at"]) in done_at_start:
                    skipped += 1
                    continue
                if not corpus_open:
                    corpus.enter_context(
                        point_in_time(
                            conn,
                            replay_at,
                            company_ids=sorted({r["company_id"] for r in group}),
                        )
                    )
                    corpus_open = True

                attempt = _CaseAttempt(
                    conn=conn,
                    cfg=cfg,
                    execute=execute,
                    row=row,
                    dataset_id=dataset_id,
                    system=system_name,
                    replay_at=replay_at,
                    collection_status_csv=collection_status_csv,
                    resume=resume,
                    stop_on_quota=stop_on_quota,
                    shard_keys=shard_keys,
                    fresh_case_state=fresh_case_state,
                )
                attempt.run()
                lock_retries.extend(attempt.lock_retry_errors)

                if attempt.skipped:
                    skipped += 1
                    continue
                outcome = attempt.outcome
                assert outcome is not None  # every non-skipped attempt sets one
                if attempt.failed:
                    outcomes.append(outcome)
                    errors += 1
                    continue
                if attempt.quota_text is not None:
                    outcomes.append(outcome)
                    quota_stop = attempt.quota_text
                    break

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
        shard=None if shard is None else format_shard(shard),
        lock_retries=len(lock_retries),
        lock_retry_errors=tuple(lock_retries),
    )


def _shard_rows(rows: Sequence[sqlite3.Row], shard: tuple[int, int]) -> list[sqlite3.Row]:
    """This shard's cases, refusing a dataset whose case identity crosses shards.

    Cases are ASSIGNED by `(posting_id, replay_at)`, but `resume` and every
    deletion recognise a case's runs by `(input_url, replay_at)` (see
    `_case_run_rows`). If two cases shared a URL and `T` but hashed to
    different shards, two workers would each treat the other's run as their
    own — skipping it, or deleting it. No dataset built so far has such a
    pair, so this is a refusal, not a merge rule.
    """
    index, count = shard
    owner: dict[tuple[str, str], int] = {}
    mine: list[sqlite3.Row] = []
    for row in rows:
        assigned = shard_of(row["posting_id"], row["replay_at"], count)
        key = (row["canonical_url"], row["replay_at"])
        previous = owner.setdefault(key, assigned)
        if previous != assigned:
            raise ValueError(
                f"cannot shard: cases for {key[0]!r} at {key[1]} fall in shards "
                f"{previous} and {assigned}, and runs are matched to cases by (url, T)"
            )
        if assigned == index:
            mine.append(row)
    return mine


class _CaseLocked(Exception):
    """`_replay_one` returned an outcome whose error is a database lock."""

    def __init__(self, outcome: CaseOutcome) -> None:
        super().__init__(outcome.error)
        self.outcome = outcome


class _CaseAttempt:
    """One case of the walk: resume check, the run, the quota check, lock retry.

    A small class rather than more nesting in `run_replay`: the per-case state
    (`skipped`, `failed`, `quota_text`, `lock_retries`) is what the walk reads
    back, and the retry loop needs to know which runs existed before it
    started so it can delete exactly the ones a failed attempt wrote.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        cfg: Config,
        execute: Callable[..., RunResult],
        row: sqlite3.Row,
        dataset_id: str,
        system: str,
        replay_at: datetime,
        collection_status_csv: str | Path | None,
        resume: bool,
        stop_on_quota: bool,
        shard_keys: frozenset[tuple[str, str]] | None,
        fresh_case_state: bool,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.execute = execute
        self.row = row
        self.dataset_id = dataset_id
        self.system = system
        self.replay_at = replay_at
        self.collection_status_csv = collection_status_csv
        self.resume = resume
        self.stop_on_quota = stop_on_quota
        self.shard_keys = shard_keys
        self.fresh_case_state = fresh_case_state

        self.skipped = False
        self.failed = False
        self.outcome: CaseOutcome | None = None
        self.quota_text: str | None = None
        self.lock_retry_errors: list[str] = []

    def _prior(self) -> list[sqlite3.Row]:
        return _case_run_rows(
            self.conn,
            dataset_id=self.dataset_id,
            system=self.system,
            input_url=self.row["canonical_url"],
            replay_at=self.row["replay_at"],
        )

    def _delete(self, run_ids: Sequence[str]) -> None:
        _assert_deletable(self.conn, run_ids, self.shard_keys)
        _delete_run_ids(self.conn, run_ids)

    def run(self) -> None:
        for attempt in range(1, _CASE_ATTEMPTS + 1):
            # The case's run ids from before this attempt wrote anything;
            # `None` until known (a failure before that point wrote nothing).
            existing: set[str] | None = None
            outcome: CaseOutcome | None = None
            try:
                if self.resume:
                    prior = self._prior()
                    done = [r for r in prior if r["status"] == "completed"]
                    if done and not any(
                        _run_quota_detail(self.conn, r["id"]) is not None for r in done
                    ):
                        self.skipped = True
                        return
                    if prior:
                        self._delete([r["id"] for r in prior])
                existing = set() if self.fresh_case_state else {r["id"] for r in self._prior()}

                outcome = _replay_one(
                    self.conn,
                    self.cfg,
                    self.execute,
                    dataset_id=self.dataset_id,
                    posting_id=self.row["posting_id"],
                    url=self.row["canonical_url"],
                    replay_at=self.replay_at,
                    collection_status_csv=self.collection_status_csv,
                )
                if outcome.error is not None and _is_lock_error_text(outcome.error):
                    raise _CaseLocked(outcome)

                if self.stop_on_quota and outcome.run_id is not None:
                    quota_text = _run_quota_detail(self.conn, outcome.run_id)
                    if quota_text is not None:
                        self._delete([outcome.run_id])
                        self.quota_text = quota_text
                self.outcome = outcome
                return
            except (sqlite3.Error, _CaseLocked) as exc:
                # Everything the SYSTEM can raise is already a case outcome
                # by the time it gets here (`_replay_one`). What is left is
                # this module's own database work AROUND that call — the
                # resume path's `_case_run_rows` / `_delete_run_ids`, the
                # quota check, and the archive claim lookup `_replay_one`
                # makes before its own handler — plus a system run that died
                # on a lock (`_CaseLocked`). A failure here is this case's
                # failure, not the walk's. See the module docstring's
                # judgment call for why the class is `sqlite3.Error` and not
                # `Exception`.
                self.conn.rollback()
                if isinstance(exc, _CaseLocked):
                    outcome = exc.outcome
                    error = exc.outcome.error or "database is locked"
                    locked = True
                else:
                    error = _sqlite_error_text(exc)
                    locked = _is_lock_error(exc)
                if locked:
                    self._discard_partial_runs(existing)
                    if attempt < _CASE_ATTEMPTS:
                        self.lock_retry_errors.append(
                            f"{self.row['posting_id']} @ {self.row['replay_at']}: {error}"
                        )
                        time.sleep(_LOCK_RETRY_DELAY_S)
                        continue
                self.failed = True
                self.outcome = _failed_case(self.row, error, outcome)
                return

    def _discard_partial_runs(self, existing: set[str] | None) -> None:
        """Delete the runs a lock-failed attempt wrote for this case (best effort).

        Such a run can be anything from a bare `status='running'` row to a
        `completed` run whose explanation never landed — and `resume` skips
        a completed run forever — so it is removed rather than trusted. If
        the cleanup itself cannot get the lock, the run stays; a run that
        is not `completed` is still redone by the next `resume`.
        """
        if existing is None:
            return
        try:
            stray = [r["id"] for r in self._prior() if r["id"] not in existing]
            if stray:
                self._delete(stray)
        except sqlite3.Error:
            self.conn.rollback()


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
            error=(
                _sqlite_error_text(exc)
                if isinstance(exc, sqlite3.Error)
                else f"{type(exc).__name__}: {exc}"
            ),
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


class ShardCaseStatus(BaseModel):
    """One shard's slice of a `SystemCaseStatus` (`dataset_status(shards=N)`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shard: int
    total: int
    completed: int
    remaining: int


class SystemCaseStatus(BaseModel):
    """One system's completion count against one dataset's case grid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    total: int
    completed: int
    remaining: int
    #: Per-shard breakdown, only when `dataset_status` was asked for one.
    shards: tuple[ShardCaseStatus, ...] = ()


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
            for part in s.shards:
                lines.append(
                    f"    shard {part.shard}/{len(s.shards)}: completed={part.completed} "
                    f"remaining={part.remaining} total={part.total}"
                )
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


#: systems `runs.system`'s CHECK constraint allows; status is reported for all of them
#: unconditionally so a fresh, never-replayed dataset still shows "remaining = total".
_ALL_SYSTEMS = ("A", "B", "C", "C2", "R")


def _completed_case_keys(
    conn: sqlite3.Connection, *, dataset_id: str, system: str
) -> set[tuple[str, str]]:
    """`(input_url, replay_at)` of every case `run_replay(resume=True)` would skip.

    The bulk form of the per-case test in `_CaseAttempt.run`: the same
    `_case_run_rows` filter (mode, system, dataset suffix), the same "some
    run completed and no completed run carries a quota signature" rule —
    in one scan of `runs` instead of one per case, which on the real corpus
    is the difference between seconds and the better part of an hour.
    """
    suffix = f"|dataset:{dataset_id}"
    by_case: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT id, status, config_hash, input_url, replay_at FROM runs
        WHERE mode = 'replay' AND system = ? AND config_hash IS NOT NULL
        """,
        (system,),
    ):
        if str(row["config_hash"]).endswith(suffix):
            by_case.setdefault((row["input_url"], row["replay_at"]), []).append(row)
    done_keys: set[tuple[str, str]] = set()
    for key, prior in by_case.items():
        done = [r for r in prior if r["status"] == "completed"]
        if done and not any(_run_quota_detail(conn, r["id"]) is not None for r in done):
            done_keys.add(key)
    return done_keys


def dataset_status(
    conn: sqlite3.Connection, *, dataset_id: str, shards: int | None = None
) -> ReplayDatasetStatus:
    """Per-system `(completed, remaining)` counts over `dataset_id`'s case grid.

    One scan of `runs` per system (`_completed_case_keys`). With `shards=N`,
    each system's counts are also split by `shard_of` — the partition
    `run_replay(shard=(I, N))` walks — so a sharded replay's progress can be
    read per worker. Raises `LookupError` for an unbuilt dataset, matching
    `run_replay`'s message shape, so a caller can rely on the same exception
    either way.
    """
    rows = dataset_case_rows(conn, dataset_id)
    if not rows:
        raise LookupError(
            f"replay dataset {dataset_id!r} has no cases; build it first (`rli replay build`)"
        )
    if shards is not None and shards < 1:
        raise ValueError(f"shard count must be >= 1, got {shards}")

    total = len(rows)
    assignment = (
        [shard_of(row["posting_id"], row["replay_at"], shards) for row in rows]
        if shards is not None
        else []
    )
    by_system = []
    for system in _ALL_SYSTEMS:
        done_keys = _completed_case_keys(conn, dataset_id=dataset_id, system=system)
        flags = [(row["canonical_url"], row["replay_at"]) in done_keys for row in rows]
        completed = sum(flags)
        parts: list[ShardCaseStatus] = []
        if shards is not None:
            for index in range(shards):
                mine = [
                    flag for flag, owner in zip(flags, assignment, strict=True) if owner == index
                ]
                parts.append(
                    ShardCaseStatus(
                        shard=index,
                        total=len(mine),
                        completed=sum(mine),
                        remaining=len(mine) - sum(mine),
                    )
                )
        by_system.append(
            SystemCaseStatus(
                system=system,
                total=total,
                completed=completed,
                remaining=total - completed,
                shards=tuple(parts),
            )
        )
    return ReplayDatasetStatus(dataset_id=dataset_id, total_cases=total, by_system=tuple(by_system))
