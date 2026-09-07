"""Tests for the rli command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from rli.cli import app
from rli.db import connect

runner = CliRunner()

EXPECTED_TABLES = {
    "companies",
    "postings",
    "posting_snapshots",
    "board_snapshots",
    "board_snapshot_jobs",
    "capture_attempts",
    "company_events",
    "evidence",
    "runs",
    "run_steps",
    "outcomes",
    "llm_cache",
    "tool_cache",
    # schema version 2 (rli.history.matching audit trail)
    "repost_links",
}


def _table_names(db_path: Path) -> set[str]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row["name"] for row in rows}
    finally:
        conn.close()


def test_init_db_command_creates_database(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    result = runner.invoke(app, ["init-db", "--path", str(db_path)])

    assert result.exit_code == 0
    assert str(db_path) in result.stdout
    assert db_path.exists()
    assert _table_names(db_path) == EXPECTED_TABLES


def test_init_db_command_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"

    first = runner.invoke(app, ["init-db", "--path", str(db_path)])
    second = runner.invoke(app, ["init-db", "--path", str(db_path)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert _table_names(db_path) == EXPECTED_TABLES


def test_help_exits_zero_and_mentions_init_db() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init-db" in result.stdout


TARGETS_CSV_BODY = (
    "company_name,website_domain,ats,tenant,open_job_count,checked_at\n"
    "Acme,acme.com,greenhouse,acme,10,2026-01-01T00:00:00Z\n"
    "Beta,beta.io,ashby,beta,5,2026-01-01T00:00:00Z\n"
)

GH_BOARD = {
    "jobs": [
        {
            "id": 1,
            "title": "Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "Remote"}],
            "content": "desc",
        }
    ]
}


def test_load_targets_command_populates_companies_table(tmp_path: Path) -> None:
    csv_path = tmp_path / "targets.csv"
    csv_path.write_text(TARGETS_CSV_BODY, encoding="utf-8")
    db_path = tmp_path / "rli.db"

    result = runner.invoke(
        app, ["load-targets", "--targets", str(csv_path), "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.stdout
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT company_id, name FROM companies ORDER BY company_id").fetchall()
    finally:
        conn.close()
    assert [r["company_id"] for r in rows] == ["acme.com", "beta.io"]
    assert rows[0]["name"] == "Acme"


def test_snapshot_command_runs_capture_and_prints_summary(tmp_path: Path) -> None:
    csv_path = tmp_path / "targets.csv"
    csv_path.write_text(
        "company_name,website_domain,ats,tenant,open_job_count,checked_at\n"
        "Acme,acme.com,greenhouse,acme,10,2026-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "rli.db"

    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json=GH_BOARD)
        )
        result = runner.invoke(
            app, ["snapshot", "--targets", str(csv_path), "--db", str(db_path)]
        )

    assert result.exit_code == 0, result.stdout
    assert "companies: ok=1" in result.stdout
    assert "postings: new=1" in result.stdout

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM board_snapshots").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"] == 1
    finally:
        conn.close()


def test_snapshot_command_only_filters_by_tenant(tmp_path: Path) -> None:
    csv_path = tmp_path / "targets.csv"
    csv_path.write_text(TARGETS_CSV_BODY, encoding="utf-8")
    db_path = tmp_path / "rli.db"

    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json=GH_BOARD)
        )
        result = runner.invoke(
            app,
            [
                "snapshot",
                "--targets",
                str(csv_path),
                "--db",
                str(db_path),
                "--only",
                "acme",
            ],
        )

    assert result.exit_code == 0, result.stdout
    conn = connect(db_path)
    try:
        # Only "acme" was captured; "beta" is upserted as a company (both
        # targets pass through upsert_companies unfiltered by --only... no:
        # snapshot filters targets by --only before upsert_companies too).
        captured_companies = {
            r["company_id"]
            for r in conn.execute("SELECT company_id FROM board_snapshots").fetchall()
        }
    finally:
        conn.close()
    assert captured_companies == {"acme.com"}


def test_snapshot_status_command_on_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"
    result = runner.invoke(app, ["snapshot-status", "--db", str(db_path)])
    assert result.exit_code == 0, result.stdout
    assert "coverage gaps" in result.stdout


def test_snapshot_status_command_after_snapshot_run(tmp_path: Path) -> None:
    csv_path = tmp_path / "targets.csv"
    csv_path.write_text(
        "company_name,website_domain,ats,tenant,open_job_count,checked_at\n"
        "Acme,acme.com,greenhouse,acme,10,2026-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "rli.db"

    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json=GH_BOARD)
        )
        runner.invoke(app, ["snapshot", "--targets", str(csv_path), "--db", str(db_path)])

    result = runner.invoke(app, ["snapshot-status", "--db", str(db_path)])
    assert result.exit_code == 0, result.stdout
    assert "acme.com" in result.stdout
    assert "coverage_status=complete" in result.stdout
    assert "coverage gaps (failed capture_attempts): 0" in result.stdout


# ---------------------------------------------------------------------------
# `rli run` / `rli runs-summary` (rli.eval; spec.md §1/§6)
# ---------------------------------------------------------------------------

RUN_CLI_JOB = {
    "id": 42,
    "title": "CLI Backend Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
    "content": "desc",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Remote"}],
}
RUN_CLI_URL = "https://boards.greenhouse.io/acme/jobs/42"

# spec.md §1's exact output shape and field order: `Decision.model_dump`
# follows declaration order in `rli.models.decision.Decision`.
EXPECTED_DECISION_KEYS = [
    "posting_state",
    "recommended_action",
    "recheck_after_days",
    "evidence_quality",
    "hypotheses",
    "reason",
    "evidence",
]


def _mock_run_cli_greenhouse() -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/42").mock(
        return_value=httpx.Response(200, json=RUN_CLI_JOB)
    )
    respx.get(RUN_CLI_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [RUN_CLI_JOB]})
    )


def test_run_command_prints_decision_json_with_spec_key_order(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"

    with respx.mock:
        _mock_run_cli_greenhouse()
        result = runner.invoke(
            app, ["run", "--system", "A", "--url", RUN_CLI_URL, "--db", str(db_path)]
        )

    assert result.exit_code == 0, result.output
    # CliRunner may merge stderr (the "run_id=..." trace line) into the same
    # stream as stdout depending on the click/typer version, so the decision
    # JSON is not necessarily the whole output — parse it from the first
    # "{" onward rather than assuming stdout is pure JSON. This still pins
    # the field ORDER spec.md §1 requires, which is the point of the test.
    start = result.output.index("{")
    payload = json.loads(result.output[start:])
    assert list(payload.keys()) == EXPECTED_DECISION_KEYS


def test_run_command_bogus_system_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"

    result = runner.invoke(
        app, ["run", "--system", "bogus", "--url", RUN_CLI_URL, "--db", str(db_path)]
    )

    assert result.exit_code != 0


def test_runs_summary_command_prints_action_distribution_after_a_run(tmp_path: Path) -> None:
    db_path = tmp_path / "rli.db"

    with respx.mock:
        _mock_run_cli_greenhouse()
        run_result = runner.invoke(
            app, ["run", "--system", "A", "--url", RUN_CLI_URL, "--db", str(db_path)]
        )
    assert run_result.exit_code == 0, run_result.output

    summary_result = runner.invoke(app, ["runs-summary", "--system", "A", "--db", str(db_path)])

    assert summary_result.exit_code == 0, summary_result.output
    assert "actions:" in summary_result.stdout
    start = run_result.output.index("{")
    decision_payload = json.loads(run_result.output[start:])
    assert decision_payload["recommended_action"] in summary_result.stdout
