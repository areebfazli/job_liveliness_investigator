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

A migration is either a SQL script (run with `executescript`) or a callable
taking the connection. The callable form exists for `ALTER TABLE ... ADD
COLUMN`, which SQLite cannot spell idempotently (there is no `IF NOT
EXISTS`): the callable inspects `PRAGMA table_info` and adds only what is
missing, inside one transaction that also stamps the new `user_version`, so
an interrupted upgrade is either fully applied or not applied at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from importlib import resources
from pathlib import Path

__all__ = [
    "BUSY_TIMEOUT_MS",
    "MIGRATIONS",
    "POSTING_PAGE_DATES_DDL",
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
# 2 -> 3: added the replay tables (`replay_datasets`, `replay_cases`,
#         `replay_probe_results`) — the cached full-probe record spec.md §6
#         requires replay to read instead of making live tool calls.
# 3 -> 4: `board_snapshot_jobs.first_published` / `.updated_at` (the ATS's
#         own stated dates, kept per daily capture) and the
#         `posting_page_dates` table (JSON-LD `datePosted` read once from a
#         Lever job page) — point-in-time publish evidence for replay.
SCHEMA_VERSION = 4

# from-version -> SQL script (or idempotent callable) that upgrades that
# version to version + 1. `schema.sql` describes only the newest version;
# every prior jump needed to reach it from an older on-disk database lives
# here instead.
MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection], None]] = {}

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

# 2 -> 3: the replay tables (PLAN.md M4). spec.md §6: "live **tool** calls are
# forbidden in replay; results come only from the cached full-probe record".
# `replay_probe_results` IS that record; `replay_datasets` / `replay_cases`
# name the (posting, T) subjects it was collected for. This DDL is duplicated
# verbatim in rli/db/schema.sql (the fresh-database path);
# tests/test_replay_migration.py asserts the two stay in sync. A migration's
# text is frozen once shipped, which is why it is spelled out here rather than
# read back from that file.
#
# JUDGMENT CALL: `replay_cases.posting_id` / `replay_probe_results.posting_id`
# carry NO foreign key to `postings`, unlike every other posting-scoped table
# in this schema. A replay subject is identified by the resolver's
# `"{ats}:{tenant}:{job_id}"` convention, and `rli.eval` is forbidden from
# creating a `postings` row it has not collected (see `rli.eval.runner`'s write
# invariant) — so a dataset must be able to name a posting the collector has
# never committed a row for. The alternative (an FK) would force the dataset
# builder to write to the collection corpus, which is exactly what makes an
# A/B/C comparison invalid.
MIGRATIONS[2] = """
CREATE TABLE IF NOT EXISTS replay_datasets (
    dataset_id      TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    split_kind      TEXT NOT NULL CHECK (split_kind IN ('temporal', 'company')),
    split_name      TEXT NOT NULL CHECK (split_name IN ('dev', 'validation', 'test')),
    grid_step_days  INTEGER NOT NULL,
    postings        INTEGER NOT NULL,
    companies       INTEGER NOT NULL,
    cases           INTEGER NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS replay_cases (
    dataset_id      TEXT NOT NULL REFERENCES replay_datasets (dataset_id),
    posting_id      TEXT NOT NULL,
    replay_at       TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    canonical_url   TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    PRIMARY KEY (dataset_id, posting_id, replay_at)
);

CREATE TABLE IF NOT EXISTS replay_probe_results (
    dataset_id   TEXT NOT NULL,
    posting_id   TEXT NOT NULL,
    replay_at    TEXT NOT NULL,
    probe_name   TEXT NOT NULL,
    args_hash    TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    ok           INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error        TEXT,
    retryable    INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    data         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, posting_id, replay_at, probe_name, args_hash)
);

CREATE INDEX IF NOT EXISTS idx_replay_probe_results_lookup
    ON replay_probe_results (dataset_id, posting_id, replay_at);
CREATE INDEX IF NOT EXISTS idx_replay_cases_dataset ON replay_cases (dataset_id);
"""


# 3 -> 4. The `posting_page_dates` DDL is duplicated verbatim in
# rli/db/schema.sql (the fresh-database path); tests/test_board_dates.py
# asserts the two stay in sync. Frozen once shipped, like the scripts above.
POSTING_PAGE_DATES_DDL = """
CREATE TABLE IF NOT EXISTS posting_page_dates (
    posting_id          TEXT PRIMARY KEY REFERENCES postings (posting_id),
    page_url            TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('ok', 'no_date', 'failed')),
    date_posted_raw     TEXT,
    date_posted         TEXT,
    fetched_at          TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_attempt_at     TEXT NOT NULL,
    last_error          TEXT
);
"""

# Nullable, no default: SQLite's ADD COLUMN for such a column is a schema-only
# change (no table rewrite), so this is O(1) even on a multi-GB database.
_BOARD_DATE_COLUMNS = (("first_published", "TEXT"), ("updated_at", "TEXT"))


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """Add the board-capture date columns and `posting_page_dates`, atomically.

    Idempotent: a column that already exists is skipped (`ALTER TABLE ... ADD
    COLUMN` has no `IF NOT EXISTS`, and a `board_snapshot_jobs` created from a
    newer `schema.sql` already has both). Everything — including the
    `user_version` stamp — happens in ONE `BEGIN IMMEDIATE` transaction, so an
    interrupted upgrade leaves the database exactly at version 3 and a re-run
    simply does it again.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(board_snapshot_jobs)")}
        for name, sql_type in _BOARD_DATE_COLUMNS:
            if name not in existing:
                # Names come from the fixed tuple above, never from input.
                conn.execute(f"ALTER TABLE board_snapshot_jobs ADD COLUMN {name} {sql_type}")
        conn.execute(POSTING_PAGE_DATES_DDL)
        _set_schema_version(conn, 4)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


MIGRATIONS[3] = _migrate_3_to_4


def _schema_sql() -> str:
    return resources.files("rli.db").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(path: str | Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a SQLite connection configured for concurrent, replay-safe use.

    Sets WAL journaling (a no-op for the in-memory `:memory:` database, which
    has no journal file), a busy timeout so concurrent writers block briefly
    instead of raising `sqlite3.OperationalError` immediately, foreign key
    enforcement, and row access by column name.

    `busy_timeout_ms` defaults to `BUSY_TIMEOUT_MS`. A long-running writer
    that shares the database with sibling processes — the sharded System C
    replay (`rli replay run --shard`) — passes a longer one, because a case
    that gives up on a lock after 5 s is a case that has to be redone.
    """
    busy_timeout_ms = int(busy_timeout_ms)
    if busy_timeout_ms < 0:
        raise ValueError(f"busy_timeout_ms must be >= 0, got {busy_timeout_ms}")
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
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
            if callable(migration):
                migration(conn)
            else:
                conn.executescript(migration)
            version += 1
            _set_schema_version(conn, version)

        conn.commit()
    finally:
        conn.close()
