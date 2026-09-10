"""Shared machinery for the spec.md §6 baseline systems (PLAN.md M3, last bullet).

spec.md §6 names four systems (A full probes, B rules, C agent, C2 agent +
learned ranking) and states one constraint over all of them: "All systems use
the same frozen action policy." A comparison between systems is only
meaningful if the ONLY thing that differs between them is which probes they
choose to run — so everything that is not that choice lives here and is
imported by `rli.eval.system_a` and `rli.eval.system_b` rather than
re-implemented per system:

* `Run` — the `runs` row, the `run_steps` trace, and continuous evidence
  numbering across a whole run;
* `ProbeRunner` — one traced probe execution (timing, cost, cache status,
  structured failure);
* `decide_and_finish` — evidence quality -> frozen action policy ->
  deterministic reasons -> `Decision`, plus the closing `runs` update;
* `config_hash` — the reproducibility fingerprint of the configuration a run
  was made under.

--------------------------------------------------------------------------
The write invariant: the eval runner is a READER of the collection corpus
--------------------------------------------------------------------------

Everything in `rli.eval` writes to exactly three tables — `runs`,
`run_steps`, `evidence` — plus `tool_cache` when caching is enabled
(`rli.net.ToolCache`, which is a cache, not evidence). It NEVER writes to
`companies`, `postings`, `posting_snapshots`, `board_snapshots`,
`board_snapshot_jobs`, `capture_attempts`, `company_events` or
`repost_links`.

That is not tidiness, it is the precondition for the spec.md §6 evaluation
being valid at all. The metrics are computed over the collection corpus; if
running System A could add a `board_snapshots` row or create a `postings`
row, then measuring A would change the data B and C are measured on, and a
system that runs more probes would silently enrich its own history features.
An A/B/C comparison over a corpus mutated by the systems under test is not a
comparison.

The most visible consequence: **a failed always-run probe does NOT get a
`capture_attempts` row here.** `rli.snapshots.daily` writes one because its
job is collection and spec.md §4 requires a failed capture to be recorded as
a coverage gap rather than an absence. This module's job is evaluation, and
the same failure is recorded where an evaluation needs it — in the
`run_steps` row's `error` column (spec.md §2's structured probe failure) —
without touching the coverage record that history features are derived from.
A run therefore cannot improve or degrade its own company's
`history_coverage`.

--------------------------------------------------------------------------
`cost_usd` is a placeholder UNITLESS COST POINT, not a dollar
--------------------------------------------------------------------------

`run_steps.cost_usd` is a REAL column named for dollars, and this module
writes `rli.config.ProbeCosts` values into it: `low=1`, `medium=3`,
`high=10`, documented in `rli.config.ProbeCosts` as "PLACEHOLDERS (arbitrary
unitless cost points ...) pending real measurement from live probe runs".
So `runs.total_cost_usd` for a run that executed a `low` and a `medium`
probe is `4.0`, meaning four cost points, not four dollars.

This is deliberate and it is consistent: `[budgets].max_cost_usd` is
compared against the same scale, and `rli.probes.registry.cost_value` ranks
against the same scale. When real per-probe money costs are measured, only
`[probe_costs]` changes and every consumer — budgets, ranking, this trace,
and `rli.eval.report`'s mean-cost-per-run — moves together. Renaming the
column is a schema migration that would invalidate existing traces, so the
unit is documented here instead of encoded in the name.

The always-run pair (`resolve_posting`, `board_snapshot`) is charged the
same way, from `cost_tier` — they are `low` — even though spec.md §4 puts
them outside the "Dynamic probes" cost table. Charging them zero would make
`runs.total_cost_usd` disagree with the sum of its own `run_steps`.

--------------------------------------------------------------------------
`run_steps.cache_status` is measured, not assumed
--------------------------------------------------------------------------

spec.md §2 requires the trace to record cache hits, and spec.md §6's replay
rules make "did this actually hit the network?" a load-bearing question. A
probe cannot report it (probes do not know they are being traced), and
guessing from the probe name would be a lie the moment a probe changes.

So the runner hands every probe a `_RecordingNetClient`, a `NetClient`
subclass that appends each `NetResult` to one shared log, and reads that
log's tail after the probe returns:

* `'n/a'` — the probe made no HTTP call at all (`company_events` reads only
  the local event store; `repost_history` reads only the database);
* `'hit'` — it made calls and every one was served from `tool_cache`
  (`NetResult.from_cache`);
* `'miss'` — it made calls and at least one reached the network.

A partially-cached probe is `'miss'`, the pessimistic reading: the claim
"this step cost nothing" must be true whenever it is recorded.

--------------------------------------------------------------------------
`decision_type` vocabulary
--------------------------------------------------------------------------

`run_steps.decision_type` is `NOT NULL` and free text. This package uses a
`kind[:qualifier[:qualifier]]` convention, so a trace stays queryable with a
`LIKE 'kind:%'` prefix match while still naming the branch that fired:

* `probe_run` — component `'probe'`; one probe execution.
* `probe_skipped:<reason>` — component `'controller'`; a probe that was NOT
  run, and why. Recorded rather than omitted because "board_snapshot did not
  run" and "board_snapshot ran and found nothing" are different facts and
  the trace must distinguish them.
* `policy_decision:<policy branch>:<quality rule>` — component
  `'controller'`; the frozen policy's fired branch
  (`rli.policy.action.PolicyOutcome.branch`) and the evidence-quality rule
  (`rli.policy.quality.QualityVerdict.rule`). spec.md §2: "The internal run
  trace ... stores probes, controller decisions".
* `route:<rule id>` — component `'controller'`; System B's routing decision
  (`rli.eval.system_b`).
* `system_version` — component `'controller'`; System B's frozen rules
  version (spec.md §6: "Freeze/version B before evaluating C").

`rli.replay.mode` adds two more under the same convention —
`replay_dataset:<dataset id>` and `replay_violation:<kind>` — so a replay
run's dataset and any attempted rule breach are visible in the canonical
trace. They are named there rather than here because this module must not
depend on `rli.replay` (see `ReplayHook`).

The columns keep their declared meanings: `args_hash` holds an argument
hash and nothing else, `probe_name` holds a probe name and nothing else.
Anything that is neither goes into `decision_type` (queryable, above) or
`error` (free text, and only ever for something that actually went wrong).

--------------------------------------------------------------------------
Other judgment calls
--------------------------------------------------------------------------

* **`runs.total_latency_ms` is the SUM OF STEP LATENCIES, not wall clock.**
  Wall clock would include this process's SQLite writes, the case-state
  derivation and the policy evaluation — work that is identical for every
  system and that would therefore dilute exactly the difference spec.md §6
  asks us to measure ("total cost, latency" as an agent-efficiency metric
  against the probes a system chose to run). The summed step latency is
  attributable per step and reconstructible from `run_steps`, which the wall
  clock is not. It is a LOWER bound on the user-visible latency, and
  `rli.eval.report` says so when it prints it.
* **Every write commits immediately.** `rli.net.ToolCache` documents that it
  takes ownership of write-commit on the connection it is given, and this
  runner deliberately gives it the same connection it writes `runs` /
  `run_steps` / `evidence` on (so a cache hit is visible in the same
  database as the trace that cites it). That is only safe if the runner
  never holds an in-flight multi-statement transaction across a probe call —
  hence commit-per-write here rather than one transaction per run. The cost
  is a partially-written trace if the process is killed mid-run, which is
  the correct trade: a trace of what actually happened up to the crash beats
  an atomic trace of nothing.
* **A probe that RAISES still gets its `run_steps` row**, with the exception
  text in `error`, before the exception is re-raised and `Run.__exit__`
  marks the run `'failed'`. Probes are contractually forbidden from raising
  for network/parse problems (`rli.probes.base`), so an exception here is a
  bug — and a bug that leaves no trace of which probe was running when it
  happened is much harder to fix. Nothing is swallowed.
* **`created_at` on every step is the run clock (`now`), not the wall
  clock.** A replayed or `now=`-overridden run must produce a trace whose
  timestamps are a function of its inputs (spec.md §6), and the real elapsed
  time is already recorded, per step, in `latency_s`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.models.decision import Decision
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, PolicyInputs
from rli.models.probe import ProbeResult
from rli.models.time import ensure_aware, now_utc, to_utc_z
from rli.net import NetClient, NetResult, hash_args
from rli.policy.action import decide, policy_version
from rli.policy.explain_stub import reasons_from_inputs
from rli.policy.inputs import last_publish_or_refresh
from rli.policy.quality import evidence_quality_detail
from rli.probes.base import Probe, ProbeClaim, ProbeContext
from rli.probes.persist import save_evidence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rli.history.features import PostingHistoryFeatures

__all__ = [
    "STEP_POLICY_DECISION",
    "STEP_PROBE_RUN",
    "STEP_PROBE_SKIPPED",
    "STEP_ROUTE",
    "STEP_SYSTEM_VERSION",
    "CacheStatus",
    "ProbeRunner",
    "ReplayHook",
    "Run",
    "RunResult",
    "SystemName",
    "config_hash",
    "decide_and_finish",
    "open_probe_runner",
    "open_system_runner",
    "replay_run_shape",
]

SystemName = Literal["A", "B", "C", "C2"]
CacheStatus = Literal["hit", "miss", "n/a"]

# `run_steps.decision_type` prefixes; see the module docstring's vocabulary.
STEP_PROBE_RUN = "probe_run"
STEP_PROBE_SKIPPED = "probe_skipped"
STEP_POLICY_DECISION = "policy_decision"
STEP_ROUTE = "route"
STEP_SYSTEM_VERSION = "system_version"


def config_hash(cfg: Config) -> str:
    """A stable fingerprint of the configuration a run was made under.

    Hashes the canonical JSON form of the whole `Config` (sorted keys,
    no whitespace), so any threshold, allowlist, budget or probe-cost change
    produces a different value. `runs.config_hash` records it next to
    `runs.policy_version`, which covers only the policy half: two runs that
    agree on `policy_version` can still differ in `min_history_days` or in a
    probe's allowlist, and spec.md §6's replay reproducibility needs both
    halves recorded.

    blake2b-128 rather than sha256 for the same reason
    `rli.policy.action.policy_version` uses it: this is a change detector,
    not a security primitive, and a short digest keeps the composed
    `config_hash` values System B writes (see `rli.eval.system_b`) readable.
    """
    payload = json.dumps(
        cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


# ---------------------------------------------------------------------------
# Network call recording (for an honest run_steps.cache_status)
# ---------------------------------------------------------------------------


class _RecordingNetClient(NetClient):
    """A `NetClient` that appends every `NetResult` it produces to a log.

    `NetClient.from_config` constructs through `cls(...)` with a fixed set of
    keyword arguments, so a subclass is built by it unchanged but cannot
    receive an extra `call_log=` through it. Hence the log defaults to a
    fresh list and `_NetClientPool` rebinds it to the shared one right after
    construction — the alternative, overriding `from_config` with a wider
    signature, buys nothing and breaks the parent's contract.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.call_log: list[NetResult] = []

    def get(self, *args: Any, **kwargs: Any) -> NetResult:
        result = super().get(*args, **kwargs)
        self.call_log.append(result)
        return result


def _cache_status(calls: Sequence[NetResult]) -> CacheStatus:
    """Cache status for one probe execution's network calls (module docstring)."""
    if not calls:
        return "n/a"
    if all(call.from_cache for call in calls):
        return "hit"
    return "miss"


@dataclass
class _NetClientPool:
    """One `NetClient` per allowlist name, all logging into one shared list.

    Per-name rather than per-call so the per-host `RateLimiter` state is
    shared across a run (the same reasoning as `rli.snapshots.daily`, which
    reuses one client across every target). One probe execution may pull
    several names — `resolve_posting` fetches the ATS under its own
    allowlist and the career page under `json_ld` — which is exactly why the
    log is shared rather than per client: `cache_status` is a property of the
    probe execution, not of one allowlist.
    """

    cfg: Config
    conn: sqlite3.Connection | None
    sleep: Callable[[float], None]
    calls: list[NetResult] = field(default_factory=list)
    clients: dict[str, NetClient] = field(default_factory=dict)

    def client_for(self, probe_name: str) -> NetClient:
        client = self.clients.get(probe_name)
        if client is None:
            recording = _RecordingNetClient.from_config(
                self.cfg, probe=probe_name, conn=self.conn, sleep=self.sleep
            )
            # See _RecordingNetClient's docstring: shared log attached post
            # construction, because from_config's keyword set is fixed.
            recording.call_log = self.calls  # type: ignore[attr-defined]
            self.clients[probe_name] = recording
            client = recording
        return client

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class Run:
    """One `runs` row plus its `run_steps` trace and evidence numbering.

    Use as a context manager. `__enter__` inserts the `runs` row with
    `status='running'`; `finish` closes it with the decision; `__exit__`
    marks it `'failed'` if an exception escaped, or `'stopped'` if the block
    ended without a decision (a legal `runs.status`, and the honest label for
    "we gave up before deciding").

    A probe returning `ok=False` is NOT an exception (spec.md §2 structured
    failure): the run completes normally with the failure recorded in that
    step's `error` column, and `rli.policy.quality` turns an always-run
    failure into `evidence_quality='weak'`.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        *,
        input_url: str,
        system: SystemName,
        config_hash: str,
        started_at: datetime,
        run_id: str | None = None,
        mode: Literal["live", "replay"] = "live",
        replay_at: datetime | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        # `uuid4().hex` rather than a content hash: two runs of the same URL
        # under the same config are legitimately distinct rows. `run_id=` is
        # the deterministic-test/replay override.
        self.id = run_id if run_id is not None else uuid4().hex
        self.input_url = input_url
        self.system: SystemName = system
        self.config_hash = config_hash
        self.started_at = started_at
        self.mode = mode
        self.replay_at = replay_at

        self.posting_id: str | None = None
        self._open = False
        self._finished = False
        self._step_index = 0
        self._next_evidence_index = 1
        self._total_cost_usd = 0.0
        self._total_latency_s = 0.0

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> Run:
        """Insert the `runs` row. Idempotent; called by `__enter__`."""
        if self._open:
            return self
        self.conn.execute(
            """
            INSERT INTO runs
                (id, posting_id, input_url, system, mode, replay_at, policy_version,
                 config_hash, started_at, status)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                self.id,
                self.input_url,
                self.system,
                self.mode,
                to_utc_z(self.replay_at) if self.replay_at is not None else None,
                policy_version(self.cfg),
                self.config_hash,
                to_utc_z(self.started_at),
            ),
        )
        self.conn.commit()
        self._open = True
        return self

    def __enter__(self) -> Run:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        if exc_type is not None:
            self._close(status="failed", decision=None, error=f"{exc_type.__name__}: {exc}")
            return False  # never swallow
        if not self._finished:
            self._close(status="stopped", decision=None, error=None)
        return False

    # -- trace -------------------------------------------------------------

    @property
    def next_evidence_index(self) -> int:
        """The `e{n}` index the next saved claim will receive."""
        return self._next_evidence_index

    @property
    def step_count(self) -> int:
        return self._step_index

    @property
    def total_cost_usd(self) -> float:
        """Running total of step `cost_usd` (placeholder cost points — docstring)."""
        return self._total_cost_usd

    @property
    def total_latency_s(self) -> float:
        return self._total_latency_s

    def step(
        self,
        *,
        component: Literal["controller", "model", "probe"],
        decision_type: str,
        probe_name: str | None = None,
        args_hash: str | None = None,
        prompt_hash: str | None = None,
        model_id: str | None = None,
        cache_status: CacheStatus | None = None,
        cost_usd: float | None = None,
        latency_s: float | None = None,
        error: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """Append one `run_steps` row with the next `step_index` (1-based)."""
        self._step_index += 1
        if cost_usd is not None:
            self._total_cost_usd += cost_usd
        if latency_s is not None:
            self._total_latency_s += latency_s
        self.conn.execute(
            """
            INSERT INTO run_steps
                (run_id, step_index, component, decision_type, probe_name, args_hash,
                 prompt_hash, model_id, cache_status, cost_usd, latency_s, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.id,
                self._step_index,
                component,
                decision_type,
                probe_name,
                args_hash,
                prompt_hash,
                model_id,
                cache_status,
                cost_usd,
                latency_s,
                error,
                to_utc_z(created_at if created_at is not None else self.started_at),
            ),
        )
        self.conn.commit()

    def save_evidence(
        self, *, probe: str, claims: Sequence[ProbeClaim], posting_id: str | None = None
    ) -> list[EvidenceItem]:
        """Persist `claims` as `e{n}..`, continuing this run's numbering.

        `posting_id` must be `None` unless a `postings` row exists for it:
        `evidence.posting_id` is a foreign key and `PRAGMA foreign_keys` is
        ON (`rli.db.connect`), so passing an id for a posting this system has
        never collected would fail the insert. The eval runner never creates
        the missing row (see the module docstring's write invariant), so
        company-scoped evidence for an uncollected posting is simply stored
        with a NULL `posting_id` — which the schema explicitly allows.
        """
        items = save_evidence(
            self.conn,
            run_id=self.id,
            probe=probe,
            claims=list(claims),
            posting_id=posting_id,
            start_index=self._next_evidence_index,
        )
        self._next_evidence_index += len(items)
        return items

    # -- posting identity --------------------------------------------------

    def set_posting_id(self, posting_id: str | None) -> bool:
        """Point this run at `posting_id`, but only if that `postings` row exists.

        `runs.posting_id` is a foreign key. A run against a URL we have never
        snapshotted resolves to a perfectly good `"{ats}:{tenant}:{job_id}"`
        identity that has no `postings` row, and the run must still be
        recorded (the schema's own comment says so: "an unresolved-URL run
        ... must still be recordable"). Returns whether the update happened,
        so a caller can trace the difference between "no identity" and
        "identity we do not collect".
        """
        if posting_id is None:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM postings WHERE posting_id = ?", (posting_id,)
        ).fetchone()
        if row is None:
            return False
        self.conn.execute("UPDATE runs SET posting_id = ? WHERE id = ?", (posting_id, self.id))
        self.conn.commit()
        self.posting_id = posting_id
        return True

    # -- closing -----------------------------------------------------------

    def finish(
        self, decision: Decision, *, status: Literal["completed", "stopped"] = "completed"
    ) -> None:
        """Close the run with its `Decision` (spec.md §1 shape) and totals."""
        self._close(status=status, decision=decision, error=None)

    def _close(
        self,
        *,
        status: Literal["completed", "failed", "stopped"],
        decision: Decision | None,
        error: str | None,
    ) -> None:
        # Nothing to close: either already closed, or the INSERT never
        # happened (constructed but never entered).
        if self._finished or not self._open:
            return
        if error is not None:
            # The failure itself is the exception the caller is already
            # propagating; recording it as a controller step keeps a crashed
            # run explicable from `run_steps` alone.
            self.step(
                component="controller",
                decision_type="run_failed",
                error=error,
                created_at=self.started_at,
            )
        self.conn.execute(
            """
            UPDATE runs
               SET finished_at = ?, status = ?, final_decision = ?,
                   total_cost_usd = ?, total_latency_ms = ?
             WHERE id = ?
            """,
            (
                # `finished_at` is the run clock, not the wall clock: every
                # timestamp this package writes is a function of `now` so a
                # `now=`-overridden or replayed run is reproducible. The real
                # elapsed time lives in `total_latency_ms` (summed per step).
                to_utc_z(self.started_at),
                status,
                decision.model_dump_json() if decision is not None else None,
                self._total_cost_usd,
                int(round(self._total_latency_s * 1000.0)),
                self.id,
            ),
        )
        self.conn.commit()
        self._finished = True


# ---------------------------------------------------------------------------
# ProbeRunner
# ---------------------------------------------------------------------------


@dataclass
class ProbeRunner:
    """One traced probe-execution facility, shared by every system.

    Bundles the four things a traced probe call needs — the `Run`, the
    `ProbeContext`, the config and the run clock — so a system module calls
    `probes.execute(cls, args)` and cannot forget to write the step row.
    That bundling is the reason this exists rather than free functions
    taking `(run, ctx, cfg, now)`: an untraced probe call would silently
    corrupt spec.md §6's medium/high-cost probe count.
    """

    run: Run
    ctx: ProbeContext
    cfg: Config
    now: datetime
    pool: _NetClientPool

    def execute(self, probe_cls: type[Probe], args: BaseModel) -> ProbeResult:
        """Run one probe, time it, and append its `run_steps` row.

        `cost_usd` comes from `[probe_costs]` via the probe's `cost_tier`,
        i.e. the same numbers `rli.probes.registry.cost_value` ranks by
        (placeholder cost points — see the module docstring).
        """
        name = probe_cls.name
        args_hash_value = hash_args(name, **args.model_dump(mode="json"))
        cost = float(self.cfg.probe_costs.value_for(probe_cls.cost_tier))
        watermark = len(self.pool.calls)
        started = time.perf_counter()

        try:
            result = probe_cls().run(args, self.ctx)
        except Exception as exc:
            # Contract violation (`rli.probes.base`: never raise for a
            # network/parse problem). Record which probe was running, then
            # let it propagate: Run.__exit__ marks the run 'failed'.
            self.run.step(
                component="probe",
                decision_type=STEP_PROBE_RUN,
                probe_name=name,
                args_hash=args_hash_value,
                cache_status=_cache_status(self.pool.calls[watermark:]),
                cost_usd=cost,
                latency_s=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                created_at=self.now,
            )
            raise

        latency_s = time.perf_counter() - started
        self.run.step(
            component="probe",
            decision_type=STEP_PROBE_RUN,
            probe_name=name,
            args_hash=args_hash_value,
            cache_status=_cache_status(self.pool.calls[watermark:]),
            cost_usd=cost,
            latency_s=latency_s,
            error=None if result.ok else (result.error or "unspecified probe failure"),
            created_at=self.now,
        )
        return result

    def save_evidence(
        self, *, probe: str, claims: Sequence[ProbeClaim], posting_id: str | None = None
    ) -> list[EvidenceItem]:
        """Persist claims for this run. Replay overrides this to apply the
        spec.md §3 `available_at <= T` gate (see rli.replay.mode).

        Every system's evidence goes through this one method rather than
        through `self.run.save_evidence` directly, so the point-in-time gate
        has exactly one place to live and a future system cannot forget it.
        Live mode delegates unchanged.
        """
        return self.run.save_evidence(probe=probe, claims=claims, posting_id=posting_id)

    def always_run_extra(self) -> list[tuple[str, list[ProbeClaim]]]:
        """Extra always-run claims contributed by the environment. Empty in live
        mode; in replay this is the archive-derived board state for T.

        Returned as `(probe name, claims)` pairs so the contributed evidence
        is attributed in the `evidence` table like any other probe's.
        """
        return []

    def note(
        self,
        decision_type: str,
        *,
        probe_name: str | None = None,
        args_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        """Append a controller step (a decision, not an execution)."""
        self.run.step(
            component="controller",
            decision_type=decision_type,
            probe_name=probe_name,
            args_hash=args_hash,
            error=error,
            created_at=self.now,
        )


@contextmanager
def open_probe_runner(
    conn: sqlite3.Connection,
    cfg: Config,
    run: Run,
    now: datetime,
    *,
    sleep: Callable[[float], None] = time.sleep,
    use_tool_cache: bool = True,
    collection_status_csv: str | Path | None = None,
) -> Iterator[ProbeRunner]:
    """Build a `ProbeRunner` (and its net clients), closing them on exit.

    `use_tool_cache=False` passes `conn=None` to every `NetClient`, so no
    `tool_cache` row is read or written. Tests need that: a respx-mocked
    fetch that was silently answered from a cache row left by an earlier
    test would assert nothing. Live runs keep it True — spec.md §2: "Cache
    tool results where valid."

    `sleep` is injected into both the retry backoff and the per-host rate
    limiter (`NetClient.from_config` threads it into both), so a test never
    waits on a real token bucket.

    `collection_status_csv` pins the pre-collected `company_events`
    collection-status file for every probe in this run. It rides on the
    `ProbeContext` rather than on any probe's args because it names WHICH
    corpus is read, not what is being asked of it — see
    `rli.probes.base.ProbeContext`, which also explains why keeping it out of
    `args_hash` is what makes a replay dataset portable. `None` leaves each
    probe on its own default.
    """
    pool = _NetClientPool(cfg=cfg, conn=conn if use_tool_cache else None, sleep=sleep)
    ctx = ProbeContext(
        conn=conn,
        config=cfg,
        net_client_factory=pool.client_for,
        now=lambda: now,
        collection_status_csv=collection_status_csv,
    )
    try:
        yield ProbeRunner(run=run, ctx=ctx, cfg=cfg, now=now, pool=pool)
    finally:
        pool.close()


@dataclass(frozen=True)
class ReplayHook:
    """Everything a system needs to run in replay mode (spec.md §6).

    Passed as the single `replay=` keyword `run_system_a` / `run_system_b`
    accept. It is defined HERE, not in `rli.replay`, so that `rli.eval` never
    imports `rli.replay` — the dependency runs one way only (replay knows
    about the systems; the systems know only this three-field record), which
    is what keeps the import graph acyclic and keeps a live run's code path
    free of any replay machinery.

    * `replay_at` — spec.md §6's historical time `T`. Written to
      `runs.replay_at` and used as the run's decision clock.
    * `dataset_id` — appended to `runs.config_hash` as `|dataset:<id>`, so
      one column identifies "this config, these rules, this replay dataset".
    * `open_runner` — builds the `ProbeRunner` that serves probe results from
      the cached full-probe record instead of the network.
    """

    replay_at: datetime
    dataset_id: str
    open_runner: Callable[
        [sqlite3.Connection, Config, Run, datetime], AbstractContextManager[ProbeRunner]
    ]

    def config_hash_suffix(self) -> str:
        """The `runs.config_hash` suffix identifying this replay dataset.

        Appended by each system to whatever `config_hash` it would have
        written live, so one column answers "which config, which rules,
        which replay dataset" without a join. Defined here rather than in
        each system so A, B and any future C cannot spell it differently —
        `rli.eval.baseline.collect_cases` matches on this exact literal, and
        `rli.replay.mode.ReplayContext.config_hash_suffix` must produce the
        same string.
        """
        return f"|dataset:{self.dataset_id}"


# ---------------------------------------------------------------------------
# The live/replay seam every system shares
# ---------------------------------------------------------------------------


def replay_run_shape(
    replay: ReplayHook | None, now: datetime | None, config_hash_live: str
) -> tuple[datetime, Literal["live", "replay"], str, datetime | None]:
    """`(clock, runs.mode, runs.config_hash, runs.replay_at)` for one run.

    The four `Run` values that differ between a live run and a replay run,
    derived in ONE place so System A, System B and any future System C
    cannot disagree about them — spec.md §6 requires that the only thing
    differing between systems is which probes they choose to run, and a
    system that recorded its replay identity differently would be
    incomparable for reasons that have nothing to do with its probes.

    The clock is `replay.replay_at` when replaying and no `now` was given: a
    replay run's decision clock IS `T` (spec.md §6). An explicit `now` is
    still honoured rather than silently overridden, because the hook itself
    rejects a mismatch (`rli.replay.mode.replay_hook`) — a loud failure beats
    a run whose `runs.replay_at` describes a different instant than the one
    its evidence gate used.
    """
    if replay is not None:
        moment = ensure_aware(now, "now") if now is not None else replay.replay_at
        return (
            moment,
            "replay",
            config_hash_live + replay.config_hash_suffix(),
            replay.replay_at,
        )
    moment = ensure_aware(now, "now") if now is not None else now_utc()
    return moment, "live", config_hash_live, None


@contextmanager
def open_system_runner(
    conn: sqlite3.Connection,
    cfg: Config,
    run: Run,
    now: datetime,
    *,
    replay: ReplayHook | None,
    sleep: Callable[[float], None] = time.sleep,
    use_tool_cache: bool = True,
    collection_status_csv: str | Path | None = None,
) -> Iterator[ProbeRunner]:
    """`open_probe_runner`, or the replay hook's cached-record runner.

    The one branch on "am I replaying?" that a system module contains. It is
    here rather than in each system for the same reason as
    `replay_run_shape`, and it keeps `rli.eval` free of any import from
    `rli.replay` (see `ReplayHook`).

    `collection_status_csv` is the pinned `company_events` collection state
    for the run, and reaches the probes through the `ProbeContext` — which is
    why it is threaded HERE rather than into `rli.eval.case.build_case_state`
    as it once was: the case builder no longer reads the events store at all,
    the `company_events` PROBE does (see that module's docstring).

    **In the replay branch this argument is ignored, deliberately.** There the
    `ProbeContext` is constructed by the `ReplayHook`'s `open_runner`, whose
    signature is fixed at `(conn, cfg, run, now)`, so the hook carries its own
    pinned path in the closure `rli.replay.mode.replay_hook` builds. A system
    still passes its `collection_status_csv=` through to here unconditionally
    (it cannot know whether it is replaying, and that is the point of this
    seam); under replay the hook's value — the one the dataset was built
    against — is the one that wins. Passing two different paths is therefore
    not an error but it is pointless: pin the same file in both places.
    """
    if replay is not None:
        with replay.open_runner(conn, cfg, run, now) as probes:
            yield probes
        return
    with open_probe_runner(
        conn,
        cfg,
        run,
        now,
        sleep=sleep,
        use_tool_cache=use_tool_cache,
        collection_status_csv=collection_status_csv,
    ) as probes:
        yield probes


# ---------------------------------------------------------------------------
# The shared decision step
# ---------------------------------------------------------------------------


class RunResult(BaseModel):
    """What a system returns: the user-facing `Decision` plus trace handles.

    `probes_run` is the dynamic probes this system actually executed, in
    execution order (the always-run pair is excluded — every system runs it,
    so including it would flatter spec.md §6's probe-count metric equally for
    all of them and hide the difference being measured). `route_rule` names
    System B's fired routing rule and is `None` for every other system.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    system: SystemName
    decision: Decision
    probes_run: tuple[str, ...] = ()
    route_rule: str | None = None


def decide_and_finish(
    probes: ProbeRunner,
    *,
    evidence: Sequence[EvidenceItem],
    inputs: PolicyInputs,
    features: PostingHistoryFeatures | None,
    failures: Sequence[ProbeResult],
) -> Decision:
    """Evidence -> quality -> frozen policy -> reasons -> `Decision`, and close the run.

    Every system funnels through this function, which is how spec.md §6's
    "All systems use the same frozen action policy" is enforced structurally
    rather than by two modules being careful in the same way.

    `failures` must contain the ALWAYS-RUN probe results only.
    `rli.policy.quality` is explicit about why: `ProbeResult` carries no
    probe name, so the function cannot tell an always-run probe from a
    dynamic one and the caller decides — and its documented reading is the
    always-run pair, because "a `team_signal` outage should not make a
    well-evidenced posting weak". That distinction matters far more here than
    anywhere else: System A runs every eligible dynamic probe and System B
    runs a few, so counting dynamic failures would give A systematically
    weaker evidence than B on identical postings and would corrupt the
    action-agreement metric spec.md §6 defines. Passing `ok=True` results is
    harmless (they are ignored), so callers may pass the whole always-run
    set.

    `long_lived` is threaded from the history features because
    `rli.policy.action.decide` takes it as a keyword rather than a
    `PolicyInputs` field (see that module's docstring); `None` features means
    no history, which is UNKNOWN — never `False` (spec.md §4: "missing
    history never means flat hiring"). `last_refreshed_at` is threaded for
    the same reason and derived from the evidence right here; see that
    module's "two keyword inputs" section for why it is not cached.
    """
    quality = evidence_quality_detail(evidence, inputs, list(failures), probes.cfg)
    outcome = decide(
        inputs,
        quality.quality,
        probes.now,
        probes.cfg,
        long_lived=features.long_lived if features is not None else UNKNOWN,
        # Recomputed from the evidence at every call site rather than carried
        # on `CaseState` — the choice is argued once, in
        # `rli.policy.action`'s "two keyword inputs" section.
        last_refreshed_at=last_publish_or_refresh(evidence),
    )
    reasons = reasons_from_inputs(inputs, evidence, now=probes.now)

    decision = Decision(
        posting_state=outcome.posting_state,
        recommended_action=outcome.recommended_action,
        recheck_after_days=outcome.recheck_after_days,
        evidence_quality=quality.quality,
        # spec.md §1: hypotheses are LLM-authored (`evergreen`, `paused`, ...
        # "are hypotheses, not observable states"). A and B make no model
        # call, so the honest value is the empty list, not a guess.
        hypotheses=[],
        reason=reasons,
        evidence=list(evidence),
    )

    probes.note(f"{STEP_POLICY_DECISION}:{outcome.branch}:{quality.rule}")
    probes.run.finish(decision)
    return decision
