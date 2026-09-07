# Progress

Tracks milestone status against PLAN.md (which is frozen). Update this file only.

| Milestone | Status | Notes |
|---|---|---|
| M0 Skeleton | done | 146 tests, ruff clean, `rli init-db` idempotent, 13 tables (9 spec + llm_cache, tool_cache, board_snapshot_jobs, capture_attempts) |
| M1 Data foundation | nearly done | Adapters (GH/Ashby/Lever/JSON-LD) live-verified; 78 targets audited; `rli snapshot` daily job + `rli archive backfill` built and tested (250 tests). Cron installed (06:00 UTC); day-0 snapshot done (78/78, 7699 postings); 12-month backfill run 2026-09-07. Remaining: 3 unattended days, coverage report + feasibility decision. |
| M2 Evidence layer | in progress | history (closures, matching, features, sample export) + events store built, 347 tests. Remaining: hand-check 50 matches -> data/match_precision.md; event collection covers only 15/78 companies (web-search cap), rerun in a fresh session. |
| M3 Probes, policy, baselines | in progress | 4 dynamic probes + registry, policy inputs/quality/action/splits built (595 tests). Remaining: Systems A/B runners, policy tuning + freeze on temporal-validation data. |
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
- 2026-09-07 M2: company_events extra fields stored as JSON in `description`; collection status kept in `data/events/collection_status.csv`. Companies never searched resolve to Unknown, not False.
- 2026-09-07 M2: schema v2 adds `repost_links`; `postings.first_seen_absent` records the first disappearance; interval model carries the latest one separately.
- 2026-09-07 M3: policy precedence P1 closed→skip; P2 posting_state unknown→wait (addition to §5 table so resolver failures never yield quick_apply); P3a freeze→wait; P3b declared expiry + not(open∧strong)→wait; P4 repeated_unchanged+long_lived+corroborating False→skip; P5 open+recent+strong+not known-negative→apply_now; P6 mixed/weak→quick_apply; P7 default quick_apply. `material_negative_event` Unknown does not block apply_now (bounded by requiring strong quality); one-token flip documented in rli/policy/action.py.
- 2026-09-07 M3: recheck_after_days = floor(min(cap, days_until_expiry)), clamped at 0 when expiry passed.
- 2026-09-07 M3: team_signal disabled (no licensed source). The "repost/long-lived branch reachable" eligibility condition lives in the controller (M5), not the probe.
