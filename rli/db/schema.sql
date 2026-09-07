-- Role-Liveness Investigator — SQLite schema (spec.md §7)
--
-- Conventions:
--   * timestamps are ISO 8601 strings in UTC with a literal "Z" suffix
--     (see rli/models/time.py: to_utc_z / parse_utc / now_utc / ensure_aware),
--     stored as TEXT so lexical string ordering matches chronological
--     ordering and point-in-time replay comparisons (spec.md §3/§6) are
--     unambiguous
--   * booleans are stored as INTEGER (0/1)
--   * money is REAL (USD)
--   * JSON-shaped columns (lists/dicts) are stored as TEXT (JSON-encoded)
--   * all tables use CREATE TABLE IF NOT EXISTS so init_db is idempotent
--
-- company_id is the normalized company website domain (spec.md §3 "Identity").
-- ATS tenant/board identifiers are NOT stored on companies — they live on
-- postings, since a company may span more than one ATS tenant over time.
--
-- This schema describes the newest version only (SCHEMA_VERSION in
-- rli/db/__init__.py). It is used solely to create a fresh database; once a
-- database exists, forward changes travel through the PRAGMA user_version
-- migration hook in rli/db/__init__.py, not through edits replayed here.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- companies
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    company_id      TEXT PRIMARY KEY,      -- normalized website domain, e.g. "acme.com"
    name            TEXT,
    website_domain  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- postings
--
-- spec.md §5 "Own snapshots record" the per-posting lifecycle aggregates
-- below. Archive-derived closures are interval-censored: last_seen_open /
-- first_seen_absent bracket the unknown closure time and must NEVER be
-- collapsed into a fabricated exact closed_at.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS postings (
    posting_id          TEXT PRIMARY KEY,
    company_id          TEXT NOT NULL REFERENCES companies (company_id),
    ats                 TEXT NOT NULL CHECK (ats IN ('greenhouse', 'ashby', 'lever', 'other')),
    ats_tenant_id       TEXT,                  -- ATS-specific board/tenant identifier
    ats_job_id          TEXT,                  -- ATS-specific job identifier
    canonical_url       TEXT NOT NULL,
    title               TEXT,
    team                TEXT,
    location            TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    -- Lifecycle aggregates (spec.md §5), derived from posting_snapshots:
    first_observed      TEXT,
    last_seen_open      TEXT,
    first_seen_absent   TEXT,
    reappeared_at       TEXT,
    replacement_job_id  TEXT REFERENCES postings (posting_id)
);

CREATE INDEX IF NOT EXISTS idx_postings_company_id ON postings (company_id);

-- ---------------------------------------------------------------------------
-- posting_snapshots
--
-- A pure per-capture row: one observation of one posting at one point in
-- time. `status = 'absent'` means "not present in this capture" — it is NOT
-- a claim that the posting is closed; closure inference from a run of
-- absences lives on postings.first_seen_absent (spec.md §5), which stays
-- interval-censored. A failed or throttled capture attempt is recorded in
-- capture_attempts as a coverage gap, never here as an absence (spec.md §4).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS posting_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id      TEXT NOT NULL REFERENCES postings (posting_id),
    captured_at     TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('own', 'archive')),
    status          TEXT NOT NULL CHECK (status IN ('open', 'absent')),
    content_hash    TEXT,
    capture_url     TEXT
);

CREATE INDEX IF NOT EXISTS idx_posting_snapshots_posting_captured
    ON posting_snapshots (posting_id, captured_at);

-- ---------------------------------------------------------------------------
-- board_snapshots
--
-- spec.md §4 header row per company-wide capture: company_id, captured_at,
-- source, and coverage_status. The per-job contents of the capture live in
-- board_snapshot_jobs (below), not in JSON columns here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS board_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          TEXT NOT NULL REFERENCES companies (company_id),
    captured_at         TEXT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('own', 'archive')),
    coverage_status     TEXT NOT NULL CHECK (coverage_status IN ('complete', 'partial', 'gap'))
);

CREATE INDEX IF NOT EXISTS idx_board_snapshots_company_captured
    ON board_snapshots (company_id, captured_at);

-- ---------------------------------------------------------------------------
-- board_snapshot_jobs
--
-- Child rows of board_snapshots: one row per open job listed in a board
-- capture (open_job_ids/titles/teams/locations, spec.md §4), needed for M2
-- repost matching by title/team/location/description similarity.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS board_snapshot_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    board_snapshot_id   INTEGER NOT NULL REFERENCES board_snapshots (id),
    job_id              TEXT NOT NULL,
    title               TEXT,
    team                TEXT,
    location            TEXT,
    description_hash    TEXT,
    url                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_board_snapshot_jobs_board_snapshot_id
    ON board_snapshot_jobs (board_snapshot_id);

-- ---------------------------------------------------------------------------
-- capture_attempts
--
-- spec.md §4: "a throttled or failed capture is recorded as a coverage gap,
-- never as an absence." This table exists so a rate-limited or erroring
-- fetch never gets conflated with posting_snapshots.status = 'absent' or a
-- board_snapshots.coverage_status downgrade being silently unexplained —
-- every attempt (successful or not) against a company/board/posting target
-- is logged here with enough detail (ok, error, retryable) to distinguish
-- "we tried and it's gone" from "we couldn't check."
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS capture_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      TEXT NOT NULL REFERENCES companies (company_id),
    target          TEXT NOT NULL,   -- the URL/board/posting the attempt was aimed at
    attempted_at    TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('own', 'archive')),
    ok              INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error           TEXT,
    retryable       INTEGER CHECK (retryable IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_capture_attempts_company_attempted
    ON capture_attempts (company_id, attempted_at);

-- ---------------------------------------------------------------------------
-- company_events
--
-- Dated layoffs, freezes, funding, expansion (spec.md §4 `company_events`
-- probe). `available_at` supports point-in-time replay (spec.md §6).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      TEXT NOT NULL REFERENCES companies (company_id),
    event_type      TEXT NOT NULL,
    event_at        TEXT NOT NULL,   -- when the underlying event happened
    available_at    TEXT NOT NULL,   -- when this system could first know about it
    source_url      TEXT,
    description     TEXT
);

CREATE INDEX IF NOT EXISTS idx_company_events_company_id ON company_events (company_id);

-- ---------------------------------------------------------------------------
-- evidence
--
-- Matches spec.md §3 EvidenceItem. `id` is run-local (e.g. "e1") rather than
-- globally unique, so the primary key is the (run_id, id) pair. `posting_id`
-- is nullable because some evidence (e.g. company_events-derived evidence)
-- is company-scoped and has no posting. `source_event_at` is nullable
-- because archive evidence may not carry a distinct underlying event time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
    id                  TEXT NOT NULL,
    run_id              TEXT NOT NULL REFERENCES runs (id),
    posting_id          TEXT REFERENCES postings (posting_id),
    probe               TEXT NOT NULL,
    claim_type          TEXT NOT NULL,
    value               TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    raw_excerpt         TEXT,
    source_quality      TEXT NOT NULL CHECK (
        source_quality IN ('ats_native', 'page_structured', 'archive', 'news', 'enrichment')
    ),
    source_event_at     TEXT,
    available_at        TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_posting_id ON evidence (posting_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run_id ON evidence (run_id);

-- ---------------------------------------------------------------------------
-- runs
--
-- posting_id is nullable: an unresolved-URL run (resolution failed before a
-- posting could be identified) must still be recordable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    posting_id          TEXT REFERENCES postings (posting_id),
    input_url           TEXT NOT NULL,
    system              TEXT NOT NULL CHECK (system IN ('A', 'B', 'C', 'C2')),
    mode                TEXT NOT NULL CHECK (mode IN ('live', 'replay')),
    replay_at           TEXT,    -- historical time T for replay runs; null for live runs
    policy_version      TEXT,
    config_hash         TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'stopped')),
    final_decision      TEXT,    -- JSON-encoded Decision (spec.md §1), null until finished
    total_cost_usd      REAL,
    total_latency_ms    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_runs_posting_id ON runs (posting_id);

-- ---------------------------------------------------------------------------
-- run_steps
--
-- The canonical trace per spec.md §2/§4: controller/model/probe steps,
-- prompt/model hashes, cache status, cost, latency, and errors.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_steps (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL REFERENCES runs (id),
    step_index          INTEGER NOT NULL,
    component           TEXT NOT NULL CHECK (component IN ('controller', 'model', 'probe')),
    decision_type       TEXT NOT NULL,
    probe_name          TEXT,
    args_hash           TEXT,
    prompt_hash         TEXT,
    model_id            TEXT,
    cache_status        TEXT CHECK (cache_status IN ('hit', 'miss', 'n/a')),
    cost_usd            REAL,
    latency_s           REAL,
    error               TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps (run_id);
CREATE INDEX IF NOT EXISTS idx_run_steps_run_step_index ON run_steps (run_id, step_index);

-- ---------------------------------------------------------------------------
-- outcomes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id      TEXT NOT NULL REFERENCES postings (posting_id),
    outcome_type    TEXT NOT NULL CHECK (
        outcome_type IN ('applied', 'reply', 'screen', 'interview', 'offer', 'rejection', 'silence')
    ),
    occurred_at     TEXT NOT NULL,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_posting_id ON outcomes (posting_id);

-- ---------------------------------------------------------------------------
-- llm_cache
--
-- Keyed by (model_id, prompt_hash, structured_input_hash) for exact
-- benchmark replay (spec.md §2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_cache (
    model_id                TEXT NOT NULL,
    prompt_hash             TEXT NOT NULL,
    structured_input_hash   TEXT NOT NULL,
    response                TEXT NOT NULL,  -- JSON-encoded model output
    created_at              TEXT NOT NULL,
    PRIMARY KEY (model_id, prompt_hash, structured_input_hash)
);

-- ---------------------------------------------------------------------------
-- tool_cache
--
-- Used by rli.net for HTTP/probe result caching with a TTL. Append-only:
-- rows are never overwritten in place, so replay can resolve the result
-- that was in effect at a given point in time. Readers take the latest
-- non-expired row for (probe, args_hash), i.e. the one with the greatest
-- fetched_at <= T that has not expired as of T.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_cache (
    probe       TEXT NOT NULL,
    args_hash   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    response    TEXT NOT NULL,  -- JSON-encoded response
    expires_at  TEXT NOT NULL,
    PRIMARY KEY (probe, args_hash, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_tool_cache_lookup ON tool_cache (probe, args_hash, fetched_at DESC);

-- ---------------------------------------------------------------------------
-- repost_links (schema version 2)
--
-- Audit trail for rli.history.matching.link_reposts: postings keeps only the
-- winning replacement_job_id, while this table records every accepted
-- (old -> new) repost link together with the component scores that produced
-- it, so a linking decision stays explicable after the [thresholds] change.
-- The same DDL lives in rli/history/schema_ext.sql and in
-- rli.db.MIGRATIONS[1] (which upgrades an existing version-1 database).
-- ---------------------------------------------------------------------------
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
