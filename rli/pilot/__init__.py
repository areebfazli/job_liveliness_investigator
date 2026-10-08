"""One-off pilots. Nothing here is imported by the product code paths.

`rli.pilot.wayback_pages` — 2026-10-08 pilot: archived per-job ATS pages
(Wayback Machine) as point-in-time evidence. Writes only to its own SQLite
file under `data/pilot/`, reads `data/rli.db` read-only. See
`reports/pilot_wayback_job_pages.md`.
"""
