"""`rli.eval.report`: `summarize_runs` / `cost_tier_for` (spec.md §6 baseline figures).

`summarize_runs` is a pure reader of `runs` + `run_steps`; this file drives
it with a mix of real System A/B runs (via respx-mocked HTTP) and a bare,
hand-inserted run with a NULL `final_decision`, to exercise the tolerance
`summarize_runs` documents for a run that failed before deciding.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import respx
from test_eval_helpers import COMPANY, add_capture, add_posting, job

from rli.config import Config
from rli.eval.report import cost_tier_for, summarize_runs
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.models.time import to_utc_z

NOW = datetime(2026, 9, 7, tzinfo=UTC)
NO_JSONLD_PAGE = "<html><body>no structured data</body></html>"

GH_JOB = {
    "id": 8001,
    "title": "Backend Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/8001",
    "first_published": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "content": "<p>Build things.</p>",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Remote"}],
}
URL = "https://boards.greenhouse.io/acme/jobs/8001"


def _seed(conn: sqlite3.Connection) -> None:
    add_posting(
        conn,
        job_id="8001",
        first_observed=NOW - timedelta(days=60),
        last_seen_open=NOW - timedelta(days=10),
    )
    for offset in (60, 45, 30, 10):
        add_capture(conn, NOW - timedelta(days=offset), [job("8001")], company_id=COMPANY)


def _mock() -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/8001").mock(
        return_value=httpx.Response(200, json=GH_JOB)
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text=NO_JSONLD_PAGE))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [GH_JOB]})
    )


def _insert_bare_failed_run(conn: sqlite3.Connection, run_id: str, *, system: str) -> None:
    """A run that crashed before ever reaching `decide_and_finish`.

    `final_decision` is NULL and `status='failed'` — the case
    `summarize_runs` must count without raising (module docstring: "A run
    with no `final_decision` is counted, not skipped").
    """
    conn.execute(
        """
        INSERT INTO runs (id, input_url, system, mode, started_at, status,
                           total_cost_usd, total_latency_ms)
        VALUES (?, ?, ?, 'live', ?, 'failed', 1.0, 100)
        """,
        (run_id, "https://example.com/bare", system, to_utc_z(NOW)),
    )
    conn.execute(
        """
        INSERT INTO run_steps (run_id, step_index, component, decision_type, probe_name,
                                cost_usd, error, created_at)
        VALUES (?, 1, 'probe', 'probe_run', 'resolve_posting', 1.0, 'ConnectError: boom', ?)
        """,
        (run_id, to_utc_z(NOW)),
    )
    conn.commit()


@respx.mock
def test_summarize_runs_over_a_and_b_reports_counts_and_distributions(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _seed(conn)
    _mock()

    result_a = run_system_a(conn, cfg, URL, now=NOW, sleep=lambda _s: None, use_tool_cache=False)
    result_b = run_system_b(conn, cfg, URL, now=NOW, sleep=lambda _s: None, use_tool_cache=False)
    _insert_bare_failed_run(conn, "bare-a", system="A")

    summary_a = summarize_runs(conn, "A")
    assert summary_a.runs == 2
    assert summary_a.completed == 1
    assert summary_a.failed == 1
    assert summary_a.decisions_missing == 1
    assert summary_a.action_distribution.get(result_a.decision.recommended_action, 0) >= 1
    assert summary_a.evidence_quality_distribution.get(result_a.decision.evidence_quality, 0) >= 1
    # The bare run's resolve_posting step is a genuine probe execution with
    # an error: it counts as both a probe step and a failure.
    assert summary_a.probe_counts.get("resolve_posting", 0) >= 2  # real run + bare run
    assert summary_a.failure_counts.get("resolve_posting", 0) == 1
    assert summary_a.failed_steps >= 1
    assert "system A" in summary_a.describe()
    assert "NOTE:" in summary_a.describe()  # decisions_missing is called out

    summary_b = summarize_runs(conn, "B")
    assert summary_b.runs == 1
    assert summary_b.completed == 1
    assert summary_b.decisions_missing == 0
    assert result_b.decision.recommended_action in summary_b.action_distribution

    # Probe-count-by-tier includes the always-run pair (both 'low') and
    # whatever dynamic probes each system actually ran.
    assert summary_a.probe_counts_by_tier.get("low", 0) >= 1
    # `[team_signal].enabled` now defaults to `True` (spec.md §5's Amendment
    # 2026-09-10), and this corpus's 50 days of history clears
    # `min_history_days`, so System A's full-probe run also executes the
    # `high`-tier `team_signal` probe alongside the two `medium`-tier probes.
    assert summary_a.medium_high_probe_steps == sum(
        summary_a.probe_counts.get(name, 0)
        for name in ("company_events", "requirements_drift", "team_signal")
    )


def test_summarize_runs_on_empty_system_is_all_zero(conn: sqlite3.Connection) -> None:
    summary = summarize_runs(conn, "A")
    assert summary.runs == 0
    assert summary.mean_cost_per_run == 0.0
    assert summary.mean_latency_ms_per_run == 0.0
    assert summary.action_distribution == {}
    assert "(none)" in summary.describe()


def test_summarize_runs_tolerates_unparseable_final_decision(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO runs (id, input_url, system, mode, started_at, status, final_decision)
        VALUES ('corrupt-1', 'https://example.com/x', 'A', 'live', ?, 'completed', 'not json')
        """,
        (to_utc_z(NOW),),
    )
    conn.commit()

    summary = summarize_runs(conn, "A")
    assert summary.runs == 1
    assert summary.decisions_missing == 1


def test_cost_tier_for_known_and_unknown_probes() -> None:
    assert cost_tier_for("resolve_posting") == "low"
    assert cost_tier_for("board_snapshot") == "low"
    assert cost_tier_for("repost_history") == "low"
    assert cost_tier_for("requirements_drift") == "medium"
    assert cost_tier_for("company_events") == "medium"
    assert cost_tier_for("team_signal") == "high"
    assert cost_tier_for("no_such_probe") == "unknown"
    assert cost_tier_for(None) == "unknown"
