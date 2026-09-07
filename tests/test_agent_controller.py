"""The deterministic controller: filter, rank, budget, RUN/STOP (spec.md §2/§4).

Almost nothing here needs an LLM. `rli.agent.controller.decide` takes an
ALREADY-PARSED `InvestigatorOutput`, which is the point: everything spec.md
§2 puts on the deterministic side — "permissions, budgets, hard stops, probe
ranking" — is testable without a model in the loop. The one place a model
appears is the end-to-end check that a `ScriptedClient` reply survives the
round trip from `rli.llm.client` into a decision.

The four hard stops of spec.md §4 each get a test, and so does their
PRECEDENCE: a stop that is true regardless of what the model said must not
be overridable by what the model said.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from test_eval_helpers import COMPANY, add_capture, add_posting, job

from rli.agent.controller import (
    Budget,
    ControllerDecision,
    decide,
    probe_cost_usd,
    rank_candidates,
    score_candidate,
)
from rli.agent.investigator import ExecutedProbe, InvestigatorOutput, ProbeCandidate
from rli.config import Config
from rli.eval.case import CaseState
from rli.llm.client import Prompt, ScriptedClient
from rli.llm.prompts import build_investigator_prompt
from rli.models.policy_inputs import PolicyInputs
from rli.net.client import hash_args
from rli.probes.base import ProbeContext
from rli.probes.company_events import CompanyEventsProbe
from rli.probes.registry import build_args
from rli.probes.repost_history import RepostHistoryProbe
from rli.probes.requirements_drift import RequirementsDriftProbe
from rli.probes.team_signal import TeamSignalProbe

NOW = datetime(2026, 9, 7, tzinfo=UTC)
POSTING_ID = "greenhouse:acme:9001"

# Roomy enough that no test hits a cap it did not ask for.
WIDE = Budget(max_cost_usd=100.0, max_latency_s=1000.0, max_dynamic_steps=4)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx(ctx_factory: Callable[..., ProbeContext]) -> ProbeContext:
    """Fixed clock: `registry.build_args` reads `ctx.now()` for `company_events`."""
    return ctx_factory(now=lambda: NOW)


def _with_config(ctx: ProbeContext, cfg: Config) -> ProbeContext:
    """`ProbeContext` is frozen; the eligibility gates read `ctx.config`."""
    return dataclasses.replace(ctx, config=cfg)


def _case(**overrides: object) -> CaseState:
    defaults: dict[str, object] = dict(
        input_url="https://boards.greenhouse.io/acme/jobs/9001",
        canonical_url="https://boards.greenhouse.io/acme/jobs/9001",
        posting_id=POSTING_ID,
        company_id=COMPANY,
        inputs=PolicyInputs(posting_state="open"),
    )
    defaults.update(overrides)
    return CaseState(**defaults)  # type: ignore[arg-type]


def _seed_history(conn: sqlite3.Connection) -> None:
    """50 days of usable board history (>= [thresholds].min_history_days)."""
    add_posting(
        conn,
        job_id="9001",
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    for offset in (60, 45, 30, 10):
        add_capture(conn, NOW - timedelta(days=offset), [job("9001")], company_id=COMPANY)


def _output(*candidates: ProbeCandidate, stop: bool = False) -> InvestigatorOutput:
    return InvestigatorOutput(candidates=list(candidates), stop=stop)


def _candidate(probe: str, **args: object) -> ProbeCandidate:
    return ProbeCandidate(probe=probe, args=dict(args), argument="because")


def _decide(
    output: InvestigatorOutput | None,
    *,
    case: CaseState,
    cfg: Config,
    ctx: ProbeContext,
    could_change: set[str] | None = None,
    executed: tuple[ExecutedProbe, ...] = (),
    budget: Budget = WIDE,
    investigator_error: str | None = None,
) -> ControllerDecision:
    return decide(
        output=output,
        case=case,
        cfg=cfg,
        ctx=ctx,
        now=NOW,
        could_change={"repost_pattern"} if could_change is None else could_change,
        executed=executed,
        budget=budget,
        investigator_error=investigator_error,
    )


def _reasons(decision: ControllerDecision) -> dict[str, str]:
    """`{probe name: reject reason}` for the rejected candidates."""
    return {probe: reason for probe, reason, _detail in decision.rejected}


def _equal_cost_config(cfg: Config) -> Config:
    """Flatten `[probe_costs]` so ranking differences come only from `value`."""
    return cfg.model_copy(
        update={
            "probe_costs": cfg.probe_costs.model_copy(
                update={
                    "low": 3,
                    "medium": 3,
                    "high": 3,
                    "latency_low_s": 5.0,
                    "latency_medium_s": 5.0,
                    "latency_high_s": 5.0,
                }
            )
        }
    )


# ---------------------------------------------------------------------------
# spec.md §4 hard stops, and their precedence
# ---------------------------------------------------------------------------


def test_stops_when_no_unresolved_question_could_change_the_action(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    decision = _decide(
        _output(_candidate("repost_history", posting_id=POSTING_ID)),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
        could_change=set(),
    )
    assert decision.decision == "stop"
    assert decision.reason == "no_unresolved_question"
    # Nothing was even looked at: the stop is true whatever the model proposed.
    assert decision.considered == ()


def test_stops_at_the_step_cap(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    spent = Budget(
        max_cost_usd=100.0, max_latency_s=1000.0, max_dynamic_steps=2, dynamic_steps=2
    )
    decision = _decide(
        _output(_candidate("repost_history", posting_id=POSTING_ID)),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
        budget=spent,
    )
    assert (decision.decision, decision.reason) == ("stop", "step_cap")


def test_stops_with_investigator_error_when_the_output_is_none(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    decision = _decide(
        None, case=_case(), cfg=cfg, ctx=ctx, investigator_error="LLMSchemaError: bad json"
    )
    assert (decision.decision, decision.reason) == ("stop", "investigator_error")
    # The detail rides in `rejected` as a pseudo-rejection; `reason` stays a
    # bare token because rli.agent.loop concatenates it into decision_type.
    assert decision.rejected == (("", "investigator_error", "LLMSchemaError: bad json"),)


def test_stops_when_the_investigator_asks_to_stop(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    decision = _decide(
        _output(_candidate("repost_history", posting_id=POSTING_ID), stop=True),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
    )
    assert (decision.decision, decision.reason) == ("stop", "investigator_stop")


def test_deterministic_stops_outrank_the_models_opinion(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """spec.md §2: never rely on the LLM alone for stopping — in both directions."""
    _seed_history(conn)
    keep_going = _output(_candidate("repost_history", posting_id=POSTING_ID), stop=False)

    # A model that wants to continue cannot talk past an exhausted step cap...
    capped = Budget(max_cost_usd=100.0, max_latency_s=1000.0, max_dynamic_steps=0)
    assert _decide(keep_going, case=_case(), cfg=cfg, ctx=ctx, budget=capped).reason == "step_cap"

    # ... nor past an empty could_change set.
    assert (
        _decide(keep_going, case=_case(), cfg=cfg, ctx=ctx, could_change=set()).reason
        == "no_unresolved_question"
    )

    # And a model that wants to stop does not get to preempt the caps either:
    # the cap is reported, because it is the fact.
    wants_stop = _output(stop=True)
    assert _decide(wants_stop, case=_case(), cfg=cfg, ctx=ctx, budget=capped).reason == "step_cap"


def test_stops_at_the_cost_cap_and_names_the_best_candidate(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    cost = probe_cost_usd(RepostHistoryProbe, cfg)
    tight = Budget(
        max_cost_usd=cost,
        max_latency_s=1000.0,
        max_dynamic_steps=4,
        spent_cost_usd=cost / 2,
    )
    decision = _decide(
        _output(_candidate("repost_history", posting_id=POSTING_ID)),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
        budget=tight,
    )
    assert (decision.decision, decision.reason) == ("stop", "cost_cap")
    assert _reasons(decision)["repost_history"] == "budget_cost"
    # The ranking is still reported — the trace must show what was refused.
    assert decision.ranking[0][0] == "repost_history"


def test_stops_at_the_latency_cap(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    tight = Budget(max_cost_usd=100.0, max_latency_s=0.5, max_dynamic_steps=4)
    decision = _decide(
        _output(_candidate("repost_history", posting_id=POSTING_ID)),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
        budget=tight,
    )
    assert (decision.decision, decision.reason) == ("stop", "latency_cap")
    assert _reasons(decision)["repost_history"] == "budget_latency"


def test_only_the_best_candidate_is_budget_checked(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """A cheaper survivor is NOT substituted — see the controller's judgment call."""
    _seed_history(conn)
    equal = _equal_cost_config(cfg)
    could_change = {"repost_pattern", "material_negative_event", "freeze_or_pause"}
    # With flat costs, `company_events` (value 2) outranks `repost_history`
    # (value 1) — and both cost the same, so both are equally unaffordable.
    best_cost = probe_cost_usd(CompanyEventsProbe, equal)
    tight = Budget(max_cost_usd=best_cost / 2, max_latency_s=1000.0, max_dynamic_steps=4)

    decision = _decide(
        _output(
            _candidate("repost_history", posting_id=POSTING_ID),
            _candidate("company_events", company_id=COMPANY, as_of=NOW),
        ),
        case=_case(),
        cfg=equal,
        ctx=_with_config(ctx, equal),
        could_change=could_change,
        budget=tight,
    )
    assert decision.reason == "cost_cap"
    assert [name for name, _score in decision.ranking][0] == "company_events"
    # `repost_history` survived filtering and is in the ranking; it was simply
    # never offered as a fallback.
    assert "repost_history" in [name for name, _score in decision.ranking]
    assert decision.chosen_probe is None


def test_stops_with_no_eligible_candidate_when_nothing_survives(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    decision = _decide(_output(), case=_case(), cfg=cfg, ctx=ctx)
    assert (decision.decision, decision.reason) == ("stop", "no_eligible_candidate")
    assert decision.ranking == ()


# ---------------------------------------------------------------------------
# spec.md §4 step 4: filtering invalid / ineligible candidates
# ---------------------------------------------------------------------------


def test_rejects_an_unknown_probe_name(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    decision = _decide(
        _output(
            _candidate("linkedin_scrape", posting_id=POSTING_ID),
            _candidate("repost_history", posting_id=POSTING_ID),
        ),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
    )
    assert _reasons(decision)["linkedin_scrape"] == "unknown_probe"
    assert decision.decision == "run"
    assert decision.chosen_probe == "repost_history"


def test_rejects_invalid_arguments_rather_than_repairing_them(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """spec.md §6 counts "invalid arguments"; silently repairing would zero it."""
    _seed_history(conn)
    decision = _decide(
        _output(
            _candidate("repost_history", posting_id=9001),  # int, not str
            _candidate("company_events", company_id=COMPANY),  # missing `as_of`
        ),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
        could_change={"repost_pattern", "material_negative_event"},
    )
    reasons = _reasons(decision)
    assert reasons["repost_history"] == "invalid_args"
    assert reasons["company_events"] == "invalid_args"
    assert (decision.decision, decision.reason) == ("stop", "no_eligible_candidate")
    # The rejection carries the pydantic location, so the metric is auditable.
    details = {probe: detail for probe, _reason, detail in decision.rejected}
    assert "posting_id" in details["repost_history"]
    assert "as_of" in details["company_events"]


def test_the_executed_arguments_are_canonical_not_the_models(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """A valid-but-wrong proposal still runs `registry.build_args`' values."""
    _seed_history(conn)
    decision = _decide(
        # A well-typed `posting_id` that is not this case's posting.
        _output(_candidate("repost_history", posting_id="greenhouse:evil:1")),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
    )
    canonical = build_args(RepostHistoryProbe, _case().case_file(), ctx)
    expected = hash_args("repost_history", **canonical.model_dump(mode="json"))

    assert decision.decision == "run"
    assert decision.chosen_args_hash == expected
    assert decision.chosen_args_hash != hash_args(
        "repost_history", posting_id="greenhouse:evil:1"
    )


def test_rejects_a_repeat_of_an_already_executed_probe_and_args(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """spec.md §4 hard stop: "the same probe+arguments would repeat"."""
    _seed_history(conn)
    canonical = build_args(RepostHistoryProbe, _case().case_file(), ctx)
    already = ExecutedProbe(
        probe="repost_history",
        args_hash=hash_args("repost_history", **canonical.model_dump(mode="json")),
        args=canonical.model_dump(mode="json"),
        ok=True,
    )
    decision = _decide(
        _output(_candidate("repost_history", posting_id=POSTING_ID)),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
        executed=(already,),
    )
    assert _reasons(decision)["repost_history"] == "duplicate"
    assert decision.reason == "no_eligible_candidate"


def test_rejects_the_same_probe_proposed_twice_in_one_output(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """The canonical args are identical, so the second naming is the same call."""
    _seed_history(conn)
    decision = _decide(
        _output(
            _candidate("repost_history", posting_id=POSTING_ID),
            _candidate("repost_history", posting_id=POSTING_ID),
        ),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
    )
    assert decision.decision == "run"
    assert decision.considered == ("repost_history", "repost_history")
    assert _reasons(decision)["repost_history"] == "duplicate"
    assert len(decision.ranking) == 1


def test_rejects_a_history_gated_probe_without_usable_history(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """spec.md §4: "history probes are ineligible without usable history"."""
    add_posting(conn, job_id="9001")  # a posting row, but no board captures
    decision = _decide(
        _output(
            _candidate("repost_history", posting_id=POSTING_ID),
            _candidate("requirements_drift", posting_id=POSTING_ID),
        ),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
    )
    assert _reasons(decision) == {
        "repost_history": "ineligible",
        "requirements_drift": "ineligible",
    }
    assert decision.reason == "no_eligible_candidate"


def test_rejects_everything_when_the_identity_is_unresolved(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """No `posting_id`/`company_id` means `build_args` cannot even be called."""
    _seed_history(conn)
    decision = _decide(
        _output(_candidate("repost_history", posting_id=POSTING_ID)),
        case=_case(posting_id=None, company_id=None),
        cfg=cfg,
        ctx=ctx,
    )
    assert _reasons(decision)["repost_history"] == "ineligible"
    details = {probe: detail for probe, _reason, detail in decision.rejected}
    assert details["repost_history"] == "identity_unresolved"


def test_rejects_candidates_beyond_the_candidate_limit(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    narrow = cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"max_candidates": 1})})
    decision = _decide(
        _output(
            _candidate("repost_history", posting_id=POSTING_ID),
            _candidate("requirements_drift", posting_id=POSTING_ID),
        ),
        case=_case(),
        cfg=narrow,
        ctx=_with_config(ctx, narrow),
    )
    assert decision.considered == ("repost_history",)
    assert _reasons(decision)["requirements_drift"] == "over_candidate_limit"
    assert decision.chosen_probe == "repost_history"


# ---------------------------------------------------------------------------
# team_signal: the licence flag AND the reachability half, via one call
# ---------------------------------------------------------------------------


def test_team_signal_is_ineligible_while_the_licence_flag_is_false(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    assert cfg.team_signal.enabled is False
    decision = _decide(
        _output(_candidate("team_signal", posting_id=POSTING_ID, company_id=COMPANY)),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
        could_change={"corroborating_hiring_signal"},
    )
    assert _reasons(decision)["team_signal"] == "ineligible"
    assert decision.reason == "no_eligible_candidate"


def test_team_signal_runs_only_when_licensed_and_the_branch_is_reachable(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """`corroborating_hiring_signal in could_change` IS the reachability test.

    See `rli.agent.controller`'s module docstring: `could_change_action` only
    returns inputs that are unpopulated AND can still move the action, and the
    only branch reading this input is spec.md §5's repost/long-lived `skip`.
    """
    _seed_history(conn)
    licensed = cfg.model_copy(
        update={"team_signal": cfg.team_signal.model_copy(update={"enabled": True})}
    )
    licensed_ctx = _with_config(ctx, licensed)
    candidate = _candidate("team_signal", posting_id=POSTING_ID, company_id=COMPANY)

    runs = _decide(
        _output(candidate),
        case=_case(),
        cfg=licensed,
        ctx=licensed_ctx,
        could_change={"corroborating_hiring_signal"},
    )
    assert (runs.decision, runs.chosen_probe) == ("run", "team_signal")

    # Licensed, but the repost/long-lived branch is unreachable, so the input
    # is absent from `could_change` — the same single call refuses it.
    unreachable = _decide(
        _output(candidate),
        case=_case(),
        cfg=licensed,
        ctx=licensed_ctx,
        could_change={"repost_pattern"},
    )
    assert _reasons(unreachable)["team_signal"] == "ineligible"


# ---------------------------------------------------------------------------
# spec.md §4 step 5: deterministic cost-aware ranking
# ---------------------------------------------------------------------------


def test_ranking_prefers_the_cheaper_probe_at_equal_value(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    decision = _decide(
        _output(
            _candidate("requirements_drift", posting_id=POSTING_ID),  # medium
            _candidate("repost_history", posting_id=POSTING_ID),  # low
        ),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
    )
    assert decision.chosen_probe == "repost_history"
    assert [name for name, _score in decision.ranking] == [
        "repost_history",
        "requirements_drift",
    ]
    # Proposal order does not decide anything: reversing it changes nothing.
    reversed_order = _decide(
        _output(
            _candidate("repost_history", posting_id=POSTING_ID),
            _candidate("requirements_drift", posting_id=POSTING_ID),
        ),
        case=_case(),
        cfg=cfg,
        ctx=ctx,
    )
    assert reversed_order.ranking == decision.ranking


def test_ranking_prefers_the_higher_value_probe_at_equal_cost(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    equal = _equal_cost_config(cfg)
    decision = _decide(
        _output(
            _candidate("repost_history", posting_id=POSTING_ID),  # populates 1
            _candidate("company_events", company_id=COMPANY, as_of=NOW),  # populates 2
        ),
        case=_case(),
        cfg=equal,
        ctx=_with_config(ctx, equal),
        could_change={"repost_pattern", "material_negative_event", "freeze_or_pause"},
    )
    assert decision.chosen_probe == "company_events"
    assert decision.ranking[0][1] == pytest.approx(2 * decision.ranking[1][1])


def test_ranking_ties_break_on_the_probe_name(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    """Without this, an equal-score tie would be broken by the LLM's ordering."""
    _seed_history(conn)
    equal = _equal_cost_config(cfg)
    assert probe_cost_usd(RepostHistoryProbe, equal) == probe_cost_usd(
        RequirementsDriftProbe, equal
    )
    decision = _decide(
        _output(
            _candidate("requirements_drift", posting_id=POSTING_ID),
            _candidate("repost_history", posting_id=POSTING_ID),
        ),
        case=_case(),
        cfg=equal,
        ctx=_with_config(ctx, equal),
    )
    assert [name for name, _score in decision.ranking] == [
        "repost_history",
        "requirements_drift",
    ]
    assert decision.chosen_probe == "repost_history"


def test_decide_is_deterministic(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    output = _output(
        _candidate("requirements_drift", posting_id=POSTING_ID),
        _candidate("repost_history", posting_id=POSTING_ID),
        _candidate("nope"),
    )
    first = _decide(output, case=_case(), cfg=cfg, ctx=ctx)
    second = _decide(output, case=_case(), cfg=cfg, ctx=ctx)
    assert first == second


def test_rank_candidates_and_score_candidate_agree_with_decide(cfg: Config) -> None:
    could_change = {"repost_pattern", "material_negative_event"}
    assert score_candidate(RepostHistoryProbe, cfg, could_change) == pytest.approx(
        1 / probe_cost_usd(RepostHistoryProbe, cfg)
    )
    # A probe that populates nothing relevant scores zero, not a small number.
    assert score_candidate(TeamSignalProbe, cfg, could_change) == 0.0
    assert rank_candidates([]) == []


# ---------------------------------------------------------------------------
# The cost model and the ledger
# ---------------------------------------------------------------------------


def test_probe_cost_usd_is_points_plus_latency_plus_failure(cfg: Config) -> None:
    agent = cfg.agent
    expected = (
        cfg.probe_costs.low * agent.probe_cost_usd_per_point
        + cfg.probe_costs.latency_low_s * agent.latency_cost_usd_per_s
        + agent.failure_rate_placeholder * agent.failure_cost_usd
    )
    assert probe_cost_usd(RepostHistoryProbe, cfg) == pytest.approx(expected)
    # Strictly positive, so `value / cost` can never divide by zero.
    assert probe_cost_usd(RepostHistoryProbe, cfg) > 0.0
    assert probe_cost_usd(TeamSignalProbe, cfg) > probe_cost_usd(RepostHistoryProbe, cfg)


def test_budget_is_an_immutable_single_unit_ledger(cfg: Config) -> None:
    budget = Budget.from_config(cfg)
    assert budget.max_cost_usd == cfg.agent.effective_max_cost_usd(cfg)
    assert budget.max_latency_s == cfg.agent.effective_max_latency_s(cfg)
    assert budget.max_dynamic_steps == cfg.agent.effective_max_dynamic_steps(cfg)
    assert budget.remaining_steps() == budget.max_dynamic_steps

    charged = budget.with_probe(RepostHistoryProbe, cfg)
    assert budget.dynamic_steps == 0  # the original is untouched
    assert charged.dynamic_steps == 1
    assert charged.spent_cost_usd == pytest.approx(probe_cost_usd(RepostHistoryProbe, cfg))
    assert charged.remaining_cost_usd() == pytest.approx(
        budget.max_cost_usd - charged.spent_cost_usd
    )
    assert charged.remaining_latency_s() == pytest.approx(
        budget.max_latency_s - cfg.probe_costs.latency_low_s
    )


def test_budget_remaining_steps_never_goes_negative() -> None:
    budget = Budget(
        max_cost_usd=1.0, max_latency_s=1.0, max_dynamic_steps=1, dynamic_steps=3
    )
    assert budget.remaining_steps() == 0


def test_budget_with_llm_charges_real_dollars_and_measured_latency(cfg: Config) -> None:
    client = ScriptedClient([InvestigatorOutput()], cfg=cfg, latency_ms=250.0)
    response = client.complete_structured(_investigator_prompt(), InvestigatorOutput)

    budget = Budget(max_cost_usd=1.0, max_latency_s=10.0, max_dynamic_steps=4)
    charged = budget.with_llm(response)
    assert charged.spent_cost_usd == pytest.approx(response.cost_usd)
    assert charged.spent_latency_s == pytest.approx(0.25)
    # An LLM call is not a dynamic probe step (spec.md §4's cap is on probes).
    assert charged.remaining_steps() == 4


def _investigator_prompt() -> Prompt:
    return build_investigator_prompt(structured_input={"k": "v"}, untrusted=[])


# ---------------------------------------------------------------------------
# One round trip through a scripted LLM, to prove the boundary lines up
# ---------------------------------------------------------------------------


def test_a_scripted_model_reply_flows_into_a_run_decision(
    conn: sqlite3.Connection, cfg: Config, ctx: ProbeContext
) -> None:
    _seed_history(conn)
    scripted = InvestigatorOutput(
        candidates=[_candidate("repost_history", posting_id=POSTING_ID)],
        hypotheses=["evergreen repost"],
    )
    client = ScriptedClient([scripted], cfg=cfg)
    response = client.complete_structured(_investigator_prompt(), InvestigatorOutput)

    decision = _decide(response.parsed, case=_case(), cfg=cfg, ctx=ctx)  # type: ignore[arg-type]
    assert (decision.decision, decision.chosen_probe) == ("run", "repost_history")
    assert client.calls  # the prompt was actually recorded
