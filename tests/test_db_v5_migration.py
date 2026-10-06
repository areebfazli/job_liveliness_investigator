"""Schema version 5 (`rli.db.MIGRATIONS[4]`).

Version 5 adds `board_snapshot_jobs.last_published` (and moves Ashby's
"last published" date there out of `first_published`), the frozen-split
columns on the replay tables, and widens `runs.system` to accept 'R' — the
last by SQLite's table-rebuild procedure, since a CHECK cannot be altered in
place. Covered: a version-4 database upgrades and a second run is a no-op;
every `runs` row, its index and the foreign keys into it survive; 'R' is
accepted and an unknown system still is not; an interrupted upgrade leaves
version 4 untouched; and the `runs` DDL is the same in `schema.sql` and
`rli.db.RUNS_V5_DDL`.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

import pytest

import rli.db as rli_db
from rli.db import RUNS_V5_DDL, SCHEMA_VERSION, connect, init_db, schema_version

STAMP = "2026-09-01T00:00:00.000000Z"


def _schema_sql() -> str:
    return resources.files("rli.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _cut(sql: str, old: str, new: str) -> str:
    assert old in sql, old
    return sql.replace(old, new)


def _v4_sql() -> str:
    """Today's `schema.sql` minus everything version 5 added: the real DB's shape."""
    sql = _schema_sql()
    sql = _cut(
        sql,
        "CHECK (system IN ('A', 'B', 'C', 'C2', 'R'))",
        "CHECK (system IN ('A', 'B', 'C', 'C2'))",
    )
    sql = _cut(
        sql,
        "    updated_at          TEXT,\n    last_published      TEXT\n",
        "    updated_at          TEXT\n",
    )
    sql = _cut(
        sql,
        "    notes           TEXT,\n"
        "    split_method    TEXT,\n"
        "    split_seed      INTEGER,\n"
        "    split_cutoff    TEXT,\n"
        "    split_validation_cutoff TEXT,\n"
        "    exclude_companies_from TEXT\n",
        "    notes           TEXT\n",
    )
    sql = _cut(
        sql, "    split           TEXT CHECK (split IN ('dev', 'validation', 'test')),\n", ""
    )
    return sql


def _build_v4_database(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_v4_sql())
        conn.execute("PRAGMA user_version = 4")
        conn.execute(
            "INSERT INTO companies (company_id, name, website_domain, created_at) "
            "VALUES ('acme.com', 'Acme', 'acme.com', ?)",
            (STAMP,),
        )
        conn.execute(
            "INSERT INTO postings (posting_id, company_id, ats, canonical_url, created_at) "
            "VALUES ('greenhouse:acme:1', 'acme.com', 'greenhouse', 'https://x/1', ?)",
            (STAMP,),
        )
        for index, system in enumerate(("A", "B", "C", "C2")):
            conn.execute(
                """
                INSERT INTO runs (id, posting_id, input_url, system, mode, replay_at,
                                  config_hash, started_at, status, final_decision,
                                  total_cost_usd, total_latency_ms)
                VALUES (?, 'greenhouse:acme:1', 'https://x/1', ?, 'replay', ?, 'cfg|dataset:d',
                        ?, 'completed', '{"recommended_action": "quick_apply"}', 1.5, 42)
                """,
                (f"run-{index}", system, STAMP, STAMP),
            )
            conn.execute(
                "INSERT INTO run_steps (run_id, step_index, component, decision_type, created_at) "
                "VALUES (?, 1, 'probe', 'probe_run', ?)",
                (f"run-{index}", STAMP),
            )
            conn.execute(
                """
                INSERT INTO evidence (id, run_id, probe, claim_type, value, source_url,
                                      source_quality, available_at, fetched_at)
                VALUES ('e1', ?, 'resolve_posting', 'posting_state', 'open', 'https://x/1',
                        'ats_native', ?, ?)
                """,
                (f"run-{index}", STAMP, STAMP),
            )
        conn.execute(
            "INSERT INTO board_snapshots (company_id, captured_at, source, coverage_status) "
            "VALUES ('acme.com', ?, 'own', 'complete')",
            (STAMP,),
        )
        rows = [
            # Greenhouse: both dates, untouched by the migration.
            (
                "11",
                "https://boards.greenhouse.io/acme/jobs/11",
                "2026-08-01T00:00:00.000000Z",
                "2026-08-20T00:00:00.000000Z",
            ),
            # Ashby as version 4 wrote it: publishedAt in first_published.
            ("ab-1", "https://jobs.ashbyhq.com/acme/ab-1", "2026-08-15T00:00:00.000000Z", None),
            # Lever: no dates.
            ("lv-1", "https://jobs.lever.co/acme/lv-1", None, None),
        ]
        conn.executemany(
            "INSERT INTO board_snapshot_jobs (board_snapshot_id, job_id, url, first_published, "
            "updated_at) VALUES (1, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO replay_datasets (dataset_id, created_at, split_kind, split_name, "
            "grid_step_days, postings, companies, cases) "
            "VALUES ('d', ?, 'temporal', 'dev', 7, 1, 1, 1)",
            (STAMP,),
        )
        conn.execute(
            "INSERT INTO replay_cases (dataset_id, posting_id, replay_at, company_id, "
            "canonical_url, built_at) VALUES ('d', 'greenhouse:acme:1', ?, 'acme.com', "
            "'https://x/1', ?)",
            (STAMP, STAMP),
        )
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _runs(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")]


def _runs_sql(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT sql FROM sqlite_master WHERE name = 'runs'").fetchone()[0]


def test_version_4_upgrades_to_5_and_a_second_run_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite3"
    _build_v4_database(path)
    conn = connect(path)
    try:
        assert schema_version(conn) == 4
        before = _runs(conn)
        assert "'R'" not in _runs_sql(conn)
        assert "last_published" not in _columns(conn, "board_snapshot_jobs")
    finally:
        conn.close()

    init_db(path)
    conn = connect(path)
    try:
        after_first = (_runs(conn), _runs_sql(conn))
    finally:
        conn.close()
    init_db(path)  # idempotent: neither fails nor changes anything

    conn = connect(path)
    try:
        assert SCHEMA_VERSION == 5
        assert schema_version(conn) == 5
        # Every runs row survives the rebuild, byte for byte, and only once.
        assert _runs(conn) == before
        assert (_runs(conn), _runs_sql(conn)) == after_first
        assert "'R'" in _runs_sql(conn)
        assert "runs_v5_new" not in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master")
        }
        # The index is recreated; children still reference the rebuilt table.
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(runs)")}
        assert "idx_runs_posting_id" in indexes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM run_steps").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 4
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        # New columns.
        assert "last_published" in _columns(conn, "board_snapshot_jobs")
        assert {
            "split_method",
            "split_seed",
            "split_cutoff",
            "split_validation_cutoff",
            "exclude_companies_from",
        } <= _columns(conn, "replay_datasets")
        assert "split" in _columns(conn, "replay_cases")
        assert conn.execute("SELECT split FROM replay_cases").fetchone()[0] is None

        # Ashby's "last published" date moved out of first_published; nothing else did.
        jobs = {
            row["job_id"]: (row["first_published"], row["updated_at"], row["last_published"])
            for row in conn.execute("SELECT * FROM board_snapshot_jobs")
        }
        assert jobs == {
            "11": ("2026-08-01T00:00:00.000000Z", "2026-08-20T00:00:00.000000Z", None),
            "ab-1": (None, None, "2026-08-15T00:00:00.000000Z"),
            "lv-1": (None, None, None),
        }
    finally:
        conn.close()


def test_runs_accepts_r_after_the_upgrade_and_still_rejects_unknown_systems(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v4.sqlite3"
    _build_v4_database(path)
    init_db(path)
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO runs (id, input_url, system, mode, started_at, status) "
            "VALUES ('run-r', 'https://x/1', 'R', 'replay', ?, 'completed')",
            (STAMP,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (id, input_url, system, mode, started_at, status) "
                "VALUES ('run-x', 'https://x/1', 'X', 'replay', ?, 'completed')",
                (STAMP,),
            )
        # Foreign keys into the rebuilt table are still enforced.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO run_steps (run_id, step_index, component, decision_type, "
                "created_at) VALUES ('no-such-run', 1, 'probe', 'probe_run', ?)",
                (STAMP,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE replay_cases SET split = 'holdout' WHERE dataset_id = 'd'")
        conn.commit()
    finally:
        conn.close()


def test_a_fresh_database_accepts_r(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        assert schema_version(conn) == SCHEMA_VERSION
        conn.execute(
            "INSERT INTO runs (id, input_url, system, mode, started_at, status) "
            "VALUES ('run-r', 'https://x/1', 'R', 'live', ?, 'completed')",
            (STAMP,),
        )
        conn.commit()
    finally:
        conn.close()


def test_an_interrupted_upgrade_leaves_version_4_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v4.sqlite3"
    _build_v4_database(path)

    def boom(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("simulated crash mid-rebuild")

    monkeypatch.setattr(rli_db, "_rebuild_runs_for_r", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        init_db(path)

    conn = connect(path)
    try:
        assert schema_version(conn) == 4
        assert "last_published" not in _columns(conn, "board_snapshot_jobs")
        assert "split" not in _columns(conn, "replay_cases")
        assert "'R'" not in _runs_sql(conn)
        ashby = conn.execute(
            "SELECT first_published FROM board_snapshot_jobs WHERE job_id = 'ab-1'"
        ).fetchone()
        assert ashby[0] == "2026-08-15T00:00:00.000000Z"
    finally:
        conn.close()

    monkeypatch.undo()
    init_db(path)  # and a clean retry completes it
    conn = connect(path)
    try:
        assert schema_version(conn) == 5
        assert len(_runs(conn)) == 4
    finally:
        conn.close()


def _squashed(ddl: str) -> str:
    """DDL with comments and ALL whitespace removed (layout is not schema)."""
    uncommented = "\n".join(line.split("--")[0] for line in ddl.splitlines())
    return re.sub(r"\s+", "", uncommented)


def test_the_runs_ddl_is_identical_in_schema_sql_and_the_migration() -> None:
    sql = _schema_sql()
    start = sql.index("CREATE TABLE IF NOT EXISTS runs (")
    end = sql.index("\n);", start) + 2  # through the closing parenthesis
    assert _squashed(sql[start:end]) == _squashed(RUNS_V5_DDL.format(name="runs"))
