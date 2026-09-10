"""Schema version 3: the replay tables and their migration.

`rli.db.MIGRATIONS[2]` and `rli/db/schema.sql` both carry the DDL for
`replay_datasets` / `replay_cases` / `replay_probe_results` — the first
upgrades an existing version-2 database, the second creates a fresh one — and
both `rli/db/__init__.py` and `rli/db/schema.sql` state in comments that THIS
file is what keeps them from drifting apart. Mirrors
`tests/test_history_migration.py`, which does the same job for version 2.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from rli.db import MIGRATIONS, SCHEMA_VERSION, connect, init_db, schema_version
from rli.models.time import now_utc, to_utc_z

REPLAY_START = "CREATE TABLE IF NOT EXISTS replay_datasets"
REPLAY_TABLES = {"replay_datasets", "replay_cases", "replay_probe_results"}


def _schema_sql() -> str:
    return resources.files("rli.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _normalized_ddl(text: str) -> str:
    """The replay DDL with comments stripped and whitespace collapsed."""
    body = text[text.index(REPLAY_START) :]
    uncommented = "\n".join(line.split("--")[0] for line in body.splitlines())
    return " ".join(uncommented.split())


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _build_v2_database(path: Path) -> None:
    """Create a schema-version-2 database: today's schema minus the replay tables."""
    full = _schema_sql()
    # Cut at the section BANNER, not at the CREATE: the comment block above it
    # already names the tables, and executing it would be harmless but leaving
    # it in makes the "no replay tables here" assertion below meaningless.
    v2_sql = full[: full.index("-- replay_datasets / replay_cases")]
    assert "CREATE TABLE IF NOT EXISTS replay" not in v2_sql

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(v2_sql)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    stamp = to_utc_z(now_utc())
    conn.execute(
        "INSERT INTO companies (company_id, name, website_domain, created_at) VALUES (?,?,?,?)",
        ("acme.com", "Acme", "acme.com", stamp),
    )
    conn.execute(
        """
        INSERT INTO postings (posting_id, company_id, ats, ats_job_id, canonical_url, created_at)
        VALUES (?, ?, 'greenhouse', 'j1', 'https://example.com/j1', ?)
        """,
        ("greenhouse:acme:j1", "acme.com", stamp),
    )
    conn.commit()


def test_fresh_database_is_version_3_with_the_replay_tables(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        assert SCHEMA_VERSION >= 3
        assert schema_version(conn) == SCHEMA_VERSION
        assert REPLAY_TABLES <= _table_names(conn)
    finally:
        conn.close()


def test_replay_probe_results_is_keyed_by_case_probe_and_args(tmp_path: Path) -> None:
    """The primary key is what makes a rebuild replace rather than accumulate."""
    path = tmp_path / "pk.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        keys = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(replay_probe_results)")
            if row["pk"]
        ]
        assert set(keys) == {
            "dataset_id",
            "posting_id",
            "replay_at",
            "probe_name",
            "args_hash",
        }
    finally:
        conn.close()


def test_a_replay_case_cannot_name_a_dataset_that_does_not_exist(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fk.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        stamp = to_utc_z(now_utc())
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO replay_cases
                    (dataset_id, posting_id, replay_at, company_id, canonical_url, built_at)
                VALUES ('ghost', 'greenhouse:acme:1', ?, 'acme.com', 'https://x/1', ?)
                """,
                (stamp, stamp),
            )
    finally:
        conn.close()


def test_a_replay_case_may_name_a_posting_the_corpus_has_not_collected(
    tmp_path: Path,
) -> None:
    """Deliberate: `rli.eval` may not create a `postings` row it did not collect."""
    path = tmp_path / "nofk.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        stamp = to_utc_z(now_utc())
        conn.execute(
            """
            INSERT INTO replay_datasets
                (dataset_id, created_at, split_kind, split_name, grid_step_days,
                 postings, companies, cases)
            VALUES ('ds', ?, 'company', 'dev', 30, 0, 0, 0)
            """,
            (stamp,),
        )
        conn.execute(
            """
            INSERT INTO replay_cases
                (dataset_id, posting_id, replay_at, company_id, canonical_url, built_at)
            VALUES ('ds', 'greenhouse:never:collected', ?, 'acme.com', 'https://x/1', ?)
            """,
            (stamp, stamp),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM replay_cases").fetchone()[0] == 1
    finally:
        conn.close()


def test_version_2_database_upgrades_cleanly_to_version_3(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _build_v2_database(path)

    conn = connect(path)
    try:
        assert schema_version(conn) == 2
        assert not (REPLAY_TABLES & _table_names(conn))
        _seed(conn)
    finally:
        conn.close()

    init_db(path)

    conn = connect(path)
    try:
        assert schema_version(conn) == SCHEMA_VERSION
        assert REPLAY_TABLES <= _table_names(conn)
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
        assert conn.execute("SELECT posting_id FROM postings").fetchone()[0] == "greenhouse:acme:j1"
    finally:
        conn.close()


def test_migration_2_is_registered_and_reaches_the_current_version() -> None:
    assert 2 in MIGRATIONS
    assert set(MIGRATIONS) == set(range(1, SCHEMA_VERSION))
    assert max(MIGRATIONS) + 1 == SCHEMA_VERSION


def test_the_replay_ddl_is_identical_in_both_places() -> None:
    from_schema = _normalized_ddl(_schema_sql())
    from_migration = _normalized_ddl(MIGRATIONS[2])

    assert from_schema == from_migration
    assert from_schema.startswith(REPLAY_START)
    assert "idx_replay_probe_results_lookup" in from_schema
    assert "idx_replay_cases_dataset" in from_schema
