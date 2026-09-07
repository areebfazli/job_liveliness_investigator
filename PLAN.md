# Build Plan

`spec.md` is final and is the only source of requirements. This file is the build order.
`[ ]` todo · `[~]` in progress · `[x]` done

## Stack
Python 3.12 + uv · FastAPI · SQLite (raw sql, no ORM) · Pydantic · httpx · bs4 / JSON-LD · lifelines · scikit-learn · one structured-output LLM API · cron · pytest (`respx` fixtures) · all thresholds in `config.toml`.

## Layout
```
rli/  config  db  models  net  resolvers  snapshots  archive  history  events
      probes  policy  agent  llm  replay  eval  api  cli.py
tests/   data/ (gitignored)   config.toml   README.md
```

---

## M0 Skeleton
- [ ] uv project, git init, `rli` package, `rli` CLI stub, pytest smoke test
- [ ] `config.toml` + typed loader
- [ ] SQLite schema: the 9 spec §7 tables + `llm_cache`, `tool_cache`
- [ ] Models: `EvidenceItem` (§3 fields), `ProbeResult` `{ok,error,retryable}`, `PolicyInputs` (8 inputs, each nullable = unpopulated), `Decision` (§1 output: `posting_state, recommended_action, recheck_after_days, evidence_quality, hypotheses, reason[{text,evidence_ids}], evidence`)
- [ ] `net`: httpx wrapper, per-host rate limit + backoff, domain allowlist, tool cache

**Exit:** `pytest` green, `rli init-db` works.

## M1 Data foundation (Phase 0)
- [ ] Pick the first ATS from the target-company audit; build its adapter, `resolve_posting`, and `board_snapshot`
- [ ] Start daily snapshots immediately for supported targets → `board_snapshots`, `posting_snapshots` (`first_observed, last_seen_open, first_seen_absent, reappeared_at, replacement_job_id, content_hash`); document cron
- [ ] Complete Greenhouse + Ashby (`ats_native` dates), Lever (no trusted dates), and JSON-LD (`datePosted` → `page_structured`, `validThrough` → `declared_expiry`); expand collection as adapters land
- [ ] Wayback CDX client (rate-limited; throttle/failure = coverage gap, never absence); backfill 6–12 months; record capture density per company
- [ ] Target list ≥40 companies + ATS/archive coverage audit → `data/coverage_audit.md`; decide: extend live collection or reduce claimed scope

**Exit:** automated snapshots work; resolver covers most targets; coverage audit says evaluation data is feasible. Verify 3 unattended days in parallel with later development.

## M2 Evidence layer (Phase 1)
- [ ] Interval-censored closures (`last_seen_open`, `first_seen_absent` only)
- [ ] Repost/version matching (title, team, location, description; thresholds in config); hand-check 50 matches → `data/match_precision.md`
- [ ] Board-history features, always with `history_days` / `history_coverage`
- [ ] `company_events` store with `available_at`; pre-collect dated events for target companies

**Exit:** case files hold only timestamped, source-linked facts with explicit coverage.

## M3 Probes, policy, baselines (Phase 2)
- [ ] Probe base: Pydantic args, allowlist, cost tier, `history_required`, structured failure
- [ ] `repost_history`, `requirements_drift`, `company_events`, `team_signal` (optional source; stub if unlicensed)
- [ ] `policy_inputs`: derive 8 inputs; distinguish unknown from false; expose unpopulated inputs and which could still change the action
- [ ] `evidence_quality` (strong/mixed/weak) and `action_policy` (§5 table, `recheck_after_days` rule) — deterministic
- [ ] Before freezing, document overlapping policy branches and their precedence; test freeze/expiry against weak evidence and recent publication
- [ ] Temporal + company splits (needed here to tune policy)
- [ ] Tune policy once on dev/temporal-validation data → `frozen_at` in config
- [ ] System A (full probes); System B (rules), versioned and frozen
- [ ] Tests for every policy branch and unknown-input case

**Exit:** A and B run on live postings; policy frozen; B versioned.

## M4 Replay (Phase 2)
- [ ] Build and validate replay on a small cached dataset while live collection continues
- [ ] Point-in-time builder: only `available_at <= T`; dynamic results only if selected
- [ ] Replay mode: live tool calls forbidden; live LLM allowed on cache miss and recorded
- [ ] Leakage checker (target 0)
- [ ] lifelines interval-censored closure/repost curves
- [ ] A/B baseline metrics on development/validation data → `reports/baseline.md`; keep final holdouts untouched until M6

**Exit:** offline replay end-to-end, leakage 0, baseline report.

## M5 Agent (Phase 3)
- [ ] `LLMClient` + cache keyed `(model_id, prompt_hash, structured_input_hash)`; untrusted content delimited; outputs Pydantic-validated
- [ ] Investigator: contradictions, unresolved questions, candidate probes+args, or STOP
- [ ] Controller: eligibility = can populate ≥1 unpopulated input, history gating, `team_signal` only if `corroborating_hiring_signal` unknown **and** repost/long-lived branch reachable; schema + allowlist; budget/step/latency caps; reject identical probe+args; deterministic cost-aware ranking; RUN/STOP
- [ ] Bounded loop (max 4 dynamic steps), every decision in `run_steps`; bounded retry only if `retryable`
- [ ] Explanation: validate every `reason` cites existing `evidence_ids`; check that cited evidence supports the claim and measure failures
- [ ] System C on replay and live
- [ ] Tests: budget cap, no repeats, history gating, failure recovery, stops when no unresolved input could change the action

**Exit:** C runs on cached replay with zero live tool calls, and on live postings.

## M6 Evaluation (Phase 4)
- [ ] **Headline evaluation gate:** ≥300 postings, ≥40 companies, ≥100 closure events; otherwise extend collection or report explicitly reduced scope
- [ ] Run frozen A/B/C on temporal + company holdouts
- [ ] Metrics per §6: data quality, posting behavior, agent efficiency (agreement with A overall + macro per class, with action distribution)
- [ ] Agent gate: C medium/high-cost **probe count** ≤ 70% of B AND agreement ≥ B − 2pp, both overall and macro
- [ ] C2 (logistic ranker) only if data supports; drop if it does not beat deterministic ranking
- [ ] `reports/evaluation.md` with go/no-go and limitations
- [ ] Product gate: claim improved job-search outcomes only with held-out personal outcomes; otherwise report product value as unproven

**Exit:** report proves or disproves the agent.

## M7 Product shell (Phase 5)
- [ ] `POST /investigate {url}` → §1 output only; `GET /runs/{id}` internal/debug only
- [ ] Minimal UI: URL in, action + recheck, evidence timeline; optional role-watch
- [ ] Outcomes endpoint (`applied … silence`)
- [ ] README: run instructions, agent go/no-go, product gate status, limitations
- [ ] Demos: clear, ambiguous, failure/recovery

**Exit:** all of spec §9 satisfied.

---

## Notes
- Live snapshots are the long pole: start with the first working adapter in M1, keep running through M6. Data targets gate headline evaluation, not implementation.
- M2 and M3 can run in parallel after M1. History-gated probes need real history before M3's live exit is meaningful.
- No numeric confidence in user output until calibrated.
