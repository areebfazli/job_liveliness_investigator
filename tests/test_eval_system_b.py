"""System B end-to-end (spec.md §6 "B — Rules"; rli.eval.system_b).

Two distinct corpora exercise the two interesting non-terminal branches:
`R2_healthy` (recent publish + strong always-run evidence -> `company_events`
only) and `R1_repost_suspicious` (a stale publish date -> the repost probes).
A third corpus exercises `R0_terminal` via a resolver failure, mirroring
`tests/test_eval_system_a.py`'s failure case.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx
from test_eval_helpers import (
    COMPANY,
    add_capture,
    add_company_event,
    add_posting,
    decision_branch,
    job,
    write_status_csv,
)

from rli.config import Config
from rli.eval.runner import STEP_ROUTE, STEP_SYSTEM_VERSION
from rli.eval.system_a import run_system_a
from rli.eval.system_b import (
    B_VERSION,
    RouteDecision,
    b_config_hash,
    b_rules_hash,
    route_detail,
    run_system_b,
)
from rli.models.decision import Decision
from rli.models.policy_inputs import PolicyInputs
from rli.policy.quality import QualityVerdict

NOW = datetime(2026, 9, 7, tzinfo=UTC)
NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"


def _gh_job(job_id: str, *, first_published_days_ago: int) -> dict:
    return {
        "id": int(job_id),
        "title": "Backend Engineer",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "first_published": (NOW - timedelta(days=first_published_days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "content": "<p>Build things.</p>",
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Remote"}],
    }


def _seed_history(conn: sqlite3.Connection, job_id: str) -> None:
    """50-day board history: usable (>=30) but not long-lived (<180)."""
    add_posting(
        conn,
        job_id=job_id,
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    for offset in (60, 45, 30, 10):
        add_capture(conn, NOW - timedelta(days=offset), [job(job_id)], company_id=COMPANY)


def _mock_greenhouse(job_id: str, gh_job: dict) -> None:
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/acme/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json=gh_job)
    )
    respx.get(url).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [gh_job]})
    )


@respx.mock
def test_system_b_fires_r2_healthy_and_runs_only_company_events(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    job_id = "7001"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    gh_job = _gh_job(job_id, first_published_days_ago=5)  # recent
    _mock_greenhouse(job_id, gh_job)

    result = run_system_b(conn, cfg, url, now=NOW, sleep=lambda _s: None, use_tool_cache=False)

    assert result.route_rule == "R2_healthy"
    assert result.probes_run == ("company_events",)

    run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run_row["system"] == "B"
    assert run_row["config_hash"] == b_config_hash(cfg)
    assert "|b1:" in run_row["config_hash"]

    steps = conn.execute(
        "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index", (result.run_id,)
    ).fetchall()
    version_step = next(s for s in steps if s["decision_type"] == STEP_SYSTEM_VERSION)
    assert version_step["component"] == "controller"
    assert version_step["args_hash"] == b_rules_hash(cfg)

    route_step = next(s for s in steps if s["decision_type"].startswith(f"{STEP_ROUTE}:"))
    assert route_step["decision_type"] == f"{STEP_ROUTE}:R2_healthy"

    # -- structural assertions mirroring System A's end-to-end test ---------
    indices = [s["step_index"] for s in steps]
    assert indices == list(range(1, len(steps) + 1))
    assert len(set(indices)) == len(indices)

    evidence_rows = conn.execute(
        "SELECT id FROM evidence WHERE run_id = ? ORDER BY id", (result.run_id,)
    ).fetchall()
    ids = [r["id"] for r in evidence_rows]
    assert ids == [f"e{i}" for i in range(1, len(ids) + 1)]

    claim_types = {
        row["claim_type"]
        for row in conn.execute(
            "SELECT DISTINCT claim_type FROM evidence WHERE run_id = ?", (result.run_id,)
        ).fetchall()
    }
    assert "posting_state" in claim_types
    assert ("board_present" in claim_types) or ("board_absent" in claim_types)

    decision = Decision.model_validate(json.loads(run_row["final_decision"]))
    decision_evidence_ids = {item.id for item in decision.evidence}
    evidence_ids_in_table = set(ids)
    for reason in decision.reason:
        for eid in reason.evidence_ids:
            assert eid in decision_evidence_ids
            assert eid in evidence_ids_in_table

    # -- B's routed set is a subset of what A runs on the identical case ----
    result_a = run_system_a(conn, cfg, url, now=NOW, sleep=lambda _s: None, use_tool_cache=False)
    assert set(result.probes_run) <= set(result_a.probes_run)


@respx.mock
def test_system_b_fires_r1_repost_suspicious_on_a_stale_publish_date(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    job_id = "7002"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    gh_job = _gh_job(job_id, first_published_days_ago=40)  # not recent (> 14 days)
    _mock_greenhouse(job_id, gh_job)

    result = run_system_b(conn, cfg, url, now=NOW, sleep=lambda _s: None, use_tool_cache=False)

    assert result.route_rule == "R1_repost_suspicious"
    assert set(result.probes_run) == {"repost_history", "requirements_drift", "company_events"}

    steps = conn.execute(
        "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index", (result.run_id,)
    ).fetchall()
    route_step = next(s for s in steps if s["decision_type"].startswith(f"{STEP_ROUTE}:"))
    assert route_step["decision_type"] == f"{STEP_ROUTE}:R1_repost_suspicious"

    result_a = run_system_a(conn, cfg, url, now=NOW, sleep=lambda _s: None, use_tool_cache=False)
    assert set(result.probes_run) <= set(result_a.probes_run)


@respx.mock
def test_system_a_and_b_agree_on_action_and_posting_state_for_identical_inputs(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    """spec.md §6's action-agreement property, on a fixture engineered to hold it.

    This equality is only guaranteed when the probes B skips would not have
    changed the policy inputs A also sees. On the `R2_healthy` corpus the
    probes A runs beyond B (`repost_history`, `requirements_drift`) emit no
    claims `rli.policy.inputs` reads (the job is never absent from a
    capture, so `repost_history` returns no claims at all, and
    `requirements_changed`/`unchanged` feeds no `PolicyInputs` field) — so
    this is a genuine agreement, not a coincidence of a thin fixture. If a
    corpus ever made A and B diverge here, that would be a real finding to
    report, not a reason to weaken this assertion.
    """
    job_id = "7003"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
    _seed_history(conn, job_id)
    gh_job = _gh_job(job_id, first_published_days_ago=5)
    _mock_greenhouse(job_id, gh_job)

    result_a = run_system_a(conn, cfg, url, now=NOW, sleep=lambda _s: None, use_tool_cache=False)
    result_b = run_system_b(conn, cfg, url, now=NOW, sleep=lambda _s: None, use_tool_cache=False)

    assert result_a.decision.recommended_action == result_b.decision.recommended_action
    assert result_a.decision.posting_state == result_b.decision.posting_state
    # The interesting extra probes A ran beyond B's routed set.
    assert set(result_b.probes_run) < set(result_a.probes_run)


@respx.mock
def test_system_b_terminal_route_on_resolver_failure(conn: sqlite3.Connection, cfg: Config) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/9999").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards.greenhouse.io/acme/jobs/9999").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    result = run_system_b(
        conn,
        cfg,
        "https://boards.greenhouse.io/acme/jobs/9999",
        now=NOW,
        sleep=lambda _s: None,
        use_tool_cache=False,
    )

    assert result.route_rule == "R0_terminal"
    assert result.probes_run == ()
    assert result.decision.posting_state == "unknown"
    assert result.decision.recommended_action == "wait"

    run_row = conn.execute("SELECT status FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run_row["status"] == "completed"


# ---------------------------------------------------------------------------
# Unit tests: routing purity, versioning
# ---------------------------------------------------------------------------


def _case_state(**overrides):
    from rli.eval.case import CaseState

    defaults = dict(
        input_url="https://example.com/1",
        canonical_url="https://example.com/1",
        inputs=PolicyInputs(posting_state="open", publish_recency="recent"),
        quality=QualityVerdict(quality="strong", rule="strong", detail="d"),
        features=None,
    )
    defaults.update(overrides)
    return CaseState(**defaults)


def test_route_detail_is_pure_and_deterministic(cfg: Config) -> None:
    case = _case_state()
    first = route_detail(case, cfg)
    second = route_detail(case, cfg)
    assert first == second
    assert isinstance(first, RouteDecision)
    assert first.rule == "R2_healthy"


def test_route_detail_r0_terminal_for_closed_or_unknown(cfg: Config) -> None:
    closed = _case_state(inputs=PolicyInputs(posting_state="closed"))
    assert route_detail(closed, cfg).rule == "R0_terminal"
    assert route_detail(closed, cfg).probes == ()

    unresolved = _case_state(inputs=PolicyInputs())  # posting_state stays UNKNOWN
    assert route_detail(unresolved, cfg).rule == "R0_terminal"


def test_b_rules_hash_is_stable_and_depends_on_max_dynamic_steps(cfg: Config) -> None:
    assert b_rules_hash(cfg) == b_rules_hash(cfg)

    changed = cfg.model_copy(
        update={
            "thresholds": cfg.thresholds.model_copy(
                update={"max_dynamic_steps": cfg.thresholds.max_dynamic_steps + 1}
            )
        }
    )
    assert b_rules_hash(cfg) != b_rules_hash(changed)


def test_b_rules_hash_depends_on_team_signal_enabled(cfg: Config) -> None:
    # `[team_signal].enabled` now defaults to `True`, so the contrasting
    # config is built by flipping it to `False` instead.
    changed = cfg.model_copy(
        update={"team_signal": cfg.team_signal.model_copy(update={"enabled": False})}
    )
    assert b_rules_hash(cfg) != b_rules_hash(changed)


def test_b_version_label_is_frozen_string() -> None:
    assert B_VERSION == "b1"


# ---------------------------------------------------------------------------
# spec.md §5 Amendment 2026-09-10: P3c, end to end and evidence-cited
# ---------------------------------------------------------------------------


@respx.mock
def test_system_b_waits_on_a_material_layoff_after_the_last_refresh(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """The same P3c scenario `tests/test_eval_system_a.py` runs, through B.

    B reaches `company_events` by ROUTING it by name rather than through the
    registry's unresolved-question gate, so this is the check that B's route
    still delivers the evidence the P3c `wait` is explained by — and, since B
    and A share one frozen policy (spec.md §6), that both systems land on the
    same branch for the same corpus.

    The layoff ids must appear among the decision's citations. That is the
    assertion that would have failed before the events pipeline moved onto
    claims: the probe ran, its `company_events` record contributed nothing
    the policy read, and the `wait` came from an input nobody could cite.
    """
    job_id = "7900"
    url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"

    add_posting(
        conn,
        job_id=job_id,
        first_observed=NOW - timedelta(days=520),
        last_seen_open=NOW,
    )
    for offset in (520, 300, 120, 40, 2):
        add_capture(conn, NOW - timedelta(days=offset), [job(job_id)], company_id=COMPANY)
    add_company_event(
        conn,
        event_date=(NOW - timedelta(days=18)).date(),
        available_at=NOW - timedelta(days=18),
    )
    status_csv = write_status_csv(tmp_path / "collection_status.csv", NOW - timedelta(days=1))

    _mock_greenhouse(job_id, _gh_job(job_id, first_published_days_ago=520))

    result = run_system_b(
        conn,
        cfg,
        url,
        now=NOW,
        sleep=lambda _s: None,
        use_tool_cache=False,
        collection_status_csv=status_csv,
    )

    assert "company_events" in result.probes_run
    assert result.decision.recommended_action == "wait"
    assert decision_branch(conn, result.run_id) == "P3c_material_event_unrefreshed"

    layoff_ids = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM evidence WHERE run_id = ? AND probe = 'company_events' "
            "AND claim_type = 'layoff'",
            (result.run_id,),
        ).fetchall()
    }
    assert layoff_ids
    cited = {eid for reason in result.decision.reason for eid in reason.evidence_ids}
    assert layoff_ids <= cited
