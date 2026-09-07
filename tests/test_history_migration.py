"""Tests for the schema-version-2 `repost_links` table and its migration."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from rli.db import MIGRATIONS, SCHEMA_VERSION, connect, init_db, schema_version
from rli.models.time import now_utc, to_utc_z

REPOST_LINKS_START = "CREATE TABLE IF NOT EXISTS repost_links"


def _schema_sql() -> str:
    return resources.files("rli.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _schema_ext_sql() -> str:
    return resources.files("rli.history").joinpath("schema_ext.sql").read_text(encoding="utf-8")


def _normalized_ddl(text: str) -> str:
    """The repost_links DDL with comments stripped and whitespace collapsed."""
    body = text[text.index(REPOST_LINKS_START) :]
    # Later schema versions append further tables after repost_links; stop at the
    # next CREATE TABLE so the comparison covers only the repost_links DDL.
    nxt = body.find("CREATE TABLE", len(REPOST_LINKS_START))
    if nxt != -1:
        body = body[:nxt]
    uncommented = "\n".join(line.split("--")[0] for line in body.splitlines())
    return " ".join(uncommented.split())


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _build_v1_database(path: Path) -> None:
    """Create a schema-version-1 database: today's schema minus repost_links."""
    full = _schema_sql()
    v1_sql = full[: full.index(REPOST_LINKS_START)]
    assert REPOST_LINKS_START not in v1_sql

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(v1_sql)
        conn.execute("PRAGMA user_version = 1")
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


# ---------------------------------------------------------------------------
# Fresh database
# ---------------------------------------------------------------------------


def test_fresh_database_is_version_2_with_repost_links(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    init_db(path)

    conn = connect(path)
    try:
        assert SCHEMA_VERSION >= 2
        assert schema_version(conn) == SCHEMA_VERSION
        assert "repost_links" in _table_names(conn)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(repost_links)")
        }
        assert columns == {
            "id",
            "company_id",
            "old_posting_id",
            "new_posting_id",
            "combined_score",
            "component_scores",
            "matched_at",
        }
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(repost_links)")}
        assert "idx_repost_links_company_id" in indexes
    finally:
        conn.close()


def test_repost_links_enforces_one_row_per_old_new_pair(tmp_path: Path) -> None:
    path = tmp_path / "unique.sqlite3"
    init_db(path)
    conn = connect(path)
    try:
        _seed(conn)
        conn.execute(
            """
            INSERT INTO postings
                (posting_id, company_id, ats, ats_job_id, canonical_url, created_at)
            VALUES ('greenhouse:acme:j2', 'acme.com', 'greenhouse', 'j2', 'https://e/2', ?)
            """,
            (to_utc_z(now_utc()),),
        )
        stamp = to_utc_z(now_utc())
        row = ("acme.com", "greenhouse:acme:j1", "greenhouse:acme:j2", 0.9, "{}", stamp)
        statement = """
            INSERT INTO repost_links
                (company_id, old_posting_id, new_posting_id, combined_score,
                 component_scores, matched_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        conn.execute(statement, row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Upgrading an existing version-1 database
# ---------------------------------------------------------------------------


def test_version_1_database_upgrades_cleanly_to_version_2(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _build_v1_database(path)

    conn = connect(path)
    try:
        assert schema_version(conn) == 1
        assert "repost_links" not in _table_names(conn)
        _seed(conn)
    finally:
        conn.close()

    init_db(path)

    conn = connect(path)
    try:
        assert schema_version(conn) == SCHEMA_VERSION
        assert "repost_links" in _table_names(conn)
        # Pre-existing data survives the upgrade untouched.
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
        assert (
            conn.execute("SELECT posting_id FROM postings").fetchone()[0]
            == "greenhouse:acme:j1"
        )
    finally:
        conn.close()


def test_repeated_init_db_on_a_populated_database_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "twice.sqlite3"
    init_db(path)

    conn = connect(path)
    try:
        _seed(conn)
    finally:
        conn.close()

    init_db(path)
    init_db(path)

    conn = connect(path)
    try:
        assert schema_version(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_1_is_registered_and_reaches_the_current_version() -> None:
    assert 1 in MIGRATIONS
    assert set(MIGRATIONS) == set(range(1, SCHEMA_VERSION))
    assert max(MIGRATIONS) + 1 == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# The three copies of the DDL must not drift apart
# ---------------------------------------------------------------------------


def test_repost_links_ddl_is_identical_in_all_three_places() -> None:
    from_schema = _normalized_ddl(_schema_sql())
    from_ext = _normalized_ddl(_schema_ext_sql())
    from_migration = _normalized_ddl(MIGRATIONS[1])

    assert from_schema == from_ext == from_migration
    assert from_schema.startswith(REPOST_LINKS_START)
    assert "idx_repost_links_company_id" in from_schema
