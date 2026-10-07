"""Retired replay datasets (schema version 7; `rli.replay.retire`).

Retirement is a reversible stamp on `replay_datasets`: nothing is deleted,
the company-holdout breach check ignores a retired dataset (reporting it as
"retired, ignored"), and `replay run` / `eval run` refuse it unless
explicitly allowed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_run_windows_and_holdout import _case, _dataset, _posting
from typer.testing import CliRunner

from rli.cli import app
from rli.config import Config
from rli.db import SCHEMA_VERSION, connect, init_db, schema_version
from rli.eval.diagnostics import company_holdout_check
from rli.eval.evaluate import evaluate
from rli.models.time import to_utc_z
from rli.policy.splits import stable_company_split_of
from rli.replay.retire import (
    RetiredDatasetError,
    dataset_rows,
    ensure_not_retired,
    retire_dataset,
    retired_datasets,
    retirement_of,
    unretire_dataset,
)
from rli.replay.run import run_replay

runner = CliRunner()
WHEN = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _two_datasets(conn: sqlite3.Connection) -> None:
    """`company-ds` (clean) plus an old `temporal-ds` that used a test company."""
    assert stable_company_split_of("acme.com") == "test"
    assert stable_company_split_of("other.com") == "dev"
    _posting(conn, "pa", "acme.com", "2026-09-01T00:00:00Z")
    _posting(conn, "pb", "other.com", "2026-09-01T00:00:00Z")
    _dataset(conn, "company-ds", "company", "company-hash")
    _dataset(conn, "temporal-ds", "temporal", "temporal")
    _case(conn, "company-ds", "pb", "other.com")
    _case(conn, "temporal-ds", "pa", "acme.com")
    conn.commit()


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM replay_datasets").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM replay_cases").fetchone()[0],
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_fresh_schema_has_the_retired_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(replay_datasets)")}
    assert {"retired_at", "retired_reason"} <= columns
    assert schema_version(conn) == SCHEMA_VERSION == 7


def test_version_6_upgrades_to_7_keeping_data_and_a_second_run_is_a_no_op(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v6.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        _two_datasets(conn)
    finally:
        conn.close()
    raw = sqlite3.connect(str(path))
    try:
        raw.execute("ALTER TABLE replay_datasets DROP COLUMN retired_reason")
        raw.execute("ALTER TABLE replay_datasets DROP COLUMN retired_at")
        raw.execute("PRAGMA user_version = 6")
        raw.commit()
    finally:
        raw.close()

    # A pre-v7 database: nothing is retired, nothing raises.
    conn = connect(path)
    try:
        assert retired_datasets(conn) == {}
        assert retirement_of(conn, "temporal-ds") is None
        assert ensure_not_retired(conn, "temporal-ds") is None
        assert [row["retired_at"] for row in dataset_rows(conn)] == [None, None]
        with pytest.raises(RuntimeError, match="init-db"):
            retire_dataset(conn, "temporal-ds", reason="old")
    finally:
        conn.close()

    init_db(path)
    init_db(path)
    conn = connect(path)
    try:
        assert schema_version(conn) == 7
        columns = {row[1] for row in conn.execute("PRAGMA table_info(replay_datasets)")}
        assert {"retired_at", "retired_reason"} <= columns
        assert _counts(conn) == (2, 2)
        assert retired_datasets(conn) == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# retire / unretire
# ---------------------------------------------------------------------------


def test_retire_and_unretire_are_reversible_and_delete_nothing(conn: sqlite3.Connection) -> None:
    _two_datasets(conn)
    before = _counts(conn)

    retirement = retire_dataset(conn, "temporal-ds", reason="pre-fix build", now=WHEN)
    assert retirement.retired_at == to_utc_z(WHEN)
    assert retirement.reason == "pre-fix build"
    assert set(retired_datasets(conn)) == {"temporal-ds"}
    assert _counts(conn) == before

    # Re-retiring keeps the original stamp and replaces the reason.
    again = retire_dataset(conn, "temporal-ds", reason="holdout breach", now=datetime.now(UTC))
    assert again.retired_at == retirement.retired_at
    assert again.reason == "holdout breach"

    with pytest.raises(RetiredDatasetError, match="--allow-retired"):
        ensure_not_retired(conn, "temporal-ds")
    assert ensure_not_retired(conn, "temporal-ds", allow_retired=True) == again
    assert ensure_not_retired(conn, "company-ds") is None

    assert unretire_dataset(conn, "temporal-ds") == again
    assert retired_datasets(conn) == {}
    assert unretire_dataset(conn, "temporal-ds") is None
    assert _counts(conn) == before


def test_retire_refuses_unknown_datasets_and_empty_reasons(conn: sqlite3.Connection) -> None:
    _two_datasets(conn)
    with pytest.raises(LookupError):
        retire_dataset(conn, "nope", reason="x")
    with pytest.raises(LookupError):
        unretire_dataset(conn, "nope")
    with pytest.raises(ValueError, match="reason"):
        retire_dataset(conn, "temporal-ds", reason="   ")
    assert retired_datasets(conn) == {}


# ---------------------------------------------------------------------------
# the company-holdout breach check
# ---------------------------------------------------------------------------


def test_holdout_check_ignores_a_retired_dataset_and_reports_it(conn: sqlite3.Connection) -> None:
    _two_datasets(conn)
    breached = company_holdout_check(conn, dataset_id="company-ds", split_kind="company")
    assert breached.breaches == {"temporal-ds": {"companies": 1, "cases": 1}}
    assert breached.datasets_checked == 1

    retire_dataset(conn, "temporal-ds", reason="pre-fix build", now=WHEN)
    check = company_holdout_check(conn, dataset_id="company-ds", split_kind="company")
    assert check.clean
    assert check.breaches == {}
    assert check.datasets_checked == 0
    assert check.retired_ignored == {"temporal-ds": {"companies": 1, "cases": 1}}
    text = check.describe()
    assert "CLEAN" in text
    assert "retired, ignored: temporal-ds (1 test companies, 1 cases)" in text

    unretire_dataset(conn, "temporal-ds")
    assert not company_holdout_check(conn, dataset_id="company-ds", split_kind="company").clean


def test_a_retired_dataset_without_overlap_is_still_listed(conn: sqlite3.Connection) -> None:
    _two_datasets(conn)
    _dataset(conn, "clean-old", "temporal", "temporal")
    _case(conn, "clean-old", "pb", "other.com")
    conn.commit()
    retire_dataset(conn, "clean-old", reason="superseded", now=WHEN)
    check = company_holdout_check(conn, dataset_id="company-ds", split_kind="company")
    assert check.retired_ignored == {"clean-old": {"companies": 0, "cases": 0}}
    assert check.datasets_checked == 1  # temporal-ds, still active and breaching
    assert not check.clean


# ---------------------------------------------------------------------------
# replay run / eval run refuse a retired dataset
# ---------------------------------------------------------------------------


def test_run_replay_refuses_a_retired_dataset_unless_allowed(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _two_datasets(conn)
    retire_dataset(conn, "temporal-ds", reason="pre-fix build", now=WHEN)

    with pytest.raises(RetiredDatasetError):
        run_replay(conn, cfg, dataset_id="temporal-ds", system="A")
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    summary = run_replay(
        conn, cfg, dataset_id="temporal-ds", system="A", limit_cases=0, allow_retired=True
    )
    assert summary.cases == 0


def test_evaluate_refuses_a_retired_dataset_unless_allowed(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    _two_datasets(conn)
    retire_dataset(conn, "temporal-ds", reason="pre-fix build", now=WHEN)
    with pytest.raises(RetiredDatasetError):
        evaluate(conn, cfg, dataset_id="temporal-ds", read_only=True, include_survival=False)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_db(tmp_path: Path) -> Path:
    path = tmp_path / "rli.db"
    init_db(path)
    conn = connect(path)
    try:
        _two_datasets(conn)
    finally:
        conn.close()
    return path


def test_cli_retire_list_status_unretire(tmp_path: Path) -> None:
    db = str(_cli_db(tmp_path))

    listed = runner.invoke(app, ["replay", "list", "--db", db])
    assert listed.exit_code == 0, listed.output
    assert "temporal-ds" in listed.output and "RETIRED" not in listed.output

    retired = runner.invoke(
        app, ["replay", "retire", "--dataset", "temporal-ds", "--reason", "pre-fix", "--db", db]
    )
    assert retired.exit_code == 0, retired.output
    assert "retired" in retired.output and "pre-fix" in retired.output

    listed = runner.invoke(app, ["replay", "list", "--db", db])
    line = next(row for row in listed.output.splitlines() if row.startswith("temporal-ds"))
    assert "RETIRED" in line and "pre-fix" in line
    other = next(row for row in listed.output.splitlines() if row.startswith("company-ds"))
    assert other.endswith("active")

    status = runner.invoke(app, ["replay", "status", "--dataset", "temporal-ds", "--db", db])
    assert status.exit_code == 0, status.output
    assert "retired" in status.output

    refused = runner.invoke(
        app, ["replay", "run", "--system", "A", "--dataset", "temporal-ds", "--db", db]
    )
    assert refused.exit_code == 2
    assert "--allow-retired" in refused.output

    refused_eval = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--dataset",
            "temporal-ds",
            "--read-only",
            "--no-survival",
            "--out",
            str(tmp_path / "r.md"),
            "--db",
            db,
        ],
    )
    assert refused_eval.exit_code == 2
    assert not (tmp_path / "r.md").exists()

    allowed = runner.invoke(
        app,
        [
            "replay",
            "run",
            "--system",
            "A",
            "--dataset",
            "temporal-ds",
            "--limit-cases",
            "0",
            "--allow-retired",
            "--db",
            db,
        ],
    )
    assert allowed.exit_code == 0, allowed.output

    back = runner.invoke(app, ["replay", "unretire", "--dataset", "temporal-ds", "--db", db])
    assert back.exit_code == 0 and "active again" in back.output
    again = runner.invoke(app, ["replay", "unretire", "--dataset", "temporal-ds", "--db", db])
    assert again.exit_code == 0 and "was not retired" in again.output


def test_cli_retire_rejects_unknown_dataset_and_blank_reason(tmp_path: Path) -> None:
    db = str(_cli_db(tmp_path))
    unknown = runner.invoke(
        app, ["replay", "retire", "--dataset", "nope", "--reason", "x", "--db", db]
    )
    assert unknown.exit_code == 2
    blank = runner.invoke(
        app, ["replay", "retire", "--dataset", "temporal-ds", "--reason", " ", "--db", db]
    )
    assert blank.exit_code == 2
