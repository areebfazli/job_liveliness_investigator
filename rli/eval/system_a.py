"""System A — the full-probe reference system (spec.md §6; PLAN.md M3).

spec.md §6 defines it in five words: "**A — Full probes:** every available
dynamic probe". A is not a product configuration and is not meant to be one;
it is the REFERENCE both other systems are scored against —

    "action agreement with A (overall and macro-averaged per action class
    ...), medium/high-cost probe count, total cost, latency"

— so its only job is to spend whatever it takes to reach the best-evidenced
decision the frozen policy can produce, and to record what that cost. Every
metric in spec.md §6's agent gate is a comparison against this number.

--------------------------------------------------------------------------
What "every available dynamic probe" means, precisely
--------------------------------------------------------------------------

"Available" is doing real work in that sentence, and A resolves it by
calling the ordinary controller — `rli.probes.registry.eligible_probes` —
with ONE of its three gates deliberately neutralized:

* **the unresolved-question gate is neutralized.** `eligible_probes` takes
  the set of unpopulated policy inputs and keeps a probe only if
  `populates & unpopulated` is non-empty (spec.md §4). A passes the union of
  every dynamic probe's `populates` instead of the case's actual unpopulated
  set, which makes that intersection vacuously true for all four probes. A
  is the full-probe reference: it must run `team_signal` even when nothing
  is left for it to change, and `company_events` even when the case state
  already holds an answer, because otherwise "the full-probe reference
  action" would silently depend on how much the case state happened to know
  already.

  Note this gate no longer changes anything for `company_events`
  specifically. It used to: `rli.eval.case` pre-populated that probe's three
  policy inputs from the local event store, so the real unpopulated set
  would have excluded it on most of the corpus. That pre-population is gone
  (see `rli.eval.case`'s judgment call), and those inputs now stay UNKNOWN
  until the probe itself runs. The neutralized gate is kept regardless — the
  reference system's probe set must not depend on how much any OTHER layer
  happens to have worked out first, which is a statement about A, not about
  one probe.
* **the history gate is NOT neutralized.** spec.md §4: "history probes are
  ineligible without usable history", and "missing history never means flat
  hiring". `repost_history` and `requirements_drift` on a company with two
  days of captures cannot produce a fact; running them anyway would burn
  cost to obtain nothing and would put the reference system's evidence base
  at the mercy of a probe's internal thin-history guard.
* **each probe's own `eligible()` is NOT neutralized.** That is where a
  probe states the gate only it knows: `TeamSignalProbe` reads
  `[team_signal].enabled`, and an unlicensed deployment has no licensed
  source to call. "Available" cannot mean "call an API we do not have."

This is deliberately implemented as a call INTO the registry with a
different argument, not as a re-implementation of its rules here. A
hand-rolled "run everything" loop would drift from the controller the first
time a gate changed, and A's whole value is being the same machinery making
the maximal choice.

--------------------------------------------------------------------------
Judgment calls
--------------------------------------------------------------------------

* **A ignores `[thresholds].max_dynamic_steps`.** That cap is spec.md §4's
  AGENT-LOOP budget ("Start with at most `4` dynamic probe steps"), stated
  in the agent-loop section and belonging to the bounded loop of spec.md §2
  ("One probe → append evidence → repeat — **bounded**"). Applying it to A
  would make the reference action a function of a budget knob: raise the cap
  and A's decisions change, so every agreement number in spec.md §6 would
  move without any system changing. A must be the ceiling the bounded
  systems are measured against, so it is uncapped — and today it cannot
  exceed four dynamic steps anyway, because only four dynamic probes exist.
  (System B, being a baseline rather than a reference, IS capped — see
  `rli.eval.system_b`.)
* **Probes run in the registry's ranked order** (`(cost_value, name)`,
  cheapest first, name-tiebroken — `rli.probes.registry`). A runs all of
  them, so order cannot change WHICH run; it is kept because it makes the
  `run_steps` trace of an A run and of a B/C run directly comparable, and
  because a deterministic order is what makes a replayed run reproducible.
* **An unresolved identity runs nothing.** With no `posting_id` /
  `company_id` (`CaseState.case_file()` is `None`) every dynamic probe's
  arguments are unbuildable, so A records a controller step saying so and
  decides on the always-run evidence alone. The alternative — synthesizing a
  posting id — would produce probe failures that `rli.policy.quality` would
  then have to be told to ignore.
* **A does not stop early on budget.** `[budgets].max_cost_usd` /
  `max_latency_s` are the agent controller's hard stops (spec.md §4's "Hard
  stop if: ... budget/latency/step cap is reached"). A reports its cost so
  those budgets can be set from measurement; enforcing them here would cap
  the very number the caps are supposed to be derived from.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rli.config import Config
from rli.eval.case import build_case_state, extend_case_state
from rli.eval.runner import (
    STEP_PROBE_SKIPPED,
    ReplayHook,
    Run,
    RunResult,
    config_hash,
    decide_and_finish,
    open_system_runner,
    replay_run_shape,
)
from rli.probes.base import Probe
from rli.probes.registry import DYNAMIC_PROBES, eligible_probes

__all__ = ["ALL_DYNAMIC_INPUTS", "run_system_a"]

# The union of every dynamic probe's `populates`. Passing this as
# `unpopulated_inputs` makes `eligible_probes`' first gate vacuous while
# leaving the history gate and each probe's own `eligible()` in force — see
# the module docstring. Computed from the registry so a fifth dynamic probe
# is included automatically.
ALL_DYNAMIC_INPUTS: frozenset[str] = frozenset().union(
    *(probe_cls.populates for probe_cls in DYNAMIC_PROBES.values())  # type: ignore[attr-defined]
)


def run_system_a(
    conn: sqlite3.Connection,
    cfg: Config,
    url: str,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    use_tool_cache: bool = True,
    collection_status_csv: str | Path | None = None,
    replay: ReplayHook | None = None,
) -> RunResult:
    """Run System A against `url` and return its `Decision` plus trace handles.

    Arguments:
        conn: the corpus connection. A writes only `runs`, `run_steps`,
            `evidence` (+ `tool_cache`) — see `rli.eval.runner`.
        cfg: loaded configuration.
        url: the job URL to investigate.
        now: decision clock override; defaults to the current UTC time. Must
            be timezone-aware. Every evidence timestamp, every trace
            timestamp and the policy's `recheck_after_days` are computed from
            it, so passing it makes a run reproducible.
        run_id: `runs.id` override, for deterministic tests/replay.
        sleep: injected into the retry backoff and the per-host rate limiter.
        use_tool_cache: `False` disconnects `tool_cache` entirely, so a
            mocked fetch can never be answered from a stored row.
        collection_status_csv: pins the pre-collected company-event
            collection-status file for this run's `ProbeContext`, i.e. for the
            `company_events` probe (`rli.eval.runner.open_system_runner`).
            `None` reads the project default, which is a file in the working
            checkout — pass an explicit path to keep a test or a replay from
            depending on whatever that file happens to contain. Under
            `replay=`, the hook's own pinned value wins; see
            `open_system_runner`.
        replay: `None` for a live run. A `rli.eval.runner.ReplayHook` puts
            this run in replay mode (spec.md §6): the run is recorded with
            `mode='replay'` and `replay_at=T`, its `config_hash` carries the
            dataset id, and every probe result comes from the cached
            full-probe record instead of the network. A is otherwise
            IDENTICAL — same selection, same policy, same trace — which is
            the property that makes a replayed A comparable to a live A.
    """
    moment, mode, run_config_hash, replay_at = replay_run_shape(
        replay, now, f"cfg:{config_hash(cfg)}"
    )

    with Run(
        conn,
        cfg,
        input_url=url,
        system="A",
        config_hash=run_config_hash,
        started_at=moment,
        run_id=run_id,
        mode=mode,  # type: ignore[arg-type]
        replay_at=replay_at,
    ) as run:
        with open_system_runner(
            conn,
            cfg,
            run,
            moment,
            replay=replay,
            sleep=sleep,
            use_tool_cache=use_tool_cache,
            collection_status_csv=collection_status_csv,
        ) as probes:
            case = build_case_state(
                conn,
                cfg,
                url=url,
                now=moment,
                probes=probes,
            )
            run.set_posting_id(case.posting_id)

            case_file = case.case_file()
            selected: list[type[Probe]] = []
            if case_file is None:
                probes.note(
                    f"{STEP_PROBE_SKIPPED}:identity_unresolved",
                    error=(
                        "no posting_id/company_id could be resolved, so no dynamic probe's "
                        "arguments can be built; deciding on always-run evidence alone"
                    ),
                )
            else:
                selected = eligible_probes(
                    probes.ctx, case_file, unpopulated_inputs=set(ALL_DYNAMIC_INPUTS)
                )
                extend_case_state(case, selected, probes=probes)

            decision = decide_and_finish(
                probes,
                evidence=case.evidence,
                inputs=case.inputs,
                features=case.features,
                failures=case.failures,
            )

    return RunResult(
        run_id=run.id,
        system="A",
        decision=decision,
        probes_run=tuple(probe_cls.name for probe_cls in selected),
    )
