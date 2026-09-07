-- rli.history schema extension: repost_links (schema version 2)
--
-- The audit trail behind `rli.history.matching.link_reposts`. `postings`
-- records only the winning `replacement_job_id`; this table records EVERY
-- accepted (old -> new) link with the scores that produced it, so a linking
-- decision stays explicable after the thresholds in `[thresholds]` change.
--
-- Conventions follow rli/db/schema.sql: timestamps are `to_utc_z` TEXT,
-- JSON-shaped columns are TEXT, and every object uses IF NOT EXISTS.
--
-- This DDL is duplicated VERBATIM (modulo comments/whitespace) in three
-- places, and `tests/test_history_migration.py` asserts they stay in sync:
--   1. here — the history package's own copy, for readers of this module;
--   2. the tail of rli/db/schema.sql — so a FRESH database gets the table
--      from the normal `executescript` path (schema.sql must always describe
--      the newest version, per its own docstring);
--   3. rli.db.MIGRATIONS[1] — so an EXISTING version-1 database is walked
--      forward to version 2 by `init_db`.

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
