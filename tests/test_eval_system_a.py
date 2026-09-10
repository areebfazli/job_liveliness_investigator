"""System A end-to-end (spec.md §6 "A — Full probes"; rli.eval.system_a).

Mocks the HTTP layer only (respx) — every probe, the registry, the policy
and the runner run for real against a synthetic corpus deep enough to make
`repost_history`/`requirements_drift` eligible (`min_history_days`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import respx
from test_eval_helpers import COMPANY, add_capture, add_posting, job

from rli.config import Config
from rli.eval.system_a import ALL_DYNAMIC_INPUTS, run_system_a
from rli.models.case_file import CaseFile
from rli.models.decision import Decision
from rli.probes.base import ProbeContext
from rli.probes.registry import eligible_probes

NOW = datetime(2026, 9, 7, tzinfo=UTC)
POSTING_ID = "greenhouse:acme:6001"
URL = "https://boards.greenhouse.io/acme/jobs/6001"

GH_JOB_OPEN = {
    "id": 6001,
    "title": "Backend Engineer",
    "absolute_url": URL,
    "first_published": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "content": "<p>Build things.</p>",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Remote"}],
}

NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"


def _no_network(_name: str) -> object:
    raise AssertionError("eligibility checks must not touch the network")


def _expected_probes(conn: sqlite3.Connection, cfg: Config) -> set[str]:
    """The registry's own answer for "every eligible dynamic probe" (System A's rule).

    Derived from `rli.probes.registry.eligible_probes` with the same
    neutralized gate `run_system_a` uses (`ALL_DYNAMIC_INPUTS`), so this
    assertion survives a new probe being added without editing this test.
    """
    ctx = ProbeContext(conn=conn, config=cfg, net_client_factory=_no_network, now=lambda: NOW)
    case_file = CaseFile(posting_id=POSTING_ID, company_id=COMPANY, canonical_url=URL)
    selected = eligible_probes(ctx, case_file, unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))
    return {probe_cls.name for probe_cls in selected}


def _seed_corpus(conn: sqlite3.Connection) -> None:
    """Board history spanning 50 days: usable (>=30) but not long-lived (<180)."""
    add_posting(
        conn,
        job_id="6001",
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    for offset in (60, 45, 30, 10):
        add_capture(conn, NOW - timedelta(days=offset), [job("6001")], company_id=COMPANY)


@respx.mock
def test_system_a_end_to_end_runs_every_eligible_dynamic_probe(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _seed_corpus(conn)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/6001").mock(
        return_value=httpx.Response(200, json=GH_JOB_OPEN)
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [GH_JOB_OPEN]})
    )

    result = run_system_a(conn, cfg, URL, now=NOW, sleep=lambda _s: None, use_tool_cache=False)

    # -- runs row -----------------------------------------------------------
    run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run_row["system"] == "A"
    assert run_row["mode"] == "live"
    assert run_row["status"] == "completed"
    assert run_row["policy_version"]
    assert run_row["config_hash"].startswith("cfg:")
    assert run_row["final_decision"] is not None
    assert run_row["total_cost_usd"] is not None
    assert run_row["total_latency_ms"] is not None

    # -- run_steps: contiguous step_index, always-run pair, controller decision
    steps = conn.execute(
        "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index", (result.run_id,)
    ).fetchall()
    indices = [s["step_index"] for s in steps]
    assert indices == list(range(1, len(steps) + 1))
    assert len(set(indices)) == len(indices)

    probe_steps = {s["probe_name"] for s in steps if s["component"] == "probe"}
    assert {"resolve_posting", "board_snapshot"} <= probe_steps

    controller_decisions = [s["decision_type"] for s in steps if s["component"] == "controller"]
    assert any(d.startswith("policy_decision:") for d in controller_decisions)

    # -- evidence: e1..eN, no gaps, all scoped to this run -------------------
    evidence_rows = conn.execute(
        "SELECT id FROM evidence WHERE run_id = ? ORDER BY id", (result.run_id,)
    ).fetchall()
    ids = [r["id"] for r in evidence_rows]
    assert ids == [f"e{i}" for i in range(1, len(ids) + 1)]

    # -- the contract claims rli.eval.case owes -------------------------------
    claim_types = {
        row["claim_type"]
        for row in conn.execute(
            "SELECT DISTINCT claim_type FROM evidence WHERE run_id = ?", (result.run_id,)
        ).fetchall()
    }
    assert "posting_state" in claim_types
    assert ("board_present" in claim_types) or ("board_absent" in claim_types)

    # -- decision round-trips and every citation resolves (spec.md §9) -------
    decision = Decision.model_validate(json.loads(run_row["final_decision"]))
    assert decision == result.decision
    evidence_ids_in_table = {row["id"] for row in evidence_rows}
    decision_evidence_ids = {item.id for item in decision.evidence}
    for reason in decision.reason:
        for eid in reason.evidence_ids:
            assert eid in decision_evidence_ids
            assert eid in evidence_ids_in_table

    # -- A runs every eligible dynamic probe, derived from the registry ------
    expected = _expected_probes(conn, cfg)
    assert set(result.probes_run) == expected
    # `[team_signal].enabled` now defaults to `True` (spec.md §5's Amendment
    # 2026-09-10), and this corpus's 50 days of history clears
    # `min_history_days`, so `team_signal` is allowed to be genuinely
    # eligible here too — the assertion above already covers "A runs every
    # eligible probe, whichever those are".


@respx.mock
def test_system_a_disables_team_signal_when_config_says_so(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    disabled = cfg.model_copy(
        update={"team_signal": cfg.team_signal.model_copy(update={"enabled": False})}
    )
    _seed_corpus(conn)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/6001").mock(
        return_value=httpx.Response(200, json=GH_JOB_OPEN)
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [GH_JOB_OPEN]})
    )

    result = run_system_a(conn, disabled, URL, now=NOW, sleep=lambda _s: None, use_tool_cache=False)

    assert "team_signal" not in result.probes_run


@respx.mock
def test_system_a_survives_resolver_transport_failure(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A structured always-run probe failure completes the run; it never crashes it."""
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/9999").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/9999").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    result = run_system_a(
        conn,
        cfg,
        "https://boards.greenhouse.io/acme/jobs/9999",
        now=NOW,
        sleep=lambda _s: None,
        use_tool_cache=False,
    )

    decision = result.decision
    assert decision.posting_state == "unknown"
    assert decision.recommended_action == "wait"
    assert isinstance(decision.recheck_after_days, int)
    assert decision.evidence_quality == "weak"

    run_row = conn.execute("SELECT status FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run_row["status"] == "completed"

    resolver_step = conn.execute(
        "SELECT error FROM run_steps WHERE run_id = ? AND probe_name = 'resolve_posting'",
        (result.run_id,),
    ).fetchone()
    assert resolver_step is not None
    assert resolver_step["error"]


@respx.mock
def test_system_a_runs_zero_dynamic_probes_for_unresolved_identity(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """A `generic` posting with no resolvable identity still yields a valid Decision."""
    respx.get("https://careers.acme.com/jobs/1").mock(
        return_value=httpx.Response(200, text=NO_JSONLD_PAGE)
    )

    result = run_system_a(
        conn,
        cfg,
        "https://careers.acme.com/jobs/1",
        now=NOW,
        sleep=lambda _s: None,
        use_tool_cache=False,
    )

    assert result.probes_run == ()
    assert isinstance(result.decision, Decision)
    assert result.decision.posting_state == "unknown"
