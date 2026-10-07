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
    "RUNS_V5_DDL",
    "SNAPSHOT_RUNS_DDL",
    "SCHEMA_VERSION",
    "connect",
    "connect_read_only",
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
# 4 -> 5: `board_snapshot_jobs.last_published` (Ashby `publishedAt` is the
#         LAST publish time, not the first; v4 rows that carried it in
#         `first_published` are moved), frozen split assignment on the replay
#         tables (`replay_cases.split`, `replay_datasets.split_*`,
#         `.exclude_companies_from`), and `runs.system` also accepts 'R' (the
#         table is rebuilt: SQLite cannot alter a CHECK constraint in place).
# 5 -> 6: `snapshot_runs` (each daily snapshot run's start/end, so captures
#         stamped with a run's start can be dated honestly) and
#         `replay_datasets.company_holdout` (the stable company holdout a
#         build excluded).
# 6 -> 7: `replay_datasets.retired_at` / `.retired_reason` — a reversible
#         "retired" state (`rli replay retire` / `unretire`, rli.replay.retire):
#         a retired dataset is ignored by the company-holdout breach check and
#         refused by `replay run` / `eval run` without an explicit override.
#         No data is deleted.
SCHEMA_VERSION = 7

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


# 4 -> 5. Every ADD COLUMN below is nullable with no default (schema-only, no
# table rewrite), except `replay_cases.split`, whose CHECK SQLite verifies
# against the existing rows — a scan of `replay_cases` only (~10^4 rows).
_V5_ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("board_snapshot_jobs", "last_published", "TEXT"),
    ("replay_datasets", "split_method", "TEXT"),
    ("replay_datasets", "split_seed", "INTEGER"),
    ("replay_datasets", "split_cutoff", "TEXT"),
    ("replay_datasets", "split_validation_cutoff", "TEXT"),
    ("replay_datasets", "exclude_companies_from", "TEXT"),
    ("replay_cases", "split", "TEXT CHECK (split IN ('dev', 'validation', 'test'))"),
)

# The `runs` table as of version 5: identical to version 4 except that the
# `system` CHECK also admits 'R' (a no-LLM, eligibility-gated baseline). The
# same DDL lives in rli/db/schema.sql (the fresh-database path);
# tests/test_db_v5_migration.py asserts the two stay in sync. Frozen once
# shipped. `{name}` is the table name, so the migration can build it under a
# temporary name first (SQLite's documented table-rebuild procedure).
RUNS_V5_DDL = """
CREATE TABLE IF NOT EXISTS {name} (
    id                  TEXT PRIMARY KEY,
    posting_id          TEXT REFERENCES postings (posting_id),
    input_url           TEXT NOT NULL,
    system              TEXT NOT NULL CHECK (system IN ('A', 'B', 'C', 'C2', 'R')),
    mode                TEXT NOT NULL CHECK (mode IN ('live', 'replay')),
    replay_at           TEXT,    -- historical time T for replay runs; null for live runs
    policy_version      TEXT,
    config_hash         TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'failed', 'stopped')
    ),
    final_decision      TEXT,    -- JSON-encoded Decision (spec.md §1), null until finished
    total_cost_usd      REAL,
    total_latency_ms    INTEGER
)
"""

_RUNS_COLUMNS = (
    "id, posting_id, input_url, system, mode, replay_at, policy_version, config_hash, "
    "started_at, finished_at, status, final_decision, total_cost_usd, total_latency_ms"
)


def _runs_accepts_r(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone()
    return row is not None and "'R'" in str(row[0])


def _rebuild_runs_for_r(conn: sqlite3.Connection) -> None:
    """Widen `runs.system`'s CHECK via SQLite's table-rebuild procedure.

    https://www.sqlite.org/lang_altertable.html#otheralter, steps 4-8 and 10:
    create the new table under a temporary name, copy every row, drop the old
    table, rename the new one into place, recreate its index, and verify
    foreign keys. The caller has already turned `foreign_keys` OFF (step 1:
    the implicit `DELETE` of a `DROP TABLE` would otherwise trip the
    `evidence`/`run_steps` references) and opened the transaction (step 2).
    `evidence.run_id` / `run_steps.run_id` reference `runs` BY NAME, so they
    point at the rebuilt table once it is renamed.
    """
    conn.execute("DROP TABLE IF EXISTS runs_v5_new")
    conn.execute(RUNS_V5_DDL.format(name="runs_v5_new"))
    conn.execute(f"INSERT INTO runs_v5_new ({_RUNS_COLUMNS}) SELECT {_RUNS_COLUMNS} FROM runs")
    conn.execute("DROP TABLE runs")
    conn.execute("ALTER TABLE runs_v5_new RENAME TO runs")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_posting_id ON runs (posting_id)")
    problems = _runs_foreign_key_problems(conn)
    if problems:
        raise RuntimeError(
            f"runs table rebuild left {len(problems)} foreign-key violation(s); "
            f"first: {tuple(problems[0])!r}"
        )


def _runs_foreign_key_problems(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    """Foreign-key violations that involve `runs`: its own, and its children's.

    Scoped rather than DB-wide so an unrelated, older violation elsewhere (a
    `postings` reference from some other table, say) cannot block the rebuild
    of `runs`. `foreign_key_check(child)` also reports the child's OTHER
    references (e.g. `evidence.posting_id`), so those rows are filtered to
    the ones whose parent is `runs`.
    """
    problems = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check(runs)")]
    for child in ("evidence", "run_steps"):
        problems += [
            tuple(row)
            for row in conn.execute(f"PRAGMA foreign_key_check({child})")
            if row[2] == "runs"
        ]
    return problems


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """Version 5, atomically and idempotently (see `SCHEMA_VERSION`'s notes).

    * Adds the columns in `_V5_ADD_COLUMNS` that are missing (a table created
      from a newer `schema.sql` already has them).
    * Moves Ashby's `publishedAt` out of `board_snapshot_jobs.first_published`
      into `last_published`. Ashby documents it as "when the job was last
      published", so it was never first-publish evidence. A v4 row is
      recognisable structurally: the v4 writer (`rli.probes.board_snapshot`)
      stored Ashby's single date in `first_published` and never set
      `updated_at`, while Greenhouse always carries both. A re-run finds no
      such row, so the move is idempotent.
    * Rebuilds `runs` so its `system` CHECK admits 'R' — skipped when the
      stored DDL already admits it.

    `PRAGMA foreign_keys` is a no-op inside a transaction, so it is switched
    OFF before `BEGIN IMMEDIATE` and restored afterwards whatever happens.
    Everything else, including the `user_version` stamp, is ONE transaction:
    an interrupted upgrade leaves the database exactly at version 4.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "migration 4->5 must start outside a transaction (PRAGMA foreign_keys "
            "cannot change inside one)"
        )
    fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, name, sql_type in _V5_ADD_COLUMNS:
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if name not in existing:
                    # Names/types come from the fixed tuple above, never from input.
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            conn.execute(
                """
                UPDATE board_snapshot_jobs
                SET last_published = first_published, first_published = NULL
                WHERE first_published IS NOT NULL AND updated_at IS NULL
                """
            )
            if not _runs_accepts_r(conn):
                _rebuild_runs_for_r(conn)
            _set_schema_version(conn, 5)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        if fk_was_on:
            conn.execute("PRAGMA foreign_keys = ON")


MIGRATIONS[4] = _migrate_4_to_5


# 5 -> 6. The same DDL lives in rli/db/schema.sql (the fresh-database path);
# tests/test_run_windows_and_holdout.py asserts the two stay in sync. Frozen once
# shipped.
SNAPSHOT_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS snapshot_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    stamped_at_start    INTEGER NOT NULL CHECK (stamped_at_start IN (0, 1)),
    source              TEXT NOT NULL CHECK (
        source IN ('snapshot', 'log_import', 'capture_fallback')
    ),
    log_file            TEXT,
    UNIQUE (started_at, source)
);
"""


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """Version 6, atomically and idempotently: `snapshot_runs` + `company_holdout`.

    `CREATE TABLE IF NOT EXISTS` and a column added only when missing, so a
    database whose tables came from a newer `schema.sql` (which `init_db`
    runs first) passes straight through. One `BEGIN IMMEDIATE` transaction,
    the `user_version` stamp included: an interrupted upgrade leaves version 5.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(SNAPSHOT_RUNS_DDL)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(replay_datasets)")}
        if "company_holdout" not in existing:
            conn.execute("ALTER TABLE replay_datasets ADD COLUMN company_holdout TEXT")
        _set_schema_version(conn, 6)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


MIGRATIONS[5] = _migrate_5_to_6


# 6 -> 7. Nullable, no default: schema-only ADD COLUMNs (no table rewrite).
# The same columns are spelled in rli/db/schema.sql (the fresh-database path).
_V7_ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("replay_datasets", "retired_at", "TEXT"),
    ("replay_datasets", "retired_reason", "TEXT"),
)


def _migrate_6_to_7(conn: sqlite3.Connection) -> None:
    """Version 7, atomically and idempotently: the replay-dataset retired state.

    Each column is added only when missing (a table created from a newer
    `schema.sql`, which `init_db` runs first, already has both). One
    `BEGIN IMMEDIATE` transaction, the `user_version` stamp included: an
    interrupted upgrade leaves version 6.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, name, sql_type in _V7_ADD_COLUMNS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if name not in existing:
                # Names/types come from the fixed tuple above, never from input.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        _set_schema_version(conn, 7)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


MIGRATIONS[6] = _migrate_6_to_7


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


def connect_read_only(
    path: str | Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """Open an EXISTING database with SQLite's `mode=ro`: no write can reach the file.

    For reports run against the live database (`rli eval run --read-only`).
    Unlike `connect` it sets no journal mode (that is a write) and never
    creates the file: a missing path raises `sqlite3.OperationalError`.
    """
    target = Path(path)
    if not target.exists():
        raise sqlite3.OperationalError(f"database file not found: {target}")
    uri = target.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=int(busy_timeout_ms) / 1000)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
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
