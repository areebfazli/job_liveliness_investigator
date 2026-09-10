"""System C end-to-end: the bounded agent loop (spec.md §4 steps 3-8; rli.agent.loop).

`tests/test_agent_controller.py` proves the controller's rules in isolation,
against a hand-built `CaseState`. This file proves the LOOP: that those rules
are actually reached, in the right order, with a real corpus behind them, and
that every path out of the loop still ends in a valid `Decision` written to
`runs.final_decision`. Nothing here calls a live model — every run is driven
by a `rli.llm.client.ScriptedClient` through `test_agent_helpers.scripted_llm`,
so a scenario is defined by what the investigator says, not by what a model
happens to say today.

Two facts about the fixtures are load-bearing and are stated once here rather
than in ten docstrings:

* **`seed_reposted_history` is the only corpus in which a history-gated
  dynamic probe can run.** With the `tests/test_eval_system_b.py` history,
  `repost_pattern` resolves to `'none'`, which populates the input, which
  removes it from `could_change_action`, which makes `eligible_probes` refuse
  `repost_history` and `requirements_drift` outright. Every test that needs
  the loop to actually execute a probe uses the reposted corpus; see
  `test_agent_helpers.seed_reposted_history` for how it keeps the question
  open.
* **`company_events` is the probe that never resolves its own question here.**
  The suite points every run at a non-existent collection-status file, so the
  probe runs, reports `collected=False`, and leaves `material_negative_event`
  / `freeze_or_pause` UNKNOWN. That is what lets a multi-step scenario keep
  `could_change_action` non-empty and reach a step or cost cap instead of
  stopping with `no_unresolved_question`.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx
from test_agent_helpers import (
    MODEL_ID,
    NO_COLLECTION_STATUS,
    NO_JSONLD_PAGE,
    cited_explanation,
    decision_fingerprint,
    gh_job,
    job_url,
    mock_greenhouse,
    mock_greenhouse_missing,
    propose,
    run_steps,
    scripted_llm,
    seed_history,
    seed_reposted_history,
    steps_matching,
    template_calls,
)
from test_eval_helpers import COMPANY

from rli.agent.explanation import STEP_EXPLANATION_FALLBACK
from rli.agent.investigator import InvestigatorOutput
from rli.agent.loop import (
    C_VERSION,
    STEP_CANDIDATE_REJECTED,
    STEP_CONTROLLER_DECISION,
    STEP_EXPLANATION,
    STEP_INVESTIGATOR,
    STEP_PROBE_RETRY,
    c_config_hash,
    make_system_c,
    parse_tokens,
    run_system_c,
    with_tokens,
)
from rli.config import Config
from rli.eval.runner import (
    STEP_POLICY_DECISION,
    STEP_PROBE_RUN,
    STEP_PROBE_SKIPPED,
    STEP_SYSTEM_VERSION,
    RunResult,
)
from rli.eval.system_a import run_system_a
from rli.llm.client import CachedClient, LLMError, LLMSchemaError, ScriptedClient
from rli.llm.prompts import PROMPT_VERSION, TEMPLATE_INVESTIGATOR
from rli.models.decision import Decision

NOW = datetime(2026, 9, 7, tzinfo=UTC)

# `scripted_llm`'s defaults, restated so the token/cost assertions below read
# as arithmetic rather than as magic. At `[llm.prices."gemini-2.5-pro"]`
# (1.25 usd/Mtok in, 10 usd/Mtok out) one scripted call costs
# 8000/1e6*1.25 + 1000/1e6*10 = 0.02 usd.
INPUT_TOKENS = 8_000
OUTPUT_TOKENS = 1_000
CALL_USD = 0.02


def _run_c(
    conn: sqlite3.Connection,
    cfg: Config,
    job_id: str,
    llm: ScriptedClient | CachedClient | None,
    *,
    max_steps: int | None = None,
    use_tool_cache: bool = False,
) -> RunResult:
    """`run_system_c` with this suite's hermeticity keywords pinned.

    `sleep` is neutralized (a 5xx fixture would otherwise pay `[net]`'s real
    exponential backoff), the tool cache is off by default so a mocked fetch
    is never answered from a stored row, and `collection_status_csv` points at
    a file that does not exist so no run depends on the checked-in
    `data/events/collection_status.csv`.
    """
    return run_system_c(
        conn,
        cfg,
        job_url(job_id),
        llm,
        now=NOW,
        sleep=lambda _seconds: None,
        use_tool_cache=use_tool_cache,
        collection_status_csv=NO_COLLECTION_STATUS,
        max_steps=max_steps,
    )


def _dynamic_probe_runs(steps: list[sqlite3.Row], name: str) -> list[sqlite3.Row]:
    """Every `probe_run` row for one probe — one per EXECUTION, retries included."""
    return [
        row for row in steps if row["decision_type"] == STEP_PROBE_RUN and row["probe_name"] == name
    ]


def _stop_reasons(steps: list[sqlite3.Row]) -> list[str]:
    """The `controller_decision:stop:<reason>` rows, in trace order."""
    return [
        str(row["decision_type"])
        for row in steps_matching(steps, f"{STEP_CONTROLLER_DECISION}:stop:")
    ]


def _model_rows(steps: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in steps if row["component"] == "model"]


# ---------------------------------------------------------------------------
# 1. The step cap
# ---------------------------------------------------------------------------


@respx.mock
def test_step_cap_stops_the_loop_after_exactly_max_steps_probes(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §4's "at most N dynamic probe steps", driven at N=1.

    The investigator is scripted to keep proposing the SAME valid, eligible
    probe, so nothing except the cap can end this run: the candidate is never
    invalid, never ineligible, and (because the loop stops first) never a
    duplicate. If the cap were computed off by one, the second proposal would
    execute and the assertion on the `probe_run` count would fail — which is
    the only way to tell a working cap from a run that happened to stop.
    """
    job_id = "9101"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm([propose("repost_history", posting_id=posting_id)] * 3, cfg=cfg)

    result = _run_c(conn, cfg, job_id, llm, max_steps=1)

    assert result.probes_run == ("repost_history",)
    steps = run_steps(conn, result.run_id)
    assert len(_dynamic_probe_runs(steps, "repost_history")) == 1
    assert _stop_reasons(steps) == [f"{STEP_CONTROLLER_DECISION}:stop:step_cap"]


@respx.mock
def test_step_cap_of_zero_stops_before_paying_for_an_investigator_call(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`max_steps=0` must cost nothing at the model, not merely run no probes.

    This is the behavioural claim behind `rli.agent.loop`'s pre-flight
    `decide(output=None)`: the controller's model-independent hard stops are
    QUERIED before the call, so a stop that is true regardless of what the
    model would say is taken without paying for the model to say it. A loop
    that called first and stopped afterwards would satisfy every other
    assertion in this file while billing an API on every capped run, so the
    absence of the `investigator` model row is asserted directly, and the
    scripted client's own call log is asserted alongside it.
    """
    job_id = "9102"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm([propose("repost_history", posting_id=posting_id)] * 3, cfg=cfg)

    result = _run_c(conn, cfg, job_id, llm, max_steps=0)

    assert result.probes_run == ()
    steps = run_steps(conn, result.run_id)
    assert _stop_reasons(steps) == [f"{STEP_CONTROLLER_DECISION}:stop:step_cap"]
    assert [
        row for row in _model_rows(steps) if row["decision_type"].startswith(STEP_INVESTIGATOR)
    ] == []
    assert template_calls(llm, TEMPLATE_INVESTIGATOR) == []
    # The run still explains itself: the cap bounds investigation, not output.
    assert steps_matching(steps, STEP_EXPLANATION)


# ---------------------------------------------------------------------------
# 2. The cost cap
# ---------------------------------------------------------------------------


@respx.mock
def test_cost_cap_stops_the_loop_and_bounds_what_the_loop_spends(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`[agent].max_cost_usd` binds the loop, and the loop's spend is real dollars.

    The ledger is loaded deliberately: `[agent].max_cost_usd` is cut to
    0.05 usd and each scripted call is billed 0.02 usd from
    `[llm.prices]`, so two investigator calls (0.04) fit and the third
    `company_events` probe (0.017 usd from `[probe_costs]` + `[agent]`) does
    not. The run must therefore end at a cost-capped controller decision
    rather than at the step cap, which is four steps away.

    The assertion is made over `component='model'` rows only, because
    `run_steps.cost_usd` mixes two units: `rli.eval.runner` documents a probe
    row's value as "a placeholder UNITLESS COST POINT, not a dollar", while a
    model row's value is a real dollar amount from `[llm.prices]`. Summing the
    column across both would compare 1.0 cost points with 0.02 usd and mean
    nothing.

    The last assertions pin the RUN-level property, which is the one
    spec.md §4 actually states ("budget/latency/step cap is reached" is about
    the run, not about the loop): total model spend across every
    `component='model'` row stays inside `[agent].max_cost_usd`. The
    explanation (spec.md §4 step 10) is a model call like any other, so
    `run_system_c` gates it on the ledger `_run_loop` returns and falls back
    to the deterministic reasons when it will not fit — leaving an
    `explanation_fallback:cost_cap` row instead of a third billed call.
    """
    capped = cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"max_cost_usd": 0.05})})
    job_id = "9103"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm(
        [
            propose("repost_history", posting_id=posting_id),
            propose("company_events", company_id=COMPANY, as_of=NOW.isoformat()),
            propose("company_events", company_id=COMPANY, as_of=NOW.isoformat()),
        ],
        cfg=capped,
    )

    result = _run_c(conn, capped, job_id, llm)

    assert result.probes_run == ("repost_history",)
    steps = run_steps(conn, result.run_id)
    assert _stop_reasons(steps) == [f"{STEP_CONTROLLER_DECISION}:stop:cost_cap"]
    rejected = steps_matching(steps, f"{STEP_CANDIDATE_REJECTED}:budget_cost")
    assert [row["probe_name"] for row in rejected] == ["company_events"]

    cap = capped.agent.effective_max_cost_usd(capped)
    loop_spend = sum(
        row["cost_usd"]
        for row in _model_rows(steps)
        if str(row["decision_type"]).startswith(STEP_INVESTIGATOR)
    )
    assert loop_spend == pytest.approx(2 * CALL_USD)
    assert loop_spend <= cap

    # The cap binds the RUN, so the explanation is gated on the same ledger.
    # Here the two investigator calls (0.04) leave 0.01 against an estimator
    # of 0.02, so the explanation call is skipped rather than billed.
    total_model_spend = sum(row["cost_usd"] for row in _model_rows(steps))
    assert total_model_spend == pytest.approx(2 * CALL_USD)
    assert total_model_spend <= cap

    assert steps_matching(steps, f"{STEP_EXPLANATION_FALLBACK}:cost_cap")
    # Skipped, not merely failed: no explanation model row was ever written.
    assert not [
        row for row in _model_rows(steps) if str(row["decision_type"]).startswith(STEP_EXPLANATION)
    ]
    # The user-facing shape is the ordinary fallback: deterministic reasons,
    # no hypotheses.
    assert result.decision.reason
    assert result.decision.hypotheses == []


@respx.mock
def test_the_loop_refuses_an_investigator_call_it_cannot_afford(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The loop's OWN cost gate, which the controller's budget check cannot express.

    `rli.agent.controller.decide` prices the PROBE it is authorizing; by the
    time it runs, the investigator call it was handed has already been made
    and paid for. So the loop gates the model call itself against
    `max_llm_cost_seen` — the largest call this run has actually been billed
    for — and stops when the remainder could not cover another one.

    The cap is tuned to land between the two gates: at 0.05 usd the run has
    0.023 left after one call plus `repost_history`, enough for a second call,
    and it is the CONTROLLER that then refuses the next probe (the case
    `test_cost_cap_stops_the_loop_and_bounds_what_the_loop_spends` covers). At
    0.04 the remainder is 0.013 — less than the 0.02 a call has cost — and the
    loop must stop first, without a second call and therefore without any
    candidate to reject. The absence of a `candidate_rejected:budget_cost` row
    is what tells the two stops apart: they share a `decision_type`, so
    asserting the reason alone would not distinguish them.
    """
    capped = cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"max_cost_usd": 0.04})})
    job_id = "9116"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm(
        [
            propose("repost_history", posting_id=posting_id),
            propose("company_events", company_id=COMPANY, as_of=NOW.isoformat()),
        ],
        cfg=capped,
    )

    result = _run_c(conn, capped, job_id, llm)

    assert result.probes_run == ("repost_history",)
    assert len(template_calls(llm, TEMPLATE_INVESTIGATOR)) == 1

    steps = run_steps(conn, result.run_id)
    stops = steps_matching(steps, f"{STEP_CONTROLLER_DECISION}:stop:cost_cap")
    assert len(stops) == 1
    assert "no budget for another call" in str(stops[0]["error"])
    assert steps_matching(steps, f"{STEP_CANDIDATE_REJECTED}:budget_cost") == []


# ---------------------------------------------------------------------------
# 3-5. Candidate filtering: invalid, duplicate, ineligible
# ---------------------------------------------------------------------------


@respx.mock
def test_invalid_probe_arguments_are_rejected_and_the_probe_never_runs(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §2's "Pydantic-validate probe arguments", from the model outward.

    `repost_history` is proposed with NO arguments at all, which fails
    `RepostHistoryArgs` on a required field. The probe is otherwise perfectly
    runnable on this corpus — history is usable and `repost_pattern` is open —
    so the only reason it can fail to execute is the validation the controller
    performs, and the trace must say so by name. The run still has to reach a
    valid `Decision`: a model that proposes nonsense degrades System C to the
    always-run evidence, it does not break the run.
    """
    job_id = "9104"
    seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm([propose("repost_history")], cfg=cfg)

    result = _run_c(conn, cfg, job_id, llm)

    steps = run_steps(conn, result.run_id)
    rejected = steps_matching(steps, f"{STEP_CANDIDATE_REJECTED}:invalid_args")
    assert [row["probe_name"] for row in rejected] == ["repost_history"]
    assert "posting_id" in str(rejected[0]["error"])

    assert result.probes_run == ()
    assert _dynamic_probe_runs(steps, "repost_history") == []
    assert Decision.model_validate(result.decision.model_dump()) == result.decision


@respx.mock
def test_proposing_the_same_probe_and_arguments_twice_is_rejected_as_duplicate(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §4's "the same probe+arguments would repeat" hard stop, across steps.

    The corpus matters more than the script here. `repost_history` on this
    fixture emits no claim that populates `repost_pattern`, so the input stays
    open and the probe stays ELIGIBLE on the second step — which is what
    forces the controller past its eligibility gate and onto the duplicate
    check. On a corpus where the first execution answered the question, the
    second proposal would be refused as `ineligible` first (the controller
    filters in that order) and this test would silently stop testing
    duplication.
    """
    job_id = "9105"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm([propose("repost_history", posting_id=posting_id)] * 2, cfg=cfg)

    result = _run_c(conn, cfg, job_id, llm)

    assert result.probes_run == ("repost_history",)
    steps = run_steps(conn, result.run_id)
    assert len(_dynamic_probe_runs(steps, "repost_history")) == 1
    duplicates = steps_matching(steps, f"{STEP_CANDIDATE_REJECTED}:duplicate")
    assert [row["probe_name"] for row in duplicates] == ["repost_history"]
    assert _stop_reasons(steps) == [f"{STEP_CONTROLLER_DECISION}:stop:no_eligible_candidate"]


@respx.mock
def test_an_ineligible_probe_is_rejected_even_with_valid_arguments(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """`team_signal` is the cleanest ineligibility: it is off by configuration.

    `[team_signal].enabled` now defaults to `True` (spec.md §5's Amendment
    2026-09-10 re-sourced the probe from first-party board history), so this
    test builds an explicitly disabled config instead. With `enabled = false`,
    `TeamSignalProbe.eligible` reads exactly that flag, so the rejection is
    unconditional — it does not depend on the corpus, on how much history was
    seeded, or on which policy inputs happen to be open. The history-gated
    alternative (`repost_history` against a company with no captures) would
    also work, but it couples the assertion to `[thresholds].min_history_days`
    and would start passing for the wrong reason if that threshold moved.

    The arguments are deliberately VALID: the controller checks
    `invalid_args` before `ineligible`, so an under-specified proposal would
    be rejected one gate too early and never reach the gate under test.
    """
    disabled_cfg = cfg.model_copy(
        update={"team_signal": cfg.team_signal.model_copy(update={"enabled": False})}
    )
    job_id = "9106"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm(
        [propose("team_signal", posting_id=posting_id, company_id=COMPANY)], cfg=disabled_cfg
    )

    result = _run_c(conn, disabled_cfg, job_id, llm)

    steps = run_steps(conn, result.run_id)
    rejected = steps_matching(steps, f"{STEP_CANDIDATE_REJECTED}:ineligible")
    assert [row["probe_name"] for row in rejected] == ["team_signal"]
    assert result.probes_run == ()
    assert _dynamic_probe_runs(steps, "team_signal") == []


# ---------------------------------------------------------------------------
# 6. Nothing left to ask
# ---------------------------------------------------------------------------


@respx.mock
def test_a_closed_posting_stops_before_the_first_model_call(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """No unresolved question can change an unconditional action, so nothing is asked.

    A closed posting is the cleanest instance because spec.md §5's `P1_closed`
    branch is UNCONDITIONAL: whatever the remaining unknowns turn out to be,
    the action stays put, so `could_change_action` is empty and every dynamic
    probe is worthless by construction. That is the same argument
    `rli.eval.system_b`'s `R0_terminal` route makes for skipping its routed
    probes, reached here through the controller's hard stop instead of through
    a routing tree.

    The investigator is scripted with a perfectly good candidate precisely so
    that the absence of a model row means something: the loop is not stopping
    because the model said stop, it is stopping before the model is consulted.
    """
    job_id = "9107"
    posting_id = seed_history(conn, job_id, now=NOW)
    mock_greenhouse_missing(job_id)
    llm = scripted_llm([propose("repost_history", posting_id=posting_id)], cfg=cfg)

    result = _run_c(conn, cfg, job_id, llm)

    assert result.decision.posting_state == "closed"
    assert result.probes_run == ()
    steps = run_steps(conn, result.run_id)
    assert _stop_reasons(steps) == [f"{STEP_CONTROLLER_DECISION}:stop:no_unresolved_question"]
    assert [
        row for row in _model_rows(steps) if str(row["decision_type"]).startswith(STEP_INVESTIGATOR)
    ] == []
    assert template_calls(llm, TEMPLATE_INVESTIGATOR) == []


# ---------------------------------------------------------------------------
# 7. A broken investigator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(LLMSchemaError("model returned prose, not the schema"), id="schema"),
        pytest.param(LLMError("LLM request failed: connection reset"), id="transport"),
    ],
)
@respx.mock
def test_an_investigator_failure_still_yields_the_frozen_policy_decision(
    conn: sqlite3.Connection, cfg: Config, failure: LLMError
) -> None:
    """ "The model broke" and "the model answered nonsense" both stop, neither crashes.

    Both cases are parametrized through one body because
    `rli.agent.controller` gives them one consequence by design — its
    `investigator_error` stop covers both, and `LLMSchemaError` subclasses
    `LLMError` for exactly that reason. Writing them as two bodies would
    assert the same four facts twice and invite them to drift apart.

    The last assertion is the one that matters for spec.md §2 ("Never rely on
    the LLM alone for ... the final action"): with the model contributing
    nothing, System C's action must be exactly what the frozen policy produces
    from the always-run evidence alone. That baseline is a second System C run
    capped at zero dynamic steps — the same code path, the same corpus, the
    same clock, with the loop removed — rather than a re-derivation of the
    policy here, which would only prove this test can call `rli.policy`.
    """
    job_id = "9108"
    seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm([failure], cfg=cfg)

    result = _run_c(conn, cfg, job_id, llm)

    steps = run_steps(conn, result.run_id)
    failed = [
        row for row in _model_rows(steps) if str(row["decision_type"]).startswith(STEP_INVESTIGATOR)
    ]
    assert len(failed) == 1
    # No `:tokens=` suffix: there was no usage to report, and `0/0` would read
    # as "billed for nothing" rather than "we do not know" (rli.agent.loop).
    assert failed[0]["decision_type"] == STEP_INVESTIGATOR
    assert failed[0]["error"]
    assert type(failure).__name__ in str(failed[0]["error"])
    assert parse_tokens(str(failed[0]["decision_type"])) is None

    assert _stop_reasons(steps) == [f"{STEP_CONTROLLER_DECISION}:stop:investigator_error"]
    assert result.probes_run == ()

    row = conn.execute("SELECT status FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert row["status"] == "completed"
    assert Decision.model_validate(result.decision.model_dump()) == result.decision

    baseline = _run_c(conn, cfg, job_id, scripted_llm(cfg=cfg), max_steps=0)
    assert result.decision.recommended_action == baseline.decision.recommended_action
    assert result.decision.posting_state == baseline.decision.posting_state
    assert result.decision.recheck_after_days == baseline.decision.recheck_after_days
    assert result.decision.evidence_quality == baseline.decision.evidence_quality


# ---------------------------------------------------------------------------
# 8. Replay through llm_cache
# ---------------------------------------------------------------------------


@respx.mock
def test_a_second_identical_run_is_served_entirely_from_llm_cache(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §2's `(model_id, prompt_hash, structured_input_hash)` cache, end to end.

    The two runs are made identical the only way they can be: same `conn`,
    same corpus, same url, same `now`, the same `CachedClient` over the same
    inner `ScriptedClient`, and `use_tool_cache=True` so the probes' HTTP
    layer replays too. Everything the investigator prompt is built from is
    then a pure function of the case — which is precisely the property
    `rli.agent.loop` protects by reporting PROBE-ONLY budget remainders to the
    model instead of the true ledger, whose measured latency and cache-
    dependent cost would make the step-2 prompt differ between the two runs
    and turn every multi-step replay into a permanent cache miss.

    The inner client's call count is the sharpest assertion available: a
    `cache_status='hit'` row could in principle be written by a wrapper that
    called anyway, whereas an unchanged `ScriptedClient.calls` cannot.

    The two `Decision`s are compared field for field with evidence ids
    INCLUDED — they are assigned per run in append order, so identical runs
    must produce identical ids — and with only `EvidenceItem.run_id` stripped,
    since that column names the run by construction.
    """
    job_id = "9109"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    inner = scripted_llm(
        [
            propose("repost_history", posting_id=posting_id),
            InvestigatorOutput(stop=True, stop_reason="nothing left worth running"),
        ],
        explanation=cited_explanation("The board still lists the role.", "e1"),
        cfg=cfg,
    )
    client = CachedClient(inner, conn)

    first = _run_c(conn, cfg, job_id, client, use_tool_cache=True)
    calls_after_first = len(inner.calls)
    second = _run_c(conn, cfg, job_id, client, use_tool_cache=True)

    assert calls_after_first == 3  # two investigator turns plus one explanation
    assert len(inner.calls) == calls_after_first

    first_models = _model_rows(run_steps(conn, first.run_id))
    assert [row["cache_status"] for row in first_models] == ["miss"] * 3
    second_models = _model_rows(run_steps(conn, second.run_id))
    assert [row["cache_status"] for row in second_models] == ["hit"] * 3
    assert [row["cost_usd"] for row in second_models] == [0.0] * 3

    assert first.probes_run == second.probes_run == ("repost_history",)
    assert decision_fingerprint(first.decision) == decision_fingerprint(second.decision)


# ---------------------------------------------------------------------------
# 9. The trace
# ---------------------------------------------------------------------------


@respx.mock
def test_a_healthy_run_writes_the_full_ordered_trace(conn: sqlite3.Connection, cfg: Config) -> None:
    """`run_steps` is the canonical trace (spec.md §7), so its ORDER is the contract.

    A reader auditing "did the LLM choose the action?" walks `step_index` and
    must see: C's freeze identity, the always-run pair, then the investigate /
    decide / execute cycle, then the frozen policy, and only then the
    explanation. The last two being in that order is the whole reason
    `rli.agent.loop` rewrites `runs.final_decision` after the run is closed
    instead of explaining first — a trace in the other order would assert that
    C explained a decision it had not yet made.

    The row-by-row comparison uses the BASE `decision_type` token (everything
    before the first colon) so the shape assertion does not also pin which
    policy branch this fixture happens to fire or what the token counts were;
    the qualified values that carry real information — the two controller
    verdicts, the model rows' token suffixes — are asserted separately below.

    This test also carries the two run-level pins that need a healthy run with
    a real LLM-authored explanation behind them: that `runs.final_decision`
    round-trips to exactly the `Decision` the caller received, LLM-authored
    `reason` and `hypotheses` included, and that `runs.config_hash` is
    `c_config_hash`'s composite of config, loop version, model and prompt.
    """
    job_id = "9110"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm(
        [
            propose("repost_history", posting_id=posting_id),
            InvestigatorOutput(stop=True, stop_reason="nothing left worth running"),
        ],
        explanation=cited_explanation(
            "The board listing still carries the role.",
            "e1",
            hypotheses=("Speculation: the team may be slow-rolling the hire.",),
        ),
        cfg=cfg,
    )

    result = _run_c(conn, cfg, job_id, llm)
    steps = run_steps(conn, result.run_id)

    assert [row["step_index"] for row in steps] == list(range(1, len(steps) + 1))
    assert [
        (row["component"], str(row["decision_type"]).split(":")[0], row["probe_name"])
        for row in steps
    ] == [
        ("controller", STEP_SYSTEM_VERSION, None),
        ("probe", STEP_PROBE_RUN, "resolve_posting"),
        ("probe", STEP_PROBE_RUN, "board_snapshot"),
        ("model", STEP_INVESTIGATOR, None),
        ("controller", STEP_CONTROLLER_DECISION, "repost_history"),
        ("probe", STEP_PROBE_RUN, "repost_history"),
        ("model", STEP_INVESTIGATOR, None),
        ("controller", STEP_CONTROLLER_DECISION, None),
        ("controller", STEP_POLICY_DECISION, None),
        ("model", STEP_EXPLANATION, None),
    ]

    assert [
        str(row["decision_type"]) for row in steps_matching(steps, f"{STEP_CONTROLLER_DECISION}:")
    ] == [
        f"{STEP_CONTROLLER_DECISION}:run:ranked_best",
        f"{STEP_CONTROLLER_DECISION}:stop:investigator_stop",
    ]
    assert steps[0]["args_hash"] == f"{C_VERSION}:{MODEL_ID}:{PROMPT_VERSION}"

    # Every model row is fully attributed, and its tokens survive the
    # `decision_type` encoding `rli.agent.loop.with_tokens` applies.
    model_rows = _model_rows(steps)
    assert len(model_rows) == 3
    for row in model_rows:
        assert row["model_id"] == MODEL_ID
        assert row["prompt_hash"]
        assert parse_tokens(str(row["decision_type"])) == (INPUT_TOKENS, OUTPUT_TOKENS)
    assert model_rows[-1]["decision_type"] == with_tokens(
        STEP_EXPLANATION, INPUT_TOKENS, OUTPUT_TOKENS
    )

    # -- run-level pins ----------------------------------------------------
    run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run_row["system"] == "C"
    assert run_row["status"] == "completed"
    stored = Decision.model_validate(json.loads(run_row["final_decision"]))
    assert stored == result.decision
    # The explanation reached BOTH the returned decision and the stored row,
    # which is what `_rewrite_final_decision` exists to guarantee.
    assert [item.model_dump() for item in stored.reason] == [
        {"text": "The board listing still carries the role.", "evidence_ids": ["e1"]}
    ]
    assert stored.hypotheses == ["Speculation: the team may be slow-rolling the hire."]
    assert not steps_matching(steps, "explanation_fallback")

    assert run_row["config_hash"] == c_config_hash(cfg, MODEL_ID)
    assert f"|{C_VERSION}:" in run_row["config_hash"]


# ---------------------------------------------------------------------------
# 10. Agreement with System A
# ---------------------------------------------------------------------------


@respx.mock
def test_c_agrees_with_a_when_it_chooses_the_same_probes(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §6's action-agreement metric, in miniature.

    spec.md §6 compares each system's recommended action against System A's
    full-probe reference. The comparison only means something if a divergence
    can be attributed to probe SELECTION — `rli.eval.runner`'s whole design is
    that "the ONLY thing that differs between systems is which probes they
    choose to run". This test removes that variable: the investigator is
    scripted to propose exactly the probes System A runs on this corpus, in
    System A's own cheapest-first order, so the two systems see identical
    evidence and any difference in the four decided fields would be a real
    divergence in the shared policy path rather than a difference of
    investigation.

    `[team_signal].enabled` now defaults to `True` (spec.md §5's Amendment
    2026-09-10), and this corpus's usable history plus the still-open
    `corroborating_hiring_signal` question make `team_signal` genuinely
    eligible on this corpus too, so System A's full-probe run now executes
    four dynamic probes, not three — `repost_history` (`low`), then
    `company_events` and `requirements_drift` (tied at `medium`, alphabetical
    tiebreak), then `team_signal` (`high`). The script below matches that
    exactly so C reaches the same probe set.

    Four dynamic steps still fit inside the default
    `[thresholds].max_dynamic_steps` of 4, so no cap override is needed. Both
    systems run against the same `conn`: `rli.eval.runner`'s write invariant
    limits them to `runs`, `run_steps` and `evidence`, all scoped by `run_id`,
    so neither can contaminate the other's corpus.
    """
    job_id = "9111"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))
    llm = scripted_llm(
        [
            propose("repost_history", posting_id=posting_id),
            propose("company_events", company_id=COMPANY, as_of=NOW.isoformat()),
            propose("requirements_drift", posting_id=posting_id),
            propose("team_signal", posting_id=posting_id, company_id=COMPANY),
        ],
        cfg=cfg,
    )

    result_c = _run_c(conn, cfg, job_id, llm)
    result_a = run_system_a(
        conn,
        cfg,
        job_url(job_id),
        now=NOW,
        sleep=lambda _seconds: None,
        use_tool_cache=False,
        collection_status_csv=NO_COLLECTION_STATUS,
    )

    assert result_c.probes_run == result_a.probes_run
    assert result_c.decision.recommended_action == result_a.decision.recommended_action
    assert result_c.decision.posting_state == result_a.decision.posting_state
    assert result_c.decision.recheck_after_days == result_a.decision.recheck_after_days
    assert result_c.decision.evidence_quality == result_a.decision.evidence_quality


# ---------------------------------------------------------------------------
# 11. The A/B-shaped adapter
# ---------------------------------------------------------------------------


@respx.mock
def test_make_system_c_has_the_exact_ab_call_shape_and_the_same_result(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """The adapter exists so `{"A": ..., "B": ..., "C": ...}` needs no special case.

    That promise is a claim about a SIGNATURE, so it is tested as one:
    `inspect.signature` compares parameter names, kinds, defaults and
    annotations against `run_system_a` itself, rather than against a
    hand-copied list that would quietly stop matching the day A grows a
    keyword. If A and C's shapes ever diverge, this fails loudly at the one
    place that can explain why.

    The behavioural half runs the same scenario twice — once through
    `run_system_c` and once through the adapter — and compares the decisions,
    because a signature that matches while the closure drops `llm` or
    `max_steps` on the floor would be worse than no adapter at all.
    """
    assert inspect.signature(make_system_c()) == inspect.signature(run_system_a)

    job_id = "9112"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))

    direct = _run_c(
        conn, cfg, job_id, scripted_llm([propose("repost_history", posting_id=posting_id)], cfg=cfg)
    )
    adapter = make_system_c(
        scripted_llm([propose("repost_history", posting_id=posting_id)], cfg=cfg)
    )
    via_adapter = adapter(
        conn,
        cfg,
        job_url(job_id),
        now=NOW,
        sleep=lambda _seconds: None,
        use_tool_cache=False,
        collection_status_csv=NO_COLLECTION_STATUS,
    )

    assert adapter.__name__ == "run_system_c"
    assert via_adapter.system == "C"
    assert via_adapter.probes_run == direct.probes_run == ("repost_history",)
    assert decision_fingerprint(via_adapter.decision) == decision_fingerprint(direct.decision)


# ---------------------------------------------------------------------------
# 12. The bounded probe retry
# ---------------------------------------------------------------------------


@respx.mock
def test_a_retryable_probe_failure_is_retried_exactly_max_probe_retries_times(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §2's "no uncontrolled retry loops", measured against the config knob.

    `requirements_drift` is the only dynamic probe on this corpus that makes a
    network call (it fetches the posting's CURRENT ATS content to diff), so it
    is the only one whose failure a respx route can control. The route is
    scripted to answer the always-run resolver once and then serve 503
    forever: `rli.net` turns a persistent 5xx into a structured
    `{ok:false, retryable:true}` failure, which is exactly the condition
    `rli.agent.loop._execute_chosen` retries on.

    The two halves are one test on purpose. The claim is a CONTRAST — one
    identical failure retried once under `[agent].max_probe_retries = 1` and
    not at all under `0` — and split into two tests each half would keep
    passing if the knob were ignored and the count were hard-coded. Two job
    ids and two counters keep the runs independent inside the one mock.

    The persistent (rather than transient) failure is what pins the bound:
    the second attempt fails too, and there must still be no third execution
    and no `probe_retry:2` row. `RunResult.probes_run` names the probe once
    either way — it is the set of investigations C chose to open, not the
    number of attempts, and double-counting a flaky probe would penalize C in
    the one metric spec.md §6's agent gate turns on.
    """

    def flaky(job_id: str) -> Callable[[httpx.Request], httpx.Response]:
        """200 for the always-run resolver's one call, then 503 for everything after."""
        payload = gh_job(job_id, now=NOW, first_published_days_ago=40)
        seen: list[int] = []

        def responder(_request: httpx.Request) -> httpx.Response:
            seen.append(1)
            if len(seen) == 1:
                return httpx.Response(200, json=payload)
            return httpx.Response(503, text="upstream unavailable")

        return responder

    retried_job, plain_job = "9113", "9114"
    posting_ids: dict[str, str] = {}
    for job_id in (retried_job, plain_job):
        posting_ids[job_id] = seed_reposted_history(conn, job_id, now=NOW)
        mock_greenhouse(
            job_id,
            gh_job(job_id, now=NOW, first_published_days_ago=40),
            job_api_side_effect=flaky(job_id),
        )

    # -- [agent].max_probe_retries = 1 (the config default) -----------------
    assert cfg.agent.max_probe_retries == 1
    llm = scripted_llm(
        [propose("requirements_drift", posting_id=posting_ids[retried_job])] * 2,
        cfg=cfg,
    )
    retried = _run_c(conn, cfg, retried_job, llm)

    assert retried.probes_run == ("requirements_drift",)
    steps = run_steps(conn, retried.run_id)
    retries = steps_matching(steps, STEP_PROBE_RETRY)
    assert [str(row["decision_type"]) for row in retries] == [f"{STEP_PROBE_RETRY}:1"]
    assert retries[0]["probe_name"] == "requirements_drift"
    # The row carries the PREVIOUS attempt's error, so the trace says what was
    # being recovered from rather than only that a recovery happened.
    assert "503" in str(retries[0]["error"])
    executions = _dynamic_probe_runs(steps, "requirements_drift")
    assert len(executions) == 2
    assert all(row["error"] for row in executions)

    # -- [agent].max_probe_retries = 0 disables it entirely -----------------
    no_retry_cfg = cfg.model_copy(
        update={"agent": cfg.agent.model_copy(update={"max_probe_retries": 0})}
    )
    plain = _run_c(
        conn,
        no_retry_cfg,
        plain_job,
        scripted_llm(
            [propose("requirements_drift", posting_id=posting_ids[plain_job])] * 2,
            cfg=no_retry_cfg,
        ),
    )

    assert plain.probes_run == ("requirements_drift",)
    plain_steps = run_steps(conn, plain.run_id)
    assert steps_matching(plain_steps, STEP_PROBE_RETRY) == []
    assert len(_dynamic_probe_runs(plain_steps, "requirements_drift")) == 1


# ---------------------------------------------------------------------------
# 13. Prompt determinism — the property the llm_cache rests on
# ---------------------------------------------------------------------------


@respx.mock
def test_the_investigator_prompt_is_a_pure_function_of_the_case(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Two identical runs must build byte-identical investigator prompts, every step.

    `rli.agent.investigator.build_investigator_input` folds
    `cost_remaining_usd` and `latency_remaining_s` into the `structured_input`
    that `Prompt.structured_input_hash()` hashes — a third of the
    `(model_id, prompt_hash, structured_input_hash)` key spec.md §2 mandates
    and spec.md §6's exact replay depends on. The obvious values to pass are
    the enforcement ledger's remainders, and they are POISON: that ledger
    charges every investigator call its real `cost_usd` and its MEASURED
    wall-clock latency, so from step 2 on the prompt stops being a function of
    the case. `rli.agent.loop` works around it by reporting probe-only,
    rounded remainders instead (its "the budget SHOWN to the model excludes
    LLM spend" judgment call).

    What breaks if this test fails is silent and expensive: every step-2 call
    becomes a permanent cache MISS for every case forever, an original run and
    its replay can never agree (a hit costs 0.0 and a live call does not), and
    the only symptom is a benchmark that looks slow and costs real money.
    Nothing raises.

    The run is deliberately MULTI-step: the first prompt of a run is identical
    under either accounting, so a single-step scenario would pass while the
    bug was fully present.
    """
    job_id = "9115"
    posting_id = seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=40))

    def script() -> ScriptedClient:
        return scripted_llm(
            [
                propose("repost_history", posting_id=posting_id),
                propose("company_events", company_id=COMPANY, as_of=NOW.isoformat()),
                InvestigatorOutput(stop=True, stop_reason="nothing left worth running"),
            ],
            explanation=cited_explanation("The board still lists the role.", "e1"),
            cfg=cfg,
        )

    first_llm, second_llm = script(), script()
    first = _run_c(conn, cfg, job_id, first_llm)
    second = _run_c(conn, cfg, job_id, second_llm)

    assert first.probes_run == second.probes_run == ("repost_history", "company_events")

    first_hashes = [
        prompt.structured_input_hash()
        for prompt in template_calls(first_llm, TEMPLATE_INVESTIGATOR)
    ]
    second_hashes = [
        prompt.structured_input_hash()
        for prompt in template_calls(second_llm, TEMPLATE_INVESTIGATOR)
    ]
    assert len(first_hashes) == 3
    assert first_hashes == second_hashes
    # Distinct per step, so the equality above is not the trivial one that
    # would also hold if every prompt in a run were the same object.
    assert len(set(first_hashes)) == 3


# ---------------------------------------------------------------------------
# 14. The loop's identity precondition
# ---------------------------------------------------------------------------


@respx.mock
def test_an_unresolved_identity_stops_the_loop_without_a_model_call(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """No `posting_id`/`company_id` means no probe's arguments exist, so nothing is asked.

    `rli.probes.registry.build_args` needs both ids, so with `case.case_file()`
    `None` the controller would reject every candidate as
    `ineligible`/`identity_unresolved` — the model call is guaranteed to buy
    nothing before it is made. The loop states that as a PRECONDITION rather
    than paying to rediscover it, and records the same
    `probe_skipped:identity_unresolved` row System A and System B write plus
    the `no_eligible_candidate` stop the controller would have produced.

    The investigator is scripted with a candidate that would be perfectly
    valid on a resolvable posting, so the absence of any investigator call is
    a claim about the precondition and not about an empty script.
    """
    url = "https://careers.acme.com/jobs/1"
    respx.get(url).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    llm = scripted_llm([propose("repost_history", posting_id="greenhouse:acme:1")], cfg=cfg)

    result = run_system_c(
        conn,
        cfg,
        url,
        llm,
        now=NOW,
        sleep=lambda _seconds: None,
        use_tool_cache=False,
        collection_status_csv=NO_COLLECTION_STATUS,
    )

    assert result.probes_run == ()
    steps = run_steps(conn, result.run_id)
    assert steps_matching(steps, f"{STEP_PROBE_SKIPPED}:identity_unresolved")
    assert _stop_reasons(steps) == [f"{STEP_CONTROLLER_DECISION}:stop:no_eligible_candidate"]
    assert template_calls(llm, TEMPLATE_INVESTIGATOR) == []
    assert [
        row for row in _model_rows(steps) if str(row["decision_type"]).startswith(STEP_INVESTIGATOR)
    ] == []
    assert Decision.model_validate(result.decision.model_dump()) == result.decision
