"""rli.archive — Wayback Machine archive backfill (PLAN.md M1 bullet 4).

* `rli.archive.cdx` — Wayback CDX Server API client (`list_captures`).
* `rli.archive.fetch` — fetch one archived capture body (`fetch_capture`).
* `rli.archive.backfill` — per-target-company backfill driver (`run_backfill`),
  persisting to `companies` / `board_snapshots` / `board_snapshot_jobs` /
  `capture_attempts` only.
* `rli.archive.coverage` — per-company coverage stats and Markdown report.
* `rli.archive.cli` — a standalone `typer` sub-app (`backfill`, `coverage`
  commands); not yet mounted into `rli.cli`.
"""
