"""Replay mode: the cached full-probe record and the rules that guard it.

spec.md §6 "Replay" states four rules for a simulated system at historical
time `T`, and this module implements three of them (the fourth — the
point-in-time view of the collection corpus — is `rli.replay.pit`):

```text
* expose only evidence with `available_at <= T`
* expose a dynamic result only if the simulated system selects that probe
* live **tool** calls are forbidden in replay; results come only from the
  cached full-probe record
* live **LLM** calls are allowed on cache miss ... and are recorded
```

Concretely:

* `ReplayProbeStore` is the cached full-probe record (`replay_probe_results`).
  It is read ONLY through `ReplayProbeRunner.execute`, i.e. only when the
  simulated system actually selects that probe — which is the second rule.
  Every served record is appended to `ReplayProbeStore.exposed`, so "what did
  this system actually see?" is an assertable fact rather than an assumption.
* `ReplayProbeRunner.save_evidence` drops every claim whose `available_at` is
  after `T` before it reaches the `evidence` table — the first rule, applied
  at the single choke point every system's evidence passes through
  (`rli.eval.runner.ProbeRunner.save_evidence`).
* `ReplayNetClient` / `ReplayNetPool` make the third rule structural rather
  than aspirational: in replay every probe is handed a `NetClient` whose
  `get` cannot reach the network at all. It raises `ReplayViolation`, records
  the attempt on the pool, and writes a `replay_violation:net_call`
  controller step so `rli.replay.leakage` can see it in the canonical trace
  (spec.md §7) rather than only in this process's memory.

--------------------------------------------------------------------------
Why a missing record is an exception, not a probe failure
--------------------------------------------------------------------------

`ReplayProbeRunner.execute` raises `ReplayViolation` when the store has no
record for `(dataset, posting, T, probe, args_hash)`. The tempting
alternative — returning `ProbeResult(ok=False, error="not in the replay
record")` — would be much quieter and much worse:

* `rli.policy.quality`'s Q1 rule turns an always-run failure into
  `evidence_quality='weak'`, which changes the action, which changes
  spec.md §6's action-agreement metric. A dataset gap would silently
  manifest as "the agent disagreed with A".
* A system that asks for a probe the dataset never recorded is, by
  definition, a system the dataset cannot evaluate. That is a dataset bug
  (the builder did not run System A's full probe set, or the arguments
  drifted), and a bug that reports itself as a plausible-looking metric is
  the most expensive kind.

The run is therefore marked `'failed'` by `Run.__exit__`, `rli.replay.run`
records it in its summary, and the failure is visible instead of averaged in.

--------------------------------------------------------------------------
JUDGMENT CALL / DEVIATION: `data` needs a typed JSON codec, not `json.dumps`
--------------------------------------------------------------------------

`ProbeResult.data` is declared `dict[str, Any]` and the probes in this
codebase put live Python objects into it, not only JSON scalars:

* `resolve_posting`, `board_snapshot`, `repost_history`, `company_events`,
  `requirements_drift` all return `data["evidence"]` as a list of live
  `rli.probes.base.ProbeClaim` objects (their callers turn those into
  `EvidenceItem`s rather than serializing them);
* `board_snapshot` returns `data["jobs"]` as a list of
  `rli.probes.board_snapshot.BoardJob` objects, and `rli.eval.case._board_claim`
  reads `job.job_id` off them — a plain JSON round-trip would hand it dicts
  and raise `AttributeError`;
* `company_events` returns `data["material_negative_event"]` /
  `data["freeze_or_pause"]` as `bool | rli.models.policy_inputs.Unknown`.
  A plain JSON round-trip would turn the UNKNOWN sentinel into the string
  `"__unknown__"`, which `PolicyInputs` rejects — and, had it not rejected
  it, a truthy string would have read as a known `True`, i.e. a fabricated
  layoff. Its third signal, `data["last_material_event_at"]`, needs no new
  codec entry: the probe renders a real date as an ISO-Z STRING and passes
  UNKNOWN / `None` through, so the envelope already covers the only
  non-JSON value in it (`_UNKNOWN_TYPE`).

  The DECISION no longer reads any of the three — `rli.policy.inputs`
  derives them from this probe's `evidence` claims, which is what makes the
  policy's answer and the probe's trace incapable of disagreeing — but the
  codec entry stays, and must: `data` is what a reader of
  `replay_probe_results` inspects to see what the probe found at `T`, and
  silently corrupting `UNKNOWN` into a truthy string there would misinform
  exactly the person auditing a `wait`.

So the store uses a small typed-envelope codec (`_encode` / `_decode`)
rather than bare `json.dumps`: `{"__rli_type__": "probe_claim", "v": {...}}`
and friends. A payload containing a type the codec does not know raises at
BUILD time with the type named, so a future probe that starts returning a
new object shape fails loudly while the dataset is being written rather than
silently while it is being evaluated. That is a deliberate extension of the
architecture's "the `evidence` key holds serialized `ProbeClaim`s" — the
`evidence` key alone is not sufficient for the probes this codebase actually
has.

--------------------------------------------------------------------------
Other judgment calls
--------------------------------------------------------------------------

* **`cache_status` is always `'hit'` in replay, never `'miss'`.** A replayed
  probe execution reaches no network by construction, so `'miss'` — whose
  documented meaning in `rli.eval.runner` is "at least one call reached the
  network" — would be a false statement about a run that made zero calls.
  `rli.replay.leakage` asserts the absence of `'miss'` for exactly this
  reason: a `'miss'` in a replay run means something got out.
* **`cost_usd` is charged normally.** A replayed probe is free in wall-clock
  terms, but spec.md §6's agent-efficiency metrics compare *how much a
  system chose to spend*, not what the evaluation harness paid. Charging
  zero would make every replayed system cost nothing and delete the metric.
* **`latency_s` is the measured store-lookup time**, not the original
  probe's latency. It is the honest reading of "how long did this step take"
  for the run being recorded; the original latency belongs to the build run's
  own `run_steps` row, where it was actually observed.
* **`ReplayContext.T` keeps its single-letter name.** It is spec.md §6's own
  name for the replay instant ("At historical time `T`"), and every docstring
  in this package refers to it that way.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.runner import STEP_PROBE_RUN, ProbeRunner, ReplayHook, Run
from rli.models.evidence import EvidenceItem
from rli.models.policy_inputs import UNKNOWN, Unknown
from rli.models.probe import ProbeResult
from rli.models.time import ensure_aware, parse_utc, to_utc_z
from rli.net import NetClient, NetResult, hash_args
from rli.probes.base import Probe, ProbeClaim, ProbeContext
from rli.probes.board_snapshot import BoardJob

__all__ = [
    "ARCHIVE_BOARD_STATE_PROBE",
    "STEP_REPLAY_DATASET",
    "STEP_REPLAY_VIOLATION",
    "ReplayContext",
    "ReplayNetClient",
    "ReplayNetPool",
    "ReplayProbeRunner",
    "ReplayProbeStore",
    "ReplayViolation",
    "StoredProbeRecord",
    "decode_probe_data",
    "encode_probe_data",
    "open_replay_probe_runner",
    "replay_hook",
]

# The synthetic "probe" name under which `rli.replay.build.archive_state_claims`
# stores the archive-era board state for one `(posting, T)`. It is not a
# `rli.probes` probe: it has no `ArgsModel`, no allowlist and no cost, because
# it makes no observation of its own — it is a re-reading of board captures the
# collector already took, contributed to the run by the replay ENVIRONMENT
# through `ProbeRunner.always_run_extra`.
ARCHIVE_BOARD_STATE_PROBE = "archive_board_state"

# `run_steps.decision_type` prefixes this module adds to the
# `rli.eval.runner` vocabulary (same `kind[:qualifier]` convention).
STEP_REPLAY_DATASET = "replay_dataset"
STEP_REPLAY_VIOLATION = "replay_violation"


class ReplayViolation(RuntimeError):
    """A replay-mode rule was broken (spec.md §6).

    Raised for a live tool call attempted from replay mode, and for a probe
    result the cached full-probe record does not contain. Both are bugs — in
    the replay wiring or in the dataset — and both must stop the run rather
    than degrade into a plausible-looking metric (see the module docstring).
    """


# ---------------------------------------------------------------------------
# Typed JSON codec for ProbeResult.data (see the module docstring)
# ---------------------------------------------------------------------------

_TYPE_KEY = "__rli_type__"
_VALUE_KEY = "v"

_UNKNOWN_TYPE = "unknown"
_CLAIM_TYPE = "probe_claim"
_BOARD_JOB_TYPE = "board_job"
_DATETIME_TYPE = "datetime"


def _encode(value: Any) -> dict[str, Any]:
    """`json.dumps(default=...)` hook: wrap a known non-JSON value in an envelope."""
    if isinstance(value, Unknown):
        return {_TYPE_KEY: _UNKNOWN_TYPE}
    if isinstance(value, ProbeClaim):
        return {_TYPE_KEY: _CLAIM_TYPE, _VALUE_KEY: value.model_dump(mode="json")}
    if isinstance(value, BoardJob):
        return {_TYPE_KEY: _BOARD_JOB_TYPE, _VALUE_KEY: value.model_dump(mode="json")}
    if isinstance(value, datetime):
        return {_TYPE_KEY: _DATETIME_TYPE, _VALUE_KEY: to_utc_z(value)}
    raise TypeError(
        f"cannot store {type(value).__name__} in replay_probe_results.data; "
        "a probe started returning a payload shape rli.replay.mode's codec does "
        "not know about — teach _encode/_decode about it (see the module docstring) "
        "rather than letting the value round-trip lossily"
    )


def _decode(obj: dict[str, Any]) -> Any:
    """`json.loads(object_hook=...)`: unwrap an envelope back into a live object."""
    kind = obj.get(_TYPE_KEY)
    if kind is None:
        return obj
    if kind == _UNKNOWN_TYPE:
        return UNKNOWN
    if kind == _CLAIM_TYPE:
        return ProbeClaim.model_validate(obj[_VALUE_KEY])
    if kind == _BOARD_JOB_TYPE:
        return BoardJob.model_validate(obj[_VALUE_KEY])
    if kind == _DATETIME_TYPE:
        return parse_utc(obj[_VALUE_KEY])
    raise ValueError(f"unknown replay payload envelope {kind!r}")


def encode_probe_data(data: dict[str, Any]) -> str:
    """Serialize a `ProbeResult.data` payload for `replay_probe_results.data`."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_encode)


def decode_probe_data(text: str) -> dict[str, Any]:
    """Rehydrate a `ProbeResult.data` payload, restoring live objects."""
    decoded = json.loads(text, object_hook=_decode)
    if not isinstance(decoded, dict):  # pragma: no cover - only a corrupt row
        raise ValueError("replay_probe_results.data did not decode to an object")
    return decoded


# ---------------------------------------------------------------------------
# ReplayContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayContext:
    """The historical instant `T` and the dataset being replayed.

    `T` must be timezone-aware: a naive datetime cannot be placed on the
    `available_at <= T` timeline (`rli.models.time`, spec.md §3/§6).
    """

    T: datetime
    dataset_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "T", ensure_aware(self.T, "T"))

    def config_hash_suffix(self) -> str:
        """The `runs.config_hash` suffix that identifies this dataset.

        Appended to whatever `config_hash` the system would have written
        live, so one column answers "which config, which rules, which replay
        dataset" without a join (the same reasoning `rli.eval.system_b` gives
        for composing its rules version into `config_hash`).
        """
        return f"|dataset:{self.dataset_id}"


# ---------------------------------------------------------------------------
# The forbidden network
# ---------------------------------------------------------------------------


def _no_sleep(_seconds: float) -> None:
    """Rate limiter / backoff sleep for a client that can never make a call."""
    return None


class ReplayNetClient(NetClient):
    """A `NetClient` whose `get` always raises `ReplayViolation`.

    spec.md §6: "live **tool** calls are forbidden in replay". The parent's
    constructor still runs (so an `httpx.Client` object exists and `close()`
    is well defined), but no request is ever issued through it: `get` raises
    before touching the allowlist check, the rate limiter or the transport.

    The pool is attached after construction for the same reason
    `rli.eval.runner._RecordingNetClient` attaches its log that way —
    `NetClient.from_config` constructs through `cls(...)` with a fixed
    keyword set and cannot pass an extra argument through.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pool: ReplayNetPool | None = None

    def get(self, url: str, *args: Any, **kwargs: Any) -> NetResult:
        probe_name = str(kwargs.get("probe") or self._probe or "<unbound>")
        message = (
            f"replay-mode probe {probe_name!r} attempted a live tool call to {url!r}; "
            "spec.md §6 forbids live tool calls in replay — results come only from "
            "the cached full-probe record (replay_probe_results)"
        )
        if self.pool is not None:
            self.pool.record_attempt(probe_name, url, message)
        raise ReplayViolation(message)


@dataclass
class ReplayNetPool:
    """The replay drop-in for `rli.eval.runner._NetClientPool`.

    Same duck type — `client_for(probe_name)`, `close()`, and a `calls` list
    the runner reads to compute `cache_status`. `calls` stays empty forever,
    which is what makes `rli.eval.runner._cache_status` report `'n/a'` for
    any probe executed through the ordinary path; `ReplayProbeRunner` never
    uses that path and writes `'hit'` explicitly (module docstring).

    `attempts` accumulates one message per forbidden call, so a caller can
    assert "zero net calls" directly (`rli.replay.leakage.net_call_count`)
    without going through the database.
    """

    cfg: Config
    attempts: list[str] = field(default_factory=list)
    calls: list[NetResult] = field(default_factory=list)
    clients: dict[str, ReplayNetClient] = field(default_factory=dict)
    # Set by `open_replay_probe_runner` to mirror an attempt into `run_steps`.
    on_violation: Callable[[str, str], None] | None = None

    def client_for(self, probe_name: str) -> ReplayNetClient:
        client = self.clients.get(probe_name)
        if client is None:
            # `conn=None`: no `tool_cache` is read or written either. A cached
            # HTTP body is still a tool result, and serving one in replay would
            # break the same rule by a quieter route.
            client = ReplayNetClient.from_config(  # type: ignore[assignment]
                self.cfg, probe=probe_name, conn=None, sleep=_no_sleep
            )
            client.pool = self
            self.clients[probe_name] = client
        return client

    def record_attempt(self, probe_name: str, url: str, message: str) -> None:
        self.attempts.append(message)
        if self.on_violation is not None:
            self.on_violation(probe_name, url)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()


# ---------------------------------------------------------------------------
# The cached full-probe record
# ---------------------------------------------------------------------------


class StoredProbeRecord(BaseModel):
    """One row of `replay_probe_results`, rehydrated.

    `result` is byte-for-byte what the live probe returned at build time,
    with `data["evidence"]` back to live `ProbeClaim`s and `data["jobs"]`
    back to live `BoardJob`s, so `rli.eval.case` cannot tell a replayed
    result from a live one. `observed_at` is when that observation was
    really made — NOT `T` (spec.md §3: do not backdate a current discovery).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_name: str
    args_hash: str
    observed_at: datetime
    result: ProbeResult


@dataclass
class ReplayProbeStore:
    """Reader/writer for `replay_probe_results`, with an exposure log.

    One instance per replay run (or per build). `exposed` records every
    `(probe_name, args_hash)` this store actually SERVED, in order, which is
    how spec.md §6's "expose a dynamic result only if the simulated system
    selects that probe" becomes testable: a row that exists in the table for
    a probe the system did not select is never read, so it never appears
    here, never becomes an `EvidenceItem`, and never produces a `run_steps`
    row.
    """

    exposed: list[tuple[str, str]] = field(default_factory=list)

    def save(
        self,
        conn: sqlite3.Connection,
        *,
        dataset_id: str,
        posting_id: str,
        replay_at: datetime,
        probe_name: str,
        args_hash: str,
        observed_at: datetime,
        result: ProbeResult,
        created_at: datetime | None = None,
    ) -> None:
        """Store one probe result for one `(dataset, posting, T)`.

        `INSERT OR REPLACE`: rebuilding a dataset re-observes the world, and
        the newest observation is the one the dataset should carry. The
        primary key already pins the identity, so a replace can only ever
        overwrite the same logical record.

        `data["observed_at"]` is injected (as a `to_utc_z` string) so a
        consumer that only has the `ProbeResult` — notably
        `rli.eval.case._posting_state_claim` — can still stamp a synthesized
        claim with the time the observation was really made.
        """
        data = dict(result.data or {})
        data["observed_at"] = to_utc_z(observed_at)
        stamp = to_utc_z(created_at if created_at is not None else observed_at)
        conn.execute(
            """
            INSERT OR REPLACE INTO replay_probe_results
                (dataset_id, posting_id, replay_at, probe_name, args_hash, observed_at,
                 ok, error, retryable, data, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_id,
                posting_id,
                to_utc_z(replay_at),
                probe_name,
                args_hash,
                to_utc_z(observed_at),
                1 if result.ok else 0,
                result.error,
                1 if result.retryable else 0,
                encode_probe_data(data),
                stamp,
            ),
        )
        conn.commit()

    def get(
        self,
        conn: sqlite3.Connection,
        *,
        dataset_id: str,
        posting_id: str,
        replay_at: datetime,
        probe_name: str,
        args_hash: str,
    ) -> StoredProbeRecord | None:
        """The cached record, or `None`. A served record is logged in `exposed`."""
        row = conn.execute(
            """
            SELECT probe_name, args_hash, observed_at, ok, error, retryable, data
            FROM replay_probe_results
            WHERE dataset_id = ? AND posting_id = ? AND replay_at = ?
              AND probe_name = ? AND args_hash = ?
            """,
            (dataset_id, posting_id, to_utc_z(replay_at), probe_name, args_hash),
        ).fetchone()
        if row is None:
            return None

        record = StoredProbeRecord(
            probe_name=row["probe_name"],
            args_hash=row["args_hash"],
            observed_at=parse_utc(row["observed_at"]),
            result=ProbeResult(
                ok=bool(row["ok"]),
                error=row["error"],
                retryable=bool(row["retryable"]),
                data=decode_probe_data(row["data"]),
            ),
        )
        self.exposed.append((record.probe_name, record.args_hash))
        return record

    def claims(
        self,
        conn: sqlite3.Connection,
        *,
        dataset_id: str,
        posting_id: str,
        replay_at: datetime,
        probe_name: str,
        args_hash: str,
    ) -> list[ProbeClaim]:
        """The `data["evidence"]` claims of one stored record (empty when absent).

        Used by `rli.replay.run` to load the `archive_board_state` record,
        which is environment-contributed rather than probe-selected and so
        has no `execute` call to hang off.
        """
        record = self.get(
            conn,
            dataset_id=dataset_id,
            posting_id=posting_id,
            replay_at=replay_at,
            probe_name=probe_name,
            args_hash=args_hash,
        )
        if record is None:
            return []
        return list((record.result.data or {}).get("evidence") or [])


# ---------------------------------------------------------------------------
# The replay ProbeRunner
# ---------------------------------------------------------------------------


@dataclass
class ReplayProbeRunner(ProbeRunner):
    """`ProbeRunner` that serves probe results from the cached record.

    `posting_id` is the DATASET's posting id, not one re-derived from the
    URL by the running system. Those two can legitimately differ: the real
    corpus contains archive-only postings keyed `"archive:{company}:{job}"`
    (created by `rli.history.closures`) which `rli.eval.case._resolve_identity`
    can never reconstruct from a job URL. Keying the lookup on the dataset's
    id keeps the record reachable in that case; keying it on the re-derived
    id would silently turn every such case into a `ReplayViolation`.
    """

    replay: ReplayContext
    store: ReplayProbeStore
    conn: sqlite3.Connection
    posting_id: str
    archive_claims: list[ProbeClaim] = field(default_factory=list)

    def execute(self, probe_cls: type[Probe], args: BaseModel) -> ProbeResult:
        """Serve one probe from the cached record, tracing it like a live run.

        `args_hash` is computed exactly as `ProbeRunner.execute` computes it,
        so a replayed lookup and a live cache key can never drift apart.
        """
        name = probe_cls.name
        args_hash_value = hash_args(name, **args.model_dump(mode="json"))
        started = time.perf_counter()
        record = self.store.get(
            self.conn,
            dataset_id=self.replay.dataset_id,
            posting_id=self.posting_id,
            replay_at=self.replay.T,
            probe_name=name,
            args_hash=args_hash_value,
        )
        latency_s = time.perf_counter() - started

        if record is None:
            message = (
                f"no cached result for probe {name!r} (args_hash={args_hash_value}) at "
                f"T={to_utc_z(self.replay.T)} for posting {self.posting_id!r} in dataset "
                f"{self.replay.dataset_id!r}; spec.md §6 allows no live tool call to fill "
                "the gap, and returning a synthetic failure would corrupt the "
                "action-agreement metric (see rli.replay.mode's docstring)"
            )
            self.note(
                f"{STEP_REPLAY_VIOLATION}:missing_probe_result",
                probe_name=name,
                args_hash=args_hash_value,
                error=message,
            )
            raise ReplayViolation(message)

        self.run.step(
            component="probe",
            decision_type=STEP_PROBE_RUN,
            probe_name=name,
            args_hash=args_hash_value,
            # Never 'miss': a replayed step reached no network at all.
            cache_status="hit",
            cost_usd=float(self.cfg.probe_costs.value_for(probe_cls.cost_tier)),
            latency_s=latency_s,
            error=(
                None if record.result.ok else (record.result.error or "unspecified probe failure")
            ),
            created_at=self.now,
        )
        return record.result

    def save_evidence(
        self, *, probe: str, claims: Sequence[ProbeClaim], posting_id: str | None = None
    ) -> list[EvidenceItem]:
        """Persist only the claims spec.md §3 permits at `T`.

        "Replay at historical time `T` may only expose `available_at <= T`."
        The gate is applied here, at the one function every system's evidence
        passes through, rather than in each system — so a future System C
        gets it for free and cannot forget it.

        The boundary is INCLUSIVE (`<=`, verbatim from spec.md §3): evidence
        that became available at exactly `T` was available at `T`.
        """
        kept = [claim for claim in claims if claim.available_at <= self.replay.T]
        return self.run.save_evidence(probe=probe, claims=kept, posting_id=posting_id)

    def always_run_extra(self) -> list[tuple[str, list[ProbeClaim]]]:
        """The archive-era board state for `T` (`rli.replay.build.archive_state_claims`).

        At an archive-era `T` the live resolver's answer is not available
        (`available_at > T`, so `save_evidence` drops it) and the honest
        observable state comes from the board captures that existed at `T`.
        Contributed here rather than by a probe because no probe observes it:
        it is a re-reading of captures the collector already took.
        """
        if not self.archive_claims:
            return []
        return [(ARCHIVE_BOARD_STATE_PROBE, list(self.archive_claims))]


@contextmanager
def open_replay_probe_runner(
    conn: sqlite3.Connection,
    cfg: Config,
    run: Run,
    now: datetime,
    *,
    replay: ReplayContext,
    store: ReplayProbeStore,
    posting_id: str,
    archive_claims: Sequence[ProbeClaim] = (),
    collection_status_csv: str | Path | None = None,
) -> Iterator[ReplayProbeRunner]:
    """Build a `ReplayProbeRunner` (and its forbidden net clients), closing them.

    Mirrors `rli.eval.runner.open_probe_runner`, with two differences: the
    pool can never make a call, and any attempt to make one is mirrored into
    `run_steps` as a `replay_violation:net_call` controller step so the
    leakage checker sees it in the canonical trace.

    `collection_status_csv` pins the pre-collected `company_events`
    collection state on the `ProbeContext`, exactly as `open_probe_runner`
    does. It matters more here than live: it must be the SAME file the
    dataset was built against, or a replayed `company_events` would answer
    "had we searched this company by T?" from a different corpus than the one
    the record was written from. It is not part of `args_hash` (see
    `rli.probes.base.ProbeContext`), which is precisely what keeps a stored
    record findable from any checkout.
    """
    pool = ReplayNetPool(cfg=cfg)

    def _record(probe_name: str, url: str) -> None:
        run.step(
            component="controller",
            decision_type=f"{STEP_REPLAY_VIOLATION}:net_call",
            probe_name=probe_name,
            error=f"forbidden live tool call to {url!r} in replay mode (spec.md §6)",
            created_at=now,
        )

    pool.on_violation = _record
    ctx = ProbeContext(
        conn=conn,
        config=cfg,
        net_client_factory=pool.client_for,
        now=lambda: now,
        collection_status_csv=collection_status_csv,
    )
    try:
        yield ReplayProbeRunner(
            run=run,
            ctx=ctx,
            cfg=cfg,
            now=now,
            pool=pool,  # type: ignore[arg-type]
            replay=replay,
            store=store,
            conn=conn,
            posting_id=posting_id,
            archive_claims=list(archive_claims),
        )
    finally:
        pool.close()


def replay_hook(
    *,
    replay: ReplayContext,
    store: ReplayProbeStore,
    posting_id: str,
    archive_claims: Sequence[ProbeClaim] = (),
    collection_status_csv: str | Path | None = None,
) -> ReplayHook:
    """The `ReplayHook` `run_system_a` / `run_system_b` accept (spec.md §6).

    The returned `open_runner` also writes the `replay_dataset:<id>`
    controller step, which is the first thing in every replay run's trace:
    `runs.config_hash` already carries the dataset, but spec.md §7 makes
    `run_steps` the canonical trace, and a reader walking one run should not
    have to join to `runs` to learn which dataset produced it.

    `collection_status_csv` is captured in the closure, and this is the ONLY
    way it can reach a replayed run's probes: `ReplayHook.open_runner` has a
    fixed `(conn, cfg, run, now)` signature — deliberately, so `rli.eval`
    never has to know what a replay needs — so the system's own
    `collection_status_csv=` argument cannot be forwarded into it. A replayed
    system therefore uses the file pinned HERE, by whoever built the hook
    (`rli.replay.run._replay_one`), which is the right authority: it is the
    same value the dataset was built with.
    """

    @contextmanager
    def _open(
        conn: sqlite3.Connection, cfg: Config, run: Run, moment: datetime
    ) -> Iterator[ProbeRunner]:
        if moment != replay.T:
            # The system's decision clock and the replay instant must be the
            # same value, or `runs.replay_at` would describe a different
            # moment than the one the evidence gate used.
            raise ReplayViolation(
                f"replay clock mismatch: system now={to_utc_z(moment)} but T={to_utc_z(replay.T)}"
            )
        with open_replay_probe_runner(
            conn,
            cfg,
            run,
            moment,
            replay=replay,
            store=store,
            posting_id=posting_id,
            archive_claims=archive_claims,
            collection_status_csv=collection_status_csv,
        ) as probes:
            probes.note(f"{STEP_REPLAY_DATASET}:{replay.dataset_id}")
            yield probes

    return ReplayHook(
        replay_at=replay.T,
        dataset_id=replay.dataset_id,
        open_runner=_open,
    )
