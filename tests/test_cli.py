"""Tests for the rli command-line interface."""

from __future__ import annotations

from pathlib import Path

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
