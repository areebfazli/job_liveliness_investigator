"""Build a point-in-time replay dataset (spec.md §6 "Replay"; PLAN.md M4).

PLAN.md M4 asks for a "Point-in-time builder: only `available_at <= T`;
dynamic results only if selected". This module is that builder. Given a split
(spec.md §6 "Splits") it picks postings, lays an evaluation grid of historical
times `T` over each one, and writes the three things a replay run needs:

* `replay_cases` — one `(posting, T)` subject per grid point;
* `replay_probe_results` — the **cached full-probe record**, which spec.md §6
  names as the ONLY place a replayed system may get a probe result from;
* `replay_datasets` — the dataset header (split, grid step, counts).

--------------------------------------------------------------------------
The full-probe record is collected ONCE per posting, not once per (posting, T)
--------------------------------------------------------------------------

The record is a set of LIVE observations. There is exactly one live world, so
observing it once per posting and storing that observation under every `T` of
that posting is not an approximation — it is the only thing that could be
true. What makes the dataset point-in-time is not *when* the observation was
made but the `available_at` gate that decides whether a replay at `T` may
LOOK at it (`rli.replay.mode.ReplayProbeRunner.save_evidence`), plus the
point-in-time corpus (`rli.replay.pit`).

So the builder runs **System A, live, once per posting** — literally
`build_case_state` + `eligible_probes(..., ALL_DYNAMIC_INPUTS)` +
`extend_case_state`, the same three calls `rli.eval.system_a` makes — through
a `ProbeRunner` that records every `(probe, args_hash) -> ProbeResult` it
executes. Running A rather than a hand-rolled "call every probe" loop is what
guarantees the record covers every probe any system could later select: A is
defined as the maximal selection, and B (and any future C) can only ever
choose a subset.

**Two probes are collected per `T` instead, and they are not exceptions to
the principle.** `company_events` and `team_signal` both take an `as_of`
argument, which `rli.probes.registry.build_args` fills from the run clock —
so at replay time each one's `args_hash` is a function of `T`, and a record
stored under the build clock's hash would never be found. Both are therefore
re-run once per `T` with `as_of=T`, by `_company_events_at` and
`_team_signal_at`, and the build-clock execution System A already made is
deliberately not stored (`_PER_T_PROBES`).

Neither re-run costs anything: neither probe makes a network call at all
(`[allowlists].company_events` and `[allowlists].team_signal` are both
empty), and both read only local tables. What each one BUYS is different,
and worth stating separately:

* `company_events` reads the pre-collected event store, which
  `rli.events.store.events_for` already filters by `available_at <= as_of`
  (spec.md §4: "pre-collect dated events and replay by `available_at`; do
  not live-search during benchmark replay"). Running it per `T` is what
  makes its answer honest at each `T` rather than one answer smeared across
  the grid.
* `team_signal` derives its claims from BOARD CAPTURES. Re-running it at `T`
  is what makes each claim's `available_at` a capture time at or before `T`
  — the `first_observed` of the latest new role, the latest
  `first_seen_absent`, or the last capture in the watched window for a
  negative — instead of the build instant. That is the difference between a
  claim the `available_at <= T` gate can judge on its merits and one it must
  drop at every archive-era `T`, which is precisely what security review H4
  turned out to be: `TeamSignalArgs` had no `as_of`, one build-clock record
  served every `T`, every claim in it was stamped at the build instant, and
  `corroborating_hiring_signal` was therefore UNKNOWN at every pre-build `T`
  in every dataset built before this change. Unlike `company_events`, this
  probe reads the capture corpus, so its per-`T` run also happens inside
  `rli.replay.pit.point_in_time` — see the judgment call below.

--------------------------------------------------------------------------
Archive-era `T`: the resolver's answer is NOT available, and that is the point
--------------------------------------------------------------------------

Every claim the live resolver produces carries `available_at = <the build
instant>` — spec.md §3 is explicit that archived or historical facts are NOT
backdated onto the timeline ("Do not backdate current discoveries merely
because the underlying event happened earlier"). So at any `T` before the
build, the gate drops the entire `resolve_posting` / `board_snapshot`
evidence set, and a system replayed there would see NO `posting_state` at all.

That is correct about the resolver and wrong about the world: at an
archive-era `T` we DO know something about the posting's state, because we
hold board captures taken at or before `T`. `archive_state_claims` reads
exactly those captures and emits the two claims the policy layer needs —
`posting_state` and `board_present`/`board_absent` — each stamped with the
CAPTURE's time, so they pass the gate on their own merits. They are stored
under the synthetic probe name
`rli.replay.mode.ARCHIVE_BOARD_STATE_PROBE` and delivered to the run through
`ProbeRunner.always_run_extra()`, not through a probe, because no probe
observes them: they are a re-reading of captures the collector already took.

What an archive-era case deliberately does NOT get is a `first_published`
claim. The ATS's publish date was fetched now, not then, so
`publish_recency` stays UNKNOWN and `rli.policy.quality`'s Q2 rule marks the
evidence `weak`. That is the honest state of an archive-era case and it is a
finding, not a defect: it is precisely what "we started collecting on day 0"
costs, and inventing a backdated publish claim to avoid it would be the
single most damaging thing this module could do.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **The grid endpoint is always included, even when it is not on the step.**
  `grid_times` walks `first_observed, +N days, ...` up to
  `min(first_seen_absent, now)` and then appends that endpoint if the walk
  did not land on it. For a still-open posting the endpoint IS the build
  instant, which is the ONLY `T` at which the live full-probe record passes
  the `available_at` gate; dropping it because it fell between two grid
  points would turn every case in the dataset into an archive-era case and
  quietly delete the comparison the dataset exists to support.

* **`first_seen_absent`, not `closure_absent_at`, bounds the grid.** Past the
  first observed absence the posting is gone; a `T` after it would ask "what
  should a user do about this posting" about a posting that was not there.
  (`rli.history.closures` distinguishes the two: `first_seen_absent` is the
  first disappearance, `closure_absent_at` brackets the last one.)

* **Postings are selected round-robin across companies.** `--limit-postings`
  on a `posting_id`-ordered scan would return one company's whole board.
  spec.md §6's company holdout and its ">=40 companies" data target both
  exist because per-company correlation is the dominant confound here, so a
  small dataset must spread across companies by construction rather than by
  luck. Companies are ordered by their own posting count (descending, id
  tiebreak) and one posting is taken from each in turn.

* **The `test` split is refused outright**, by reusing
  `rli.eval.baseline.HoldoutSplitRequestedError`. PLAN.md M4: "keep final
  holdouts untouched until M6". Building a dataset over the holdout is how
  you *spend* it — the live probe calls are the irreversible part — so the
  interlock belongs here, at the build, not only at the report.

* **The build run is recorded as an ordinary LIVE run** (`mode='live'`), with
  `config_hash` = `cfg:<hash>|replay_build:<dataset id>`. It really is a live
  System A run and spec.md §7 wants it traced. The suffix deliberately does
  NOT end in `|dataset:<id>`, which is the exact form
  `rli.eval.baseline.collect_cases` matches on, so a build run can never be
  mistaken for one of the replay runs it exists to make possible.

* **A cached `ProbeResult.data` payload is build-time, and that is safe only
  because nothing decides from it.** `repost_history`'s `data` carries
  `first_seen_absent`, `censoring`, `gap_days` and the best `repost_links`
  row as they stood when the record was collected — i.e. possibly after `T`.
  Its CLAIMS are point-in-time (each carries the capture timestamp it was
  derived from, so the gate drops a disappearance that had not happened at
  `T` yet), and every policy input is derived from claims plus
  `rli.history.features` under the point-in-time corpus — never from a
  `data` blob (`rli.policy.inputs` states that contract). So the stale half
  of the payload is inert. It is called out here because it is the one place
  where a future consumer that started reading `data` directly would
  reintroduce leakage that no test would catch, and the fix at that point is
  to re-run the probe per `T` the way `company_events` already is.

  `team_signal` USED to be in this category and has now taken exactly that
  fix. Its blob (`new_roles_30d`, `closures_60d`, `open_roles_now`,
  `corroborating_hiring_signal`) was build-time like `repost_history`'s, but
  one of those keys is a policy-input name, which made it the live half of
  the hazard rather than the inert half — see `rli.replay.leakage`'s
  `blob_input_exposures`. It is now re-run per `T`, so its blob and its
  claims describe the same instant and the same corpus, and a freshly built
  dataset should report 0 `team_signal` exposures. `repost_history` remains
  in this category: its blob keys are
  `rli.history.features.PostingHistoryFeatures` fields rather than policy
  inputs, nothing decides from them, and the same fix is still available if
  that ever changes.

* **The per-`T` `team_signal` run happens inside a point-in-time corpus;
  the per-`T` `company_events` run does not need one.** The difference is
  what each probe reads. `company_events` reads `company_events` through
  `rli.events.store.events_for`, whose `available_at <= as_of` filter is the
  same restriction `rli.replay.pit` would impose on that table, so a context
  would be a second copy of one rule. `team_signal` reads `postings` and
  `board_snapshots` — lifecycle aggregates over the whole capture history —
  and those are exactly what `point_in_time` re-derives.

  `rli.history.features.team_activity` is now `as_of`-bounded on its own
  (its module docstring proves why the monotonicity of `first_observed` and
  `first_seen_absent` makes that sound), so the context is the BELT to those
  BRACES rather than the only defence: with both in place, a future edit
  that loosened either one is caught by the other. The helper composes with
  an already-installed context via `rli.replay.pit.installed_shadows` and
  `contextlib.nullcontext`, the same way `case_state_at` does, because
  `point_in_time` refuses to nest; and the probe runs INSIDE the context
  while `ReplayProbeStore.save` runs after it has exited, so no write to
  `replay_probe_results` is made while the shadows are installed.

  The cost is affordable and was checked rather than assumed:
  `_install_postings` copies the whole `postings` table (~15k rows on the
  real database) and re-derives one company's lifecycle per call, which is
  milliseconds — against a build that is already making LIVE network calls
  per posting. Per-`T` point-in-time is therefore lost in the noise of the
  thing it is protecting.

* **A probe result is stored for every `T`, including results for probes a
  system will never select at that `T`.** Storage is cheap and the exposure
  rule is enforced on the READ side (`rli.replay.mode.ReplayProbeStore` logs
  every record it serves, and serves none it is not asked for). The
  alternative — predicting each system's selection at build time — would bake
  one system's routing into the dataset and make the dataset unusable for the
  next one.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rli.config import Config
from rli.eval.baseline import HoldoutSplitRequestedError, load_split_map
from rli.eval.case import CaseState, build_case_state, extend_case_state
from rli.eval.runner import ProbeRunner, Run, config_hash, decide_and_finish, open_probe_runner
from rli.eval.system_a import ALL_DYNAMIC_INPUTS
from rli.history.closures import Capture, load_captures
from rli.models.probe import ProbeResult
from rli.models.time import ensure_aware, now_utc, parse_utc, to_utc_z
from rli.net import hash_args
from rli.policy.inputs import CLAIM_BOARD_ABSENT, CLAIM_POSTING_STATE
from rli.policy.splits import DEFAULT_SEED, Split
from rli.probes.base import Probe, ProbeClaim, ProbeContext
from rli.probes.company_events import CompanyEventsProbe
from rli.probes.lookups import posting_row
from rli.probes.registry import build_args, eligible_probes
from rli.probes.repost_history import BOARD_HISTORY_URL_PLACEHOLDER
from rli.probes.team_signal import TeamSignalProbe
from rli.replay.mode import (
    ARCHIVE_BOARD_STATE_PROBE,
    ReplayContext,
    ReplayProbeStore,
    open_replay_probe_runner,
)
from rli.replay.pit import installed_shadows, point_in_time

__all__ = [
    "DEFAULT_GRID_STEP_DAYS",
    "BuildSummary",
    "CasePlan",
    "archive_state_args_hash",
    "archive_state_claims",
    "build_dataset",
    "case_state_at",
    "dataset_case_rows",
    "dataset_companies",
    "grid_times",
    "plan_cases",
]

#: Default spacing of the evaluation grid, in days. A month is coarse enough
#: that a 12-month archive window produces a dozen cases per posting rather
#: than hundreds of near-identical ones, and fine enough that a posting's
#: state can plausibly change between two points. Overridable per build
#: (`--grid-days`); it is a property of a dataset, which is why it is stored
#: on `replay_datasets.grid_step_days` rather than read from config.toml.
DEFAULT_GRID_STEP_DAYS = 30

# The always-run and dynamic claim types this module synthesizes from board
# captures. Imported from `rli.policy.inputs` / `rli.eval.case` rather than
# restated so the archive-era claims are read by the SAME policy code that
# reads the live ones.
_CLAIM_BOARD_PRESENT = "board_present"

#: Probes whose `args` carry an `as_of` and whose record is therefore
#: collected ONCE PER `T` (`_company_events_at`, `_team_signal_at`) rather
#: than taken from the build-clock System A run. See the module docstring's
#: "Two probes are collected per `T`" section. A set, not a chain of
#: `if`s, so adding a third such probe is one edit in one place.
_PER_T_PROBES = frozenset({CompanyEventsProbe.name, TeamSignalProbe.name})


# ---------------------------------------------------------------------------
# The evaluation grid
# ---------------------------------------------------------------------------


def grid_times(first_observed: datetime, end: datetime, step_days: int) -> list[datetime]:
    """Evaluation times for one posting: `first_observed`, +N days, ..., `end`.

    Both endpoints are included; see the module docstring for why the end
    matters so much. Returns `[]` when `end < first_observed` (a posting whose
    recorded absence predates its first sighting — corrupt lifecycle data,
    which must be reported as skipped rather than turned into one arbitrary
    case).
    """
    if step_days <= 0:
        raise ValueError(f"grid step must be a positive number of days, got {step_days}")
    first_observed = ensure_aware(first_observed, "first_observed")
    end = ensure_aware(end, "end")
    if end < first_observed:
        return []

    times: list[datetime] = []
    moment = first_observed
    step = timedelta(days=step_days)
    while moment <= end:
        times.append(moment)
        moment += step
    if not times or times[-1] != end:
        times.append(end)
    return times


# ---------------------------------------------------------------------------
# Archive-era board state
# ---------------------------------------------------------------------------


def archive_state_args_hash(posting_id: str, replay_at: datetime) -> str:
    """The `replay_probe_results.args_hash` of one archive-board-state record.

    `ARCHIVE_BOARD_STATE_PROBE` is not a `rli.probes` probe and has no
    `ArgsModel`, but the store's primary key needs an `args_hash` — so the
    two inputs that actually determine the record are hashed with the same
    `rli.net.hash_args` every real probe uses. `rli.replay.run` recomputes it
    to load the record; keeping the definition here (rather than inlining it
    at both call sites) is what stops the writer and the reader from drifting.
    """
    return hash_args(
        ARCHIVE_BOARD_STATE_PROBE,
        posting_id=posting_id,
        replay_at=to_utc_z(replay_at),
    )


def _capture_quality(capture: Capture) -> Literal["ats_native", "archive"]:
    """Source quality of a claim witnessed by `capture` (spec.md §3 ranking).

    Our own daily capture IS the ATS's own answer, so `ats_native`; a Wayback
    capture is `archive`. Same mapping as
    `rli.probes.repost_history._source_quality`, and the same conservative
    default: anything that is not an own capture degrades to `archive`.
    """
    return "ats_native" if capture.source == "own" else "archive"


def archive_state_claims(
    conn: sqlite3.Connection, posting_id: str, replay_at: datetime
) -> list[ProbeClaim]:
    """The posting's observable board state at `T`, from captures alone.

    Returns two claims — `posting_state` (`open`/`closed`) and the matching
    `board_present`/`board_absent` — both stamped with the CAPTURE time that
    witnesses them, which is what lets them pass the `available_at <= T` gate
    at an archive-era `T` where the live resolver's answer cannot.

    Returns `[]`, not a fabricated `unknown`, when there is nothing to say:
    no `postings` row, no ATS job id to look for, or no capture at or before
    `T` that ever listed the job. "We had not seen this posting yet" is
    absence of evidence and must reach the policy as UNKNOWN through the
    ordinary path (no claim -> `posting_state` UNKNOWN -> `weak` -> `wait`),
    never as an observation.

    The state rule mirrors `rli.history.closures` exactly, including its
    coverage rule (spec.md §4: "a throttled or failed capture is recorded as
    a coverage gap, never as an absence"): PRESENCE counts from a capture of
    any `coverage_status`, ABSENCE only from a `'complete'` one.
    """
    replay_at = ensure_aware(replay_at, "replay_at")
    row = posting_row(conn, posting_id)
    if row is None or row["ats_job_id"] is None:
        return []

    company_id = row["company_id"]
    job_id = str(row["ats_job_id"])
    captures = [c for c in load_captures(conn, company_id) if c.captured_at <= replay_at]
    present = [index for index, c in enumerate(captures) if job_id in c.jobs]
    if not present:
        return []

    last_present = present[-1]
    absent_index = next(
        (
            index
            for index in range(last_present + 1, len(captures))
            if captures[index].is_complete and job_id not in captures[index].jobs
        ),
        None,
    )

    if absent_index is None:
        witness = captures[last_present]
        facts = witness.jobs[job_id]
        state = "open"
        board_claim_type = _CLAIM_BOARD_PRESENT
        source_url = facts.url or BOARD_HISTORY_URL_PLACEHOLDER.format(
            company_id=company_id, job_id=job_id
        )
        excerpt = facts.title
    else:
        witness = captures[absent_index]
        state = "closed"
        board_claim_type = CLAIM_BOARD_ABSENT
        # An absence has no fetchable URL by construction; cite the capture
        # record itself, in the established self-describing scheme.
        source_url = BOARD_HISTORY_URL_PLACEHOLDER.format(company_id=company_id, job_id=job_id)
        excerpt = f"{len(witness.jobs)} job(s) listed at capture, none with id {job_id}"

    seen_at = witness.captured_at
    quality = _capture_quality(witness)
    return [
        ProbeClaim(
            claim_type=CLAIM_POSTING_STATE,
            value=state,
            source_url=source_url,
            raw_excerpt=(
                f"board capture {to_utc_z(seen_at)} "
                f"(source={witness.source}, coverage={witness.coverage_status})"
            ),
            source_quality=quality,
            source_event_at=seen_at,
            available_at=seen_at,
            fetched_at=seen_at,
        ),
        ProbeClaim(
            claim_type=board_claim_type,
            value=job_id,
            source_url=source_url,
            raw_excerpt=excerpt,
            source_quality=quality,
            source_event_at=seen_at,
            available_at=seen_at,
            fetched_at=seen_at,
        ),
    ]


# ---------------------------------------------------------------------------
# Case selection
# ---------------------------------------------------------------------------


class CasePlan(BaseModel):
    """One posting selected for a dataset, with the grid laid over it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    posting_id: str
    company_id: str
    canonical_url: str
    first_observed: datetime
    end: datetime
    times: tuple[datetime, ...]


def _candidate_rows(
    conn: sqlite3.Connection, splits: dict[str, Split] | dict[str, str], split: str
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT posting_id, company_id, canonical_url, first_observed, first_seen_absent
        FROM postings
        WHERE first_observed IS NOT NULL AND canonical_url LIKE 'http%'
        ORDER BY posting_id
        """
    ).fetchall()
    return [row for row in rows if splits.get(row["posting_id"]) == split]


def _stable_key(seed: int, posting_id: str) -> str:
    """Deterministic per-`(seed, posting)` ordering key, independent of process.

    Python's `hash()` of a `str` is salted per process (`PYTHONHASHSEED`), so
    it cannot be used for anything that has to be reproducible — the same
    reasoning, and the same blake2b construction, as
    `rli.policy.splits._stable_company_key`.
    """
    return hashlib.blake2b(f"{seed}:{posting_id}".encode(), digest_size=16).hexdigest()


def plan_cases(
    conn: sqlite3.Connection,
    *,
    splits: dict[str, Split] | dict[str, str],
    split: str,
    now: datetime,
    grid_step_days: int = DEFAULT_GRID_STEP_DAYS,
    limit_postings: int | None = None,
    seed: int = DEFAULT_SEED,
) -> list[CasePlan]:
    """Pick postings from `split` and lay the evaluation grid over each.

    Only postings with a `first_observed` and a fetchable `canonical_url` are
    eligible: without the first there is no grid start, and without the second
    the builder cannot make the live observation the record is made of
    (`rli.history.closures` writes an `archive-only:...` placeholder url for
    a posting it only ever saw inside a board listing).

    Selection is round-robin across companies — see the module docstring.
    """
    rows = _candidate_rows(conn, splits, split)

    by_company: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_company.setdefault(row["company_id"], []).append(row)

    # WITHIN a company, order by a stable hash of `posting_id`, not by
    # `posting_id` itself. Ids are PREFIXED by ATS — `archive:...`,
    # `greenhouse:...`, `ashby:...`, `lever:...` — so a lexical order makes
    # the first pick of every company an `archive:`-prefixed posting (the
    # archive-only rows `rli.history.closures` creates), and a 20-posting
    # dataset ends up made entirely of one kind. That is a systematic sample,
    # not a small one. Hashing `(seed, posting_id)` — the same trick and the
    # same reason as `rli.policy.splits._stable_company_key` — keeps the order
    # reproducible across processes and independent of the query plan while
    # being uncorrelated with the id's prefix.
    for bucket in by_company.values():
        bucket.sort(key=lambda row: _stable_key(seed, row["posting_id"]))

    # Largest boards first so a small limit still reaches the companies with
    # the most history; `company_id` breaks ties so the order never depends
    # on the query plan.
    companies = sorted(by_company, key=lambda cid: (-len(by_company[cid]), cid))

    ordered: list[sqlite3.Row] = []
    depth = 0
    while any(len(by_company[cid]) > depth for cid in companies):
        for cid in companies:
            bucket = by_company[cid]
            if len(bucket) > depth:
                ordered.append(bucket[depth])
        depth += 1

    plans: list[CasePlan] = []
    for row in ordered:
        if limit_postings is not None and len(plans) >= limit_postings:
            break
        first_observed = parse_utc(row["first_observed"])
        absent = row["first_seen_absent"]
        end = min(parse_utc(absent), now) if absent is not None else now
        times = grid_times(first_observed, end, grid_step_days)
        if not times:
            continue
        plans.append(
            CasePlan(
                posting_id=row["posting_id"],
                company_id=row["company_id"],
                canonical_url=row["canonical_url"],
                first_observed=first_observed,
                end=end,
                times=tuple(times),
            )
        )
    return plans


# ---------------------------------------------------------------------------
# The recording live run
# ---------------------------------------------------------------------------


class _Observation(BaseModel):
    """One live probe execution, kept for storage under every grid `T`."""

    model_config = ConfigDict(frozen=True)

    probe_name: str
    args_hash: str
    observed_at: datetime
    result: ProbeResult


@dataclass
class _RecordingProbeRunner(ProbeRunner):
    """A `ProbeRunner` that also keeps every result it produced.

    Subclassing (rather than wrapping `probe_cls().run` at the call site) is
    what makes the record a byte-for-byte copy of what the SYSTEM saw: the
    parent still does the timing, costing and `run_steps` tracing, and the
    recording happens after it, on the same object every system uses.
    """

    observations: dict[tuple[str, str], _Observation] = field(default_factory=dict)

    def execute(self, probe_cls: type[Probe], args: BaseModel) -> ProbeResult:
        result = super().execute(probe_cls, args)
        args_hash_value = hash_args(probe_cls.name, **args.model_dump(mode="json"))
        self.observations[(probe_cls.name, args_hash_value)] = _Observation(
            probe_name=probe_cls.name,
            args_hash=args_hash_value,
            observed_at=self.now,
            result=result,
        )
        return result


@contextmanager
def _recording_runner(
    conn: sqlite3.Connection,
    cfg: Config,
    run: Run,
    now: datetime,
    *,
    use_tool_cache: bool,
    collection_status_csv: str | Path | None = None,
) -> Iterator[_RecordingProbeRunner]:
    with open_probe_runner(
        conn,
        cfg,
        run,
        now,
        use_tool_cache=use_tool_cache,
        collection_status_csv=collection_status_csv,
    ) as base:
        yield _RecordingProbeRunner(
            run=base.run, ctx=base.ctx, cfg=base.cfg, now=base.now, pool=base.pool
        )


# ---------------------------------------------------------------------------
# case_state_at
# ---------------------------------------------------------------------------


def case_state_at(
    conn: sqlite3.Connection,
    cfg: Config,
    posting_id: str,
    replay_at: datetime,
    dataset_id: str,
    *,
    canonical_url: str | None = None,
    include_dynamic: bool = False,
    store: ReplayProbeStore | None = None,
    run_id: str | None = None,
    collection_status_csv: str | Path | None = None,
) -> CaseState:
    """The case state one system would see for `posting_id` at `T`.

    This is `rli.eval.case.build_case_state` run against the cached full-probe
    record instead of the network, inside a point-in-time corpus — i.e. all
    four spec.md §6 replay rules at once, with no system's probe selection on
    top. It exists so "what did the system actually see at `T`?" can be
    answered and asserted directly, rather than inferred from a decision.

    It opens its own `rli.replay.pit.point_in_time` context UNLESS one is
    already installed on `conn`, so it composes with `rli.replay.run`'s
    per-`T` context instead of fighting it (nesting is an error — see
    `rli.replay.pit`).

    `include_dynamic=True` additionally runs System A's selection through
    `extend_case_state`. It is off by default because "the case state" in
    `rli.eval.case`'s sense is what the ALWAYS-RUN pair established; the
    dynamic probes are each system's own choice, which is exactly the degree
    of freedom `rli.replay.run` is measuring.

    The `runs` row this writes is deliberately marked with a `config_hash` of
    `cfg:<hash>|dataset:<id>|case_state` — the dataset id is present so the
    row is traceable, but the string does not END in `|dataset:<id>`, which
    is the form `rli.eval.baseline.collect_cases` matches on. An inspection
    run must never be counted as a system run. The run is left `'stopped'`
    (no decision was made), which is its honest status.
    """
    replay_at = ensure_aware(replay_at, "replay_at")
    row = posting_row(conn, posting_id)
    url = canonical_url or (row["canonical_url"] if row is not None else None)
    if url is None:
        raise ValueError(
            f"no canonical_url for posting {posting_id!r}; pass canonical_url= "
            "explicitly (a replay case is identified by the URL a system was asked "
            "about, and there is no row to read one from)"
        )

    probe_store = store if store is not None else ReplayProbeStore()
    replay = ReplayContext(T=replay_at, dataset_id=dataset_id)
    archive_claims = probe_store.claims(
        conn,
        dataset_id=dataset_id,
        posting_id=posting_id,
        replay_at=replay_at,
        probe_name=ARCHIVE_BOARD_STATE_PROBE,
        args_hash=archive_state_args_hash(posting_id, replay_at),
    )

    company_ids = [row["company_id"]] if row is not None else None
    pit = (
        nullcontext(conn)
        if installed_shadows(conn)
        else point_in_time(conn, replay_at, company_ids=company_ids)
    )

    with pit:
        with Run(
            conn,
            cfg,
            input_url=url,
            system="A",
            config_hash=f"cfg:{config_hash(cfg)}|dataset:{dataset_id}|case_state",
            started_at=replay_at,
            run_id=run_id,
            mode="replay",
            replay_at=replay_at,
        ) as run:
            with open_replay_probe_runner(
                conn,
                cfg,
                run,
                replay_at,
                replay=replay,
                store=probe_store,
                posting_id=posting_id,
                archive_claims=archive_claims,
                collection_status_csv=collection_status_csv,
            ) as probes:
                case = build_case_state(
                    conn,
                    cfg,
                    url=url,
                    now=replay_at,
                    probes=probes,
                )
                run.set_posting_id(case.posting_id)
                case_file = case.case_file()
                if include_dynamic and case_file is not None:
                    extend_case_state(
                        case,
                        eligible_probes(
                            probes.ctx, case_file, unpopulated_inputs=set(ALL_DYNAMIC_INPUTS)
                        ),
                        probes=probes,
                    )
    return case


# ---------------------------------------------------------------------------
# build_dataset
# ---------------------------------------------------------------------------


class BuildSummary(BaseModel):
    """What one `build_dataset` call produced (every counter is auditable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    split_kind: str
    split_name: str
    grid_step_days: int
    created_at: str

    postings: int = 0
    companies: int = 0
    cases: int = 0
    probe_records: int = 0
    archive_state_records: int = 0
    cases_with_archive_state: int = 0
    live_probe_executions: int = 0
    postings_failed: int = 0
    failures: tuple[str, ...] = ()

    def describe(self) -> str:
        lines = [
            f"replay dataset {self.dataset_id!r}: split={self.split_kind}/{self.split_name} "
            f"grid={self.grid_step_days}d created_at={self.created_at}",
            f"  postings={self.postings} companies={self.companies} cases={self.cases}",
            f"  probe records={self.probe_records} (live executions={self.live_probe_executions})",
            f"  archive board-state records={self.archive_state_records} "
            f"(cases with an observable archive state: {self.cases_with_archive_state})",
            f"  postings that failed to build: {self.postings_failed}",
        ]
        lines.extend(f"    - {failure}" for failure in self.failures)
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.describe()


def _upsert_dataset(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    created_at: datetime,
    split_kind: str,
    split_name: str,
    grid_step_days: int,
    postings: int,
    companies: int,
    cases: int,
    notes: str | None,
) -> None:
    """Write the `replay_datasets` header, creating or updating it in place.

    Called TWICE per build: once before any case is written (because
    `replay_cases.dataset_id` is a foreign key into this table, so the header
    must exist first) and once after, with the final counts.

    `ON CONFLICT ... DO UPDATE` rather than `INSERT OR REPLACE`: REPLACE
    DELETEs the existing row before re-inserting it, and with
    `PRAGMA foreign_keys = ON` (`rli.db.connect`) that delete fails against
    the `replay_cases` children the first pass already wrote. An upsert
    updates the row in place and never orphans anything.
    """
    conn.execute(
        """
        INSERT INTO replay_datasets
            (dataset_id, created_at, split_kind, split_name, grid_step_days,
             postings, companies, cases, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (dataset_id) DO UPDATE SET
            created_at = excluded.created_at,
            split_kind = excluded.split_kind,
            split_name = excluded.split_name,
            grid_step_days = excluded.grid_step_days,
            postings = excluded.postings,
            companies = excluded.companies,
            cases = excluded.cases,
            notes = excluded.notes
        """,
        (
            dataset_id,
            to_utc_z(created_at),
            split_kind,
            split_name,
            grid_step_days,
            postings,
            companies,
            cases,
            notes,
        ),
    )
    conn.commit()


def _insert_case(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    plan: CasePlan,
    replay_at: datetime,
    built_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO replay_cases
            (dataset_id, posting_id, replay_at, company_id, canonical_url, built_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            dataset_id,
            plan.posting_id,
            to_utc_z(replay_at),
            plan.company_id,
            plan.canonical_url,
            to_utc_z(built_at),
        ),
    )
    conn.commit()


def _observe_live(
    conn: sqlite3.Connection,
    cfg: Config,
    plan: CasePlan,
    *,
    dataset_id: str,
    now: datetime,
    use_tool_cache: bool,
    collection_status_csv: str | Path | None,
) -> tuple[dict[tuple[str, str], _Observation], CaseState]:
    """Run System A live, once, and return everything it executed.

    This is `rli.eval.system_a.run_system_a`'s body with a recording runner
    substituted; it is written out rather than called because the point of
    the build is the RECORD, and `run_system_a` returns only a `RunResult`.
    The trace it writes is a real live System A trace all the same.
    """
    with Run(
        conn,
        cfg,
        input_url=plan.canonical_url,
        system="A",
        config_hash=f"cfg:{config_hash(cfg)}|replay_build:{dataset_id}",
        started_at=now,
    ) as run:
        with _recording_runner(
            conn,
            cfg,
            run,
            now,
            use_tool_cache=use_tool_cache,
            collection_status_csv=collection_status_csv,
        ) as probes:
            case = build_case_state(
                conn,
                cfg,
                url=plan.canonical_url,
                now=now,
                probes=probes,
            )
            run.set_posting_id(case.posting_id)
            case_file = case.case_file()
            if case_file is not None:
                extend_case_state(
                    case,
                    eligible_probes(
                        probes.ctx, case_file, unpopulated_inputs=set(ALL_DYNAMIC_INPUTS)
                    ),
                    probes=probes,
                )
            decide_and_finish(
                probes,
                evidence=case.evidence,
                inputs=case.inputs,
                features=case.features,
                failures=case.failures,
            )
            return dict(probes.observations), case


def _company_events_at(
    conn: sqlite3.Connection,
    cfg: Config,
    case: CaseState,
    replay_at: datetime,
    *,
    collection_status_csv: str | Path | None = None,
) -> _Observation | None:
    """Run `company_events` with `as_of=T`, under the args replay will build.

    See the module docstring: this probe's `args_hash` depends on the run
    clock, so a record stored under the build clock could never be found at
    replay. Arguments are produced by `rli.probes.registry.build_args` with a
    context whose clock is `T` — the same call the replayed controller makes
    — rather than constructed here, so the two can never diverge.

    `collection_status_csv` goes on the CONTEXT, so the stored record
    reflects the collection state the build pinned rather than whatever the
    working checkout holds. It reaches the probe without touching `args`,
    which is exactly what the `args_hash` above requires: the hash must
    identify the QUESTION (`company_id`, `as_of`) and stay identical on the
    machine that later replays this record.
    """
    case_file = case.case_file()
    if case_file is None:
        return None

    ctx = ProbeContext(
        conn=conn,
        config=cfg,
        # This probe makes no network call at all (spec.md §4); the factory
        # exists to satisfy the dataclass and must never be reached.
        net_client_factory=_no_network,
        now=lambda: replay_at,
        collection_status_csv=collection_status_csv,
    )
    args = build_args(CompanyEventsProbe, case_file, ctx)
    result = CompanyEventsProbe().run(args, ctx)  # type: ignore[arg-type]
    return _Observation(
        probe_name=CompanyEventsProbe.name,
        args_hash=hash_args(CompanyEventsProbe.name, **args.model_dump(mode="json")),
        # The observation is a read of the LOCAL event store made now; its
        # claims carry each event's own `available_at` (spec.md §3/§4).
        observed_at=replay_at,
        result=result,
    )


def _team_signal_at(
    conn: sqlite3.Connection,
    cfg: Config,
    case: CaseState,
    replay_at: datetime,
    *,
    company_id: str,
) -> _Observation | None:
    """Run `team_signal` with `as_of=T`, inside the point-in-time corpus.

    Sibling of `_company_events_at`, and for the same first reason: this
    probe's `args` carry an `as_of`, so its `args_hash` is a function of `T`
    and a record stored under the build clock could never be found at replay.
    Arguments come from `rli.probes.registry.build_args` with a context whose
    clock is `T` — the same call the replayed controller makes — rather than
    from a hand-built `TeamSignalArgs`, so the builder and the controller can
    never disagree about the question, and therefore never about the hash.

    Unlike `company_events`, this probe reads the CAPTURE CORPUS (`postings`
    lifecycle columns and `board_snapshots`), so it runs inside
    `rli.replay.pit.point_in_time`. `rli.history.features.team_activity` is
    `as_of`-bounded on its own — the context is the belt to those braces, not
    the only defence — and the module docstring's judgment call explains why
    both are kept and what the per-`T` context costs.

    The context is entered only when one is not already installed
    (`installed_shadows` / `nullcontext`, the composition `case_state_at`
    uses), because `point_in_time` refuses to nest: this helper is then safe
    if a future caller ever wraps a whole build in a context. The probe runs
    INSIDE it and the returned `_Observation` is handed back for the caller
    to `store.save` AFTER it has exited, so no write to
    `replay_probe_results` happens while the shadows are installed and the
    context can never be left holding a half-open transaction.

    `company_id` is the CASE's company, used to scope the lifecycle
    re-derivation (`rli.replay.pit`'s scoping judgment call). The case file's
    own `company_id` is included alongside it: the two are the same company
    for every posting a build plans, and taking the union means a
    disagreement would cost one extra re-derivation rather than silently
    handing the probe a company whose lifecycle was left NULL — which would
    read as "no postings" and could manufacture a negative hiring signal.
    """
    case_file = case.case_file()
    if case_file is None:
        return None

    company_ids = sorted({company_id, case_file.company_id})
    pit = (
        nullcontext(conn)
        if installed_shadows(conn)
        else point_in_time(conn, replay_at, company_ids=company_ids)
    )
    with pit:
        ctx = ProbeContext(
            conn=conn,
            config=cfg,
            # `[allowlists].team_signal` is empty: this probe makes no network
            # call at all, so the factory exists to satisfy the dataclass and
            # must never be reached.
            net_client_factory=_no_network,
            now=lambda: replay_at,
        )
        args = build_args(TeamSignalProbe, case_file, ctx)
        result = TeamSignalProbe().run(args, ctx)  # type: ignore[arg-type]
        args_hash_value = hash_args(TeamSignalProbe.name, **args.model_dump(mode="json"))

    return _Observation(
        probe_name=TeamSignalProbe.name,
        args_hash=args_hash_value,
        # The observation is a read of the local capture corpus AS RESTRICTED
        # TO `T`; its claims carry the capture timestamps they were derived
        # from (`rli.probes.team_signal`'s availability rule).
        observed_at=replay_at,
        result=result,
    )


def _no_network(probe_name: str) -> None:  # pragma: no cover - defensive
    raise AssertionError(
        f"{probe_name!r} attempted a network call while building a per-T replay "
        "dataset record. Every probe collected per T reads only local data — "
        "`company_events` the pre-collected event store (spec.md §4), `team_signal` "
        "the board-capture history — and their `[allowlists]` entries are empty, so "
        "a reached factory is a bug rather than a policy question"
    )


def build_dataset(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    dataset_id: str,
    split: str,
    split_kind: Literal["temporal", "company"] = "company",
    grid_step_days: int = DEFAULT_GRID_STEP_DAYS,
    limit_postings: int | None = None,
    now: datetime | None = None,
    cutoff: datetime | None = None,
    validation_cutoff: datetime | None = None,
    seed: int = DEFAULT_SEED,
    use_tool_cache: bool = True,
    collection_status_csv: str | Path | None = None,
    notes: str | None = None,
) -> BuildSummary:
    """Build (or rebuild) the replay dataset `dataset_id` (PLAN.md M4).

    Makes LIVE network calls — one System A run per selected posting — and is
    the only function in `rli.replay` that does. Everything downstream
    (`rli.replay.run`, `rli.eval.baseline`) reads the record this writes and
    reaches the network never.

    Rebuilding the same `dataset_id` re-observes the world and replaces every
    record in place (`INSERT OR REPLACE` throughout, keyed by the identity
    the schema already pins). A rebuild is therefore idempotent in shape and
    fresh in content — which is the right behaviour for a record whose whole
    purpose is to be the latest live truth.

    Raises `rli.eval.baseline.HoldoutSplitRequestedError` for
    `split="test"` (PLAN.md M4: "keep final holdouts untouched until M6").
    """
    if split == "test":
        raise HoldoutSplitRequestedError(
            "refusing to build a replay dataset over the 'test' split: PLAN.md M4 "
            "requires final holdouts to stay untouched until M6, and building the "
            "dataset is what spends them (it makes the live probe calls)."
        )
    if split not in ("dev", "validation"):
        raise ValueError(f"unknown split {split!r}; expected 'dev' or 'validation'")

    moment = ensure_aware(now, "now") if now is not None else now_utc()
    splits = load_split_map(
        conn,
        cutoff=cutoff if cutoff is not None else moment,
        validation_cutoff=validation_cutoff,
        seed=seed,
        split_kind=split_kind,
    )
    plans = plan_cases(
        conn,
        splits=splits,
        split=split,
        now=moment,
        grid_step_days=grid_step_days,
        limit_postings=limit_postings,
        seed=seed,
    )

    # The header first: `replay_cases.dataset_id` is a foreign key into it.
    # A rebuild also clears the previous cases here, so a dataset rebuilt with
    # a coarser grid (or fewer postings) does not keep the old grid's cases
    # alongside the new ones — every case row would still be readable, and
    # `rli.replay.run` would replay a mixture of two datasets under one id.
    _upsert_dataset(
        conn,
        dataset_id=dataset_id,
        created_at=moment,
        split_kind=split_kind,
        split_name=split,
        grid_step_days=grid_step_days,
        postings=0,
        companies=0,
        cases=0,
        notes=notes,
    )
    conn.execute("DELETE FROM replay_cases WHERE dataset_id = ?", (dataset_id,))
    conn.execute("DELETE FROM replay_probe_results WHERE dataset_id = ?", (dataset_id,))
    conn.commit()

    store = ReplayProbeStore()
    cases = 0
    probe_records = 0
    archive_records = 0
    cases_with_archive = 0
    live_executions = 0
    failures: list[str] = []
    built_postings: list[CasePlan] = []

    for plan in plans:
        try:
            observations, case = _observe_live(
                conn,
                cfg,
                plan,
                dataset_id=dataset_id,
                now=moment,
                use_tool_cache=use_tool_cache,
                collection_status_csv=collection_status_csv,
            )
        except Exception as exc:  # noqa: BLE001 - one bad posting must not stop a build
            # A probe that raises is a contract violation (`rli.probes.base`),
            # and one posting's bug must not cost the whole dataset. It is
            # counted and named, never swallowed silently.
            failures.append(f"{plan.posting_id}: {type(exc).__name__}: {exc}")
            continue

        live_executions += len(observations)
        built_postings.append(plan)

        for replay_at in plan.times:
            _insert_case(
                conn,
                dataset_id=dataset_id,
                plan=plan,
                replay_at=replay_at,
                built_at=moment,
            )
            cases += 1

            for observation in observations.values():
                if observation.probe_name in _PER_T_PROBES:
                    # Re-run per T below, under the args_hash replay will ask
                    # for. The build-clock execution System A already made is
                    # deliberately NOT stored: its hash carries `as_of=<build
                    # instant>`, so no replay could ever select it, and keeping
                    # it would put two records for one probe in the dataset —
                    # one of them unreachable — which is exactly the kind of
                    # thing that makes a "missing probe result" bug hard to see.
                    continue
                store.save(
                    conn,
                    dataset_id=dataset_id,
                    posting_id=plan.posting_id,
                    replay_at=replay_at,
                    probe_name=observation.probe_name,
                    args_hash=observation.args_hash,
                    observed_at=observation.observed_at,
                    result=observation.result,
                    created_at=moment,
                )
                probe_records += 1

            events = _company_events_at(
                conn, cfg, case, replay_at, collection_status_csv=collection_status_csv
            )
            if events is not None:
                store.save(
                    conn,
                    dataset_id=dataset_id,
                    posting_id=plan.posting_id,
                    replay_at=replay_at,
                    probe_name=events.probe_name,
                    args_hash=events.args_hash,
                    observed_at=events.observed_at,
                    result=events.result,
                    created_at=moment,
                )
                probe_records += 1

            # `_team_signal_at` opens (and closes) its own point-in-time
            # context; the save is deliberately out here, after it has exited,
            # so `replay_probe_results` is never written while the temp
            # shadows are installed. See `rli.replay.pit`'s "The connection
            # must never hold a read snapshot across the `yield`".
            team = _team_signal_at(conn, cfg, case, replay_at, company_id=plan.company_id)
            if team is not None:
                store.save(
                    conn,
                    dataset_id=dataset_id,
                    posting_id=plan.posting_id,
                    replay_at=replay_at,
                    probe_name=team.probe_name,
                    args_hash=team.args_hash,
                    observed_at=team.observed_at,
                    result=team.result,
                    created_at=moment,
                )
                probe_records += 1

            claims = archive_state_claims(conn, plan.posting_id, replay_at)
            store.save(
                conn,
                dataset_id=dataset_id,
                posting_id=plan.posting_id,
                replay_at=replay_at,
                probe_name=ARCHIVE_BOARD_STATE_PROBE,
                args_hash=archive_state_args_hash(plan.posting_id, replay_at),
                # The archive state is a re-reading of captures, so the
                # honest "when was this observed" is `T` itself: at `T`, this
                # is what the capture record said. The CLAIMS inside carry
                # the capture times, which is what the gate reads.
                observed_at=replay_at,
                result=ProbeResult(
                    ok=True,
                    data={
                        "posting_id": plan.posting_id,
                        "replay_at": to_utc_z(replay_at),
                        "observable": bool(claims),
                        "evidence": claims,
                    },
                ),
                created_at=moment,
            )
            archive_records += 1
            if claims:
                cases_with_archive += 1

    companies = len({plan.company_id for plan in built_postings})
    _upsert_dataset(
        conn,
        dataset_id=dataset_id,
        created_at=moment,
        split_kind=split_kind,
        split_name=split,
        grid_step_days=grid_step_days,
        postings=len(built_postings),
        companies=companies,
        cases=cases,
        notes=notes,
    )

    return BuildSummary(
        dataset_id=dataset_id,
        split_kind=split_kind,
        split_name=split,
        grid_step_days=grid_step_days,
        created_at=to_utc_z(moment),
        postings=len(built_postings),
        companies=companies,
        cases=cases,
        probe_records=probe_records,
        archive_state_records=archive_records,
        cases_with_archive_state=cases_with_archive,
        live_probe_executions=live_executions,
        postings_failed=len(failures),
        failures=tuple(failures),
    )


def dataset_case_rows(conn: sqlite3.Connection, dataset_id: str) -> list[sqlite3.Row]:
    """Every `replay_cases` row of `dataset_id`, ordered `(replay_at, posting_id)`.

    Ordered by `T` first because `rli.replay.run` opens one point-in-time
    corpus per `T` (`rli.replay.pit`), so this order lets it walk the dataset
    without reopening a context it has already left.
    """
    return conn.execute(
        """
        SELECT dataset_id, posting_id, replay_at, company_id, canonical_url, built_at
        FROM replay_cases
        WHERE dataset_id = ?
        ORDER BY replay_at, posting_id
        """,
        (dataset_id,),
    ).fetchall()


def dataset_companies(conn: sqlite3.Connection, dataset_id: str) -> list[str]:
    """Distinct `company_id`s in `dataset_id`, for `rli.replay.pit`'s scoping."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT company_id FROM replay_cases WHERE dataset_id = ? ORDER BY company_id",
            (dataset_id,),
        )
    ]
