# Role-Liveness Investigator (`rli`)

Evidence-backed system that answers: *is this job posting worth effort now,
and what should I do next?*

See `spec.md` for the full product spec and `PLAN.md` for the build order.
This repository is currently at **M0 (Skeleton)** — see `PLAN.md` for
milestone status.

## Setup

```bash
uv sync
uv run rli --help
uv run rli init-db --path ./data/rli.db
```

## Tests

```bash
uv run pytest -q
uv run ruff check .
```

## Configuration

Configuration lives in `config.toml` (thresholds, budgets, `[net]` retry and
backoff knobs, per-host rate limits, per-probe domain allowlists, and
action-policy freeze state). All values there are placeholders until tuned
per `spec.md` §5/§6.

`rli.config.load_config()` resolves the file relative to the **repo root**,
not the process CWD, so a cron snapshot job and a test in a tmpdir load the
same file. Precedence is: explicit `path` argument > `$RLI_CONFIG` > repo
root. An installed (non-checkout) deployment has no repo root and must set
`RLI_CONFIG`. Every config table is validated with `extra = "forbid"`: a
misspelled key is a hard error, never a silently ignored line.

## Database

`rli init-db` is idempotent and stamps `PRAGMA user_version`. `schema.sql`
always describes the newest schema and is only used to create a fresh
database; to change the schema for existing databases, bump
`rli.db.SCHEMA_VERSION` and register the upgrade in `rli.db.MIGRATIONS`
keyed by the *from* version. Connections are opened with WAL journaling, a
busy timeout, and foreign keys enforced.
