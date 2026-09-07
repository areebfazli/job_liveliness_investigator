# Progress

Tracks milestone status against PLAN.md (which is frozen). Update this file only.

| Milestone | Status | Notes |
|---|---|---|
| M0 Skeleton | done | 146 tests, ruff clean, `rli init-db` idempotent, 13 tables (9 spec + llm_cache, tool_cache, board_snapshot_jobs, capture_attempts) |
| M1 Data foundation | nearly done | Adapters (GH/Ashby/Lever/JSON-LD) live-verified; 78 targets audited; `rli snapshot` daily job + `rli archive backfill` built and tested (250 tests). Cron installed (06:00 UTC); day-0 snapshot done (78/78, 7699 postings); 12-month backfill run 2026-09-07. Remaining: 3 unattended days, coverage report + feasibility decision. |
| M2 Evidence layer | in progress | history (closures, matching, features, sample export) + events store built, 347 tests. Matching fixed (Lever 'Apply' anchor bug, temporal gates, one-to-one): 143 links on real data. Re-backfill with fixed extractor 2026-09-07. Remaining: hand-check 50 matches -> data/match_precision.md; event collection covers only 15/78 companies (web-search cap), rerun in a fresh session. |
| M3 Probes, policy, baselines | nearly done | Probes, registry, policy, splits, Systems A/B (`rli run --system A|B`), 707 tests; live A/B agree on 3 URLs. Remaining: policy tuning + freeze on temporal-validation data (needs collection window). |
| M4 Replay | done (code) | PIT replay dataset builder, replay runner, leakage checker (0 violations on real data), lifelines survival curves, baseline/behavior reports, `rli replay build|run|check`, `rli eval baseline|behavior`. Data caveat: 117/118 dev cases are archive-era and therefore `weak`; A/B agreement 100% is near-trivial until live-era snapshots accumulate. |
| M5 Agent | done (code) | LLM client (Anthropic + scripted + cache), investigator, deterministic controller, bounded loop, evidence-cited explanation with fallback, `rli agent run|trace`, 984 tests. Not exercised live: no ANTHROPIC_API_KEY in env. |
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
- 2026-09-07 M3: System B routing tree b1 documented in rli/eval/system_b.py; version recorded in runs.config_hash. C2 contradiction uses >= so same-run resolver-open vs board-absent counts as mixed.
- 2026-09-08 M4: replay corpus is point-in-time via SQLite temp-schema views filtered to <= T; lifecycle columns re-derived at T with rli.history.closures. Archive-era T uses a synthetic `archive_board_state` source (capture-time stamped) and never backdates ATS publish dates.
- 2026-09-08 M4: the full-probe record is collected once per posting; `company_events` is re-run per T because its args include as_of. `test` split is refused at build time.
- 2026-09-08 M4: found and fixed two bugs: case builder bypassed the replay gate (saved evidence via Run instead of ProbeRunner); archive-only postings never matched their corpus row, so history probes were always ineligible and A == B trivially.
- 2026-09-08 M5: investigator prompt input carries config-derived budget estimates (not measured spend) so identical runs hash identically and the llm_cache can hit in replay. Model rows record tokens in run_steps.decision_type (`investigator:tokens=in/out`).
- 2026-09-08 M5: run_steps.cost_usd mixes dollars (model rows) and cost points (probe rows); M6 reports must split by component. A runs all dynamic probes while C is gated by could_change_action, so the probe-count gate flatters C structurally; M6 must report this.
