"""System R — the no-LLM, eligibility-gated baseline (spec.md §6 "If rules are equally good").

System C (`rli.agent.loop`) is an LLM investigator in front of a deterministic
controller: the controller computes which policy inputs could still change
the action (`rli.policy.inputs.could_change_action`), which probes are
eligible to answer them (`rli.probes.registry.eligible_probes`), ranks the
survivors by value per cost (`rli.agent.controller.rank_candidates`), checks
budgets and runs the best one. The investigator's only job is to PROPOSE
candidates. An independent review showed that replacing the proposal with
"every eligible probe" reproduces C's decisions on 10,587/10,588 replay cases,
i.e. the controller, not the model, chooses what runs.

System R is that replacement, as a first-class system so the question "does
the LLM add anything?" is answered by a measured comparison rather than by a
one-off script:

* the SAME loop (`rli.agent.loop.run_agent_loop`), the SAME controller and
  hard stops, the SAME eligibility, ranking, budgets, probe execution and
  retry as System C;
* step 3 (the investigator) is `propose_all_eligible`: every probe
  `eligible_probes` allows right now, minus those already executed with the
  same arguments, in the controller's own rank order, with the canonical
  `build_args` arguments. The controller then filters and ranks them exactly
  as it would a model's proposal, so it runs the best-ranked eligible probe;
* the explanation is the deterministic one `decide_and_finish` already
  produces (`rli.policy.explain_stub`), so R makes ZERO model calls and
  writes no `component='model'` step.

It is versioned like System B: `R_VERSION` is the freeze label,
`r_rules_hash` fingerprints the code and knobs that choose R's probes, and
both are recorded in `runs.config_hash` and in a `system_version` step.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import replace as dataclasses_replace
from datetime import datetime
from pathlib import Path

from rli.agent.controller import (
    Budget,
    RankedCandidate,
    probe_cost_usd,
    rank_candidates,
    score_candidate,
)
from rli.agent.controller import decide as controller_decide
from rli.agent.investigator import ExecutedProbe, InvestigatorOutput, ProbeCandidate
from rli.agent.loop import run_agent_loop
from rli.config import Config, load_config
from rli.eval.case import CaseState, build_case_state
from rli.eval.runner import (
    STEP_SYSTEM_VERSION,
    ReplayHook,
    Run,
    RunResult,
    config_hash,
    decide_and_finish,
    open_system_runner,
    replay_run_shape,
)
from rli.net.client import hash_args
from rli.policy.quality import evidence_quality_detail
from rli.probes.base import ProbeContext
from rli.probes.registry import build_args, eligible_probes

__all__ = [
    "R_VERSION",
    "propose_all_eligible",
    "r_config_hash",
    "r_rules_hash",
    "run_system_r",
]

#: Freeze label, mirroring `rli.eval.system_b.B_VERSION`. Bump it when the
#: proposal rule changes in a way that makes a NEW baseline.
R_VERSION = "r1"


def propose_all_eligible(
    *,
    case: CaseState,
    cfg: Config,
    ctx: ProbeContext,
    could_change: set[str],
    executed: Sequence[ExecutedProbe],
) -> InvestigatorOutput:
    """Every eligible, not-yet-executed probe, best-ranked first, with canonical args.

    Eligibility is the controller's own call (`eligible_probes` with
    `unpopulated_inputs=could_change`), the arguments are
    `rli.probes.registry.build_args` (exactly what the controller would run)
    and the order is `rank_candidates` over `score_candidate`, so the
    controller's choice among these candidates is its rank-best eligible
    probe. Already-executed `(probe, args_hash)` pairs are left out so the
    trace carries no `candidate_rejected:duplicate` noise; the controller
    would reject them anyway.
    """
    case_file = case.case_file()
    if case_file is None:  # pragma: no cover - the loop stops before calling us
        return InvestigatorOutput()

    executed_keys = {(item.probe, item.args_hash) for item in executed}
    ranked: list[RankedCandidate] = []
    for probe_cls in eligible_probes(ctx, case_file, unpopulated_inputs=set(could_change)):
        args = build_args(probe_cls, case_file, ctx)
        args_hash = hash_args(probe_cls.name, **args.model_dump(mode="json"))
        if (probe_cls.name, args_hash) in executed_keys:
            continue
        populates: frozenset[str] = getattr(probe_cls, "populates", frozenset())
        ranked.append(
            RankedCandidate(
                probe_cls=probe_cls,
                args=args,
                args_hash=args_hash,
                value=len(populates & could_change),
                cost_usd=probe_cost_usd(probe_cls, cfg),
                score=score_candidate(probe_cls, cfg, could_change),
            )
        )

    candidates = [
        ProbeCandidate(
            probe=item.probe_cls.name,
            args=item.args.model_dump(mode="json"),
            argument=f"System R: eligible, rank {position} of {len(ranked)}",
            expected_inputs=sorted(
                getattr(item.probe_cls, "populates", frozenset()) & could_change
            ),
        )
        for position, item in enumerate(rank_candidates(ranked), start=1)
    ]
    return InvestigatorOutput(candidates=candidates)


def r_rules_hash(cfg: Config | None = None) -> str:
    """A stable id for "this proposal rule, this controller, these knobs".

    Hashes `R_VERSION`, the source of the functions that choose R's probes
    (the proposer, the controller's `decide`, its scoring and ranking, and
    the loop), and the configuration values those read beyond the policy:
    the effective step cap and `[team_signal].enabled`. Like
    `rli.eval.system_b.b_rules_hash`, a reformat changes the hash; a false
    "the rules changed" is cheap, a false "nothing changed" is not.
    """
    if cfg is None:
        cfg = load_config()
    digest = hashlib.blake2b(digest_size=16)
    digest.update(R_VERSION.encode("utf-8"))
    digest.update(b"\x00")
    for function in (
        propose_all_eligible,
        controller_decide,
        score_candidate,
        rank_candidates,
        run_agent_loop,
    ):
        digest.update(inspect.getsource(function).encode("utf-8"))
        digest.update(b"\x00")
    digest.update(
        (
            f"max_dynamic_steps={cfg.agent.effective_max_dynamic_steps(cfg)}|"
            f"team_signal_enabled={cfg.team_signal.enabled}"
        ).encode()
    )
    return digest.hexdigest()


def r_config_hash(cfg: Config) -> str:
    """`runs.config_hash` for a System R run: config fingerprint + rules version."""
    return f"cfg:{config_hash(cfg)}|{R_VERSION}:{r_rules_hash(cfg)}"


def run_system_r(
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
    max_steps: int | None = None,
) -> RunResult:
    """Run System R against `url`. Same signature and contract as `run_system_a`.

    `max_steps` overrides the step cap exactly as it does for
    `rli.agent.loop.run_system_c`. No LLM client is built or called.
    """
    if max_steps is not None and max_steps < 0:
        raise ValueError(f"max_steps must be >= 0, got {max_steps}")

    moment, mode, run_config_hash, replay_at = replay_run_shape(replay, now, r_config_hash(cfg))

    probes_run: list[str] = []
    with Run(
        conn,
        cfg,
        input_url=url,
        system="R",
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
            run.step(
                component="controller",
                decision_type=STEP_SYSTEM_VERSION,
                args_hash=f"{R_VERSION}:{r_rules_hash(cfg)}",
                created_at=moment,
            )

            case = build_case_state(conn, cfg, url=url, now=moment, probes=probes)
            run.set_posting_id(case.posting_id)

            budget = Budget.from_config(cfg)
            if max_steps is not None:
                budget = dataclasses_replace(budget, max_dynamic_steps=max_steps)

            executed: list[ExecutedProbe] = []
            run_agent_loop(
                case=case,
                cfg=cfg,
                probes=probes,
                run=run,
                llm=None,
                moment=moment,
                budget=budget,
                executed=executed,
                probes_run=probes_run,
                propose=propose_all_eligible,
            )

            case.quality = evidence_quality_detail(case.evidence, case.inputs, case.failures, cfg)
            # The deterministic reasons ARE R's explanation: no model call.
            decision = decide_and_finish(
                probes,
                evidence=case.evidence,
                inputs=case.inputs,
                features=case.features,
                failures=case.failures,
            )

    return RunResult(
        run_id=run.id,
        system="R",
        decision=decision,
        probes_run=tuple(probes_run),
    )
