"""System R (`rli.eval.system_r`): C's controller and loop with no LLM.

Two claims are tested end to end against the same live-mode fixtures the
System C loop tests use (`test_agent_helpers`):

1. R makes ZERO model calls: no `component='model'` step, no LLM client built.
2. R reproduces C's probe sequence and decision when C's investigator is a
   stub that proposes every eligible probe — i.e. R is exactly "C minus the
   model", not a different controller.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from pydantic import BaseModel
from test_agent_helpers import (
    MODEL_ID,
    NO_COLLECTION_STATUS,
    gh_job,
    job_url,
    mock_greenhouse,
    run_steps,
    seed_history,
    seed_reposted_history,
)
from test_eval_helpers import add_posting
from typer.testing import CliRunner

from rli.agent.explanation import ExplanationOutput
from rli.agent.investigator import InvestigatorOutput, ProbeCandidate
from rli.agent.loop import run_system_c
from rli.cli import _normalized_system
from rli.config import Config
from rli.eval.runner import STEP_PROBE_RUN, STEP_SYSTEM_VERSION
from rli.eval.system_r import R_VERSION, r_config_hash, r_rules_hash, run_system_r
from rli.llm import client as llm_client_module
from rli.llm.client import Prompt, ScriptedClient
from rli.llm.prompts import TEMPLATE_EXPLANATION, TEMPLATE_INVESTIGATOR
from rli.probes.registry import DYNAMIC_PROBES
from rli.replay.run import SYSTEM_RUNNERS

NOW = datetime(2026, 9, 7, tzinfo=UTC)

cli_runner = CliRunner()


def _run_r(conn: sqlite3.Connection, cfg: Config, job_id: str):
    return run_system_r(
        conn,
        cfg,
        job_url(job_id),
        now=NOW,
        sleep=lambda _seconds: None,
        use_tool_cache=False,
        collection_status_csv=NO_COLLECTION_STATUS,
    )


def _all_eligible_stub(cfg: Config) -> ScriptedClient:
    """An investigator that proposes EVERY eligible catalogue entry, valid args."""

    def responder(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
        if prompt.template_id == TEMPLATE_INVESTIGATOR:
            data = prompt.structured_input
            identity = data["identity"]
            values: dict[str, Any] = {
                "posting_id": identity["posting_id"],
                "company_id": identity["company_id"],
                "as_of": data["now"],
            }
            candidates = []
            for entry in data["probe_catalogue"]:
                if not entry["eligible"]:
                    continue
                fields = DYNAMIC_PROBES[entry["name"]].ArgsModel.model_fields
                args = {key: value for key, value in values.items() if key in fields}
                candidates.append(ProbeCandidate(probe=entry["name"], args=args))
            return InvestigatorOutput(candidates=candidates)
        if prompt.template_id == TEMPLATE_EXPLANATION:
            return ExplanationOutput()
        raise AssertionError(prompt.template_id)

    return ScriptedClient(responder, model_id=MODEL_ID, cfg=cfg)


def _executed(steps: list[sqlite3.Row]) -> list[str]:
    return [
        str(row["probe_name"])
        for row in steps
        if row["decision_type"] == STEP_PROBE_RUN and row["probe_name"] in DYNAMIC_PROBES
    ]


@respx.mock
def test_system_r_makes_zero_llm_calls(
    conn: sqlite3.Connection, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("System R must never build an LLM client")

    monkeypatch.setattr(llm_client_module.OpenAICompatibleClient, "from_config", _no_client)

    job_id = "9301"
    seed_reposted_history(conn, job_id, now=NOW)
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=61))

    result = _run_r(conn, cfg, job_id)

    assert result.system == "R"
    assert result.probes_run, "the fixture must make at least one dynamic probe run"
    steps = run_steps(conn, result.run_id)
    assert [row for row in steps if row["component"] == "model"] == []
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["system"] == "R"
    assert run["status"] == "completed"
    assert run["config_hash"] == r_config_hash(cfg)
    assert f"|{R_VERSION}:" in run["config_hash"]
    version = [row for row in steps if row["decision_type"] == STEP_SYSTEM_VERSION]
    assert [row["args_hash"] for row in version] == [f"{R_VERSION}:{r_rules_hash(cfg)}"]
    # The explanation is the deterministic one: every reason cites evidence.
    assert result.decision.reason
    assert result.decision.hypotheses == []


@respx.mock
def test_system_r_matches_c_under_an_all_eligible_stub(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """Two eligible probes (team_signal + company_events); C and R must agree."""
    job_id = "9302"
    seed_history(conn, job_id, now=NOW)
    add_posting(conn, job_id=f"{job_id}-hire", first_observed=NOW - timedelta(days=5))
    mock_greenhouse(job_id, gh_job(job_id, now=NOW, first_published_days_ago=61))

    stub = _all_eligible_stub(cfg)
    c_result = run_system_c(
        conn,
        cfg,
        job_url(job_id),
        stub,
        now=NOW,
        sleep=lambda _seconds: None,
        use_tool_cache=False,
        collection_status_csv=NO_COLLECTION_STATUS,
    )
    r_result = _run_r(conn, cfg, job_id)

    assert len(c_result.probes_run) == 2
    assert r_result.probes_run == c_result.probes_run
    assert _executed(run_steps(conn, r_result.run_id)) == _executed(
        run_steps(conn, c_result.run_id)
    )
    for field in ("posting_state", "recommended_action", "evidence_quality", "recheck_after_days"):
        assert getattr(r_result.decision, field) == getattr(c_result.decision, field)
    # C paid for model calls; R did not.
    c_model = [r for r in run_steps(conn, c_result.run_id) if r["component"] == "model"]
    r_model = [r for r in run_steps(conn, r_result.run_id) if r["component"] == "model"]
    assert c_model and not r_model


def test_system_r_is_registered_everywhere_a_and_b_are() -> None:
    assert SYSTEM_RUNNERS["R"] is run_system_r
    assert _normalized_system("r") == "R"


def test_r_rules_hash_is_stable_and_tracks_the_step_cap(cfg: Config) -> None:
    assert r_rules_hash(cfg) == r_rules_hash(cfg)
    capped = cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"max_dynamic_steps": 1})})
    assert r_rules_hash(capped) != r_rules_hash(cfg)
