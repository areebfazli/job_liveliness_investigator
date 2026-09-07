# Progress

Tracks milestone status against PLAN.md (which is frozen). Update this file only.

| Milestone | Status | Notes |
|---|---|---|
| M0 Skeleton | done | 146 tests, ruff clean, `rli init-db` idempotent, 13 tables (9 spec + llm_cache, tool_cache, board_snapshot_jobs, capture_attempts) |
| M1 Data foundation | in progress | Adapters (GH/Ashby/Lever/JSON-LD) live-verified; 78 targets audited; `rli snapshot` daily job + `rli archive backfill` built and tested (250 tests). Remaining: install cron, run 3 unattended days, full 6-12 month backfill, final coverage audit + feasibility decision. |
| M2 Evidence layer | todo | |
| M3 Probes, policy, baselines | todo | |
| M4 Replay | todo | |
| M5 Agent | todo | |
| M6 Evaluation | todo | |
| M7 Product shell | todo | |

## Decisions log
- 2026-09-07 M0: `evidence_quality` is computed by the policy layer from evidence, not stored in `PolicyInputs` (it is a derived output per spec §1). The action policy still receives it as an argument, matching spec §5.
- 2026-09-07 M0: allowlist is exact-host match plus explicit `"*"` for JSON-LD; https only; IP literals and private hosts rejected; redirects checked per hop.
- 2026-09-07 M0: `tool_cache` is append-only (keyed by probe, args_hash, fetched_at) so it can serve as the replay corpus later.
- 2026-09-07 M1: daily snapshot skips a company already captured that UTC day (idempotent by skip, not upsert). Failed captures write `capture_attempts` only; lifecycle fields are never touched on failure.
- 2026-09-07 M1: archive backfill stores raw archive board snapshots only; interval-censored posting lifecycle from archive data is M2 work.
