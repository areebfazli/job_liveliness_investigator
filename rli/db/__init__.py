"""SQLite connection + schema management for rli.

`init_db` is idempotent: the schema uses `CREATE TABLE IF NOT EXISTS`
throughout, so calling it repeatedly against the same path is safe.

Schema evolution: `schema.sql` always describes the *newest* schema and is
only ever used to create a fresh database. To ship a schema change against
existing databases, bump `SCHEMA_VERSION` and add an entry to `MIGRATIONS`
keyed by the *from* version, e.g. `MIGRATIONS[1] = "ALTER TABLE ..."` to
upgrade a version-1 database to version 2. `init_db` walks `MIGRATIONS` in
ascending order from whatever `PRAGMA user_version` an existing database
reports, up to `SCHEMA_VERSION`.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

__all__ = [
    "BUSY_TIMEOUT_MS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "connect",
    "init_db",
    "schema_version",
]

# How long (ms) a connection blocks on a locked database before giving up.
# Also passed to sqlite3.connect as an equivalent `timeout=` in seconds, so
# both the driver-level busy handler and SQLite's own PRAGMA busy_timeout
# agree.
BUSY_TIMEOUT_MS = 5000

# The schema version this checkout knows how to produce/upgrade to.
# 1 -> 2: added the `repost_links` table (rli.history.matching audit trail).
SCHEMA_VERSION = 2

# from-version -> SQL script that upgrades that version to version + 1.
# `schema.sql` describes only the newest version; every prior jump needed to
# reach it from an older on-disk database lives here instead.
MIGRATIONS: dict[int, str] = {}

# 1 -> 2: `repost_links`, the audit trail for rli.history.matching.link_reposts.
# This DDL is duplicated verbatim in rli/db/schema.sql (so a FRESH database gets
# it from the executescript path above) and in rli/history/schema_ext.sql (the
# owning module's copy); tests/test_history_migration.py asserts the three stay
# in sync. A migration's text is frozen once shipped, which is why it is spelled
# out here rather than read back from either file.
MIGRATIONS[1] = """
CREATE TABLE IF NOT EXISTS repost_links (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          TEXT NOT NULL REFERENCES companies (company_id),
    old_posting_id      TEXT NOT NULL REFERENCES postings (posting_id),
    new_posting_id      TEXT NOT NULL REFERENCES postings (posting_id),
    combined_score      REAL NOT NULL,
    component_scores    TEXT NOT NULL,   -- JSON: per-component scores + pass flags
    matched_at          TEXT NOT NULL,
    UNIQUE (old_posting_id, new_posting_id)
);

CREATE INDEX IF NOT EXISTS idx_repost_links_company_id ON repost_links (company_id);
"""


def _schema_sql() -> str:
    return resources.files("rli.db").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection configured for concurrent, replay-safe use.

    Sets WAL journaling (a no-op for the in-memory `:memory:` database, which
    has no journal file), a busy timeout so concurrent writers block briefly
    instead of raising `sqlite3.OperationalError` immediately, foreign key
    enforcement, and row access by column name.
    """
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version recorded on `conn` via PRAGMA user_version."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA user_version does not accept bound parameters (`?`), so the
    # value must be interpolated directly. `version` always originates from
    # a validated int here (a MIGRATIONS key + 1, or SCHEMA_VERSION), never
    # from external input.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def init_db(path: str | Path) -> None:
    """Create/upgrade the database schema at `path`.

    Safe to call multiple times against the same path: a fresh database is
    created from `schema.sql` (idempotent `CREATE TABLE IF NOT EXISTS`), and
    an existing database is walked forward through `MIGRATIONS` until its
    `PRAGMA user_version` reaches `SCHEMA_VERSION`. Raises `RuntimeError` if
    the on-disk database reports a version newer than this checkout knows
    about (it was written by a newer version of rli).
    """
    path = Path(path)
    if str(path) != ":memory:" and path.parent:
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(path)
    try:
        conn.executescript(_schema_sql())

        version = schema_version(conn)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database at {path!r} has schema version {version}, but this "
                f"version of rli only understands up to {SCHEMA_VERSION} "
                "(the database was written by a newer version of rli)"
            )

        if version == 0:
            # SQLite's PRAGMA user_version defaults to 0 and nothing has ever
            # stamped this database, so the executescript above just created
            # every table fresh from schema.sql — which always describes the
            # newest version. There is no data to migrate; jump straight to
            # SCHEMA_VERSION instead of walking MIGRATIONS from 0.
            _set_schema_version(conn, SCHEMA_VERSION)
            version = SCHEMA_VERSION

        while version < SCHEMA_VERSION:
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(
                    f"no migration registered to upgrade schema version {version} "
                    f"to {version + 1} (SCHEMA_VERSION={SCHEMA_VERSION})"
                )
            conn.executescript(migration)
            version += 1
            _set_schema_version(conn, version)

        conn.commit()
    finally:
        conn.close()
