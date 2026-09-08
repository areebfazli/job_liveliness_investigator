# Role-Liveness Investigator (`rli`)

Evidence-backed system for tech/startup job seekers that answers:

> **Is this role worth effort now, and what should I do next?**

Given a job posting URL, `rli` resolves it against its ATS (Greenhouse,
Ashby, Lever) or its career page's JSON-LD, gathers timestamped,
source-linked evidence, and returns one of four recommended actions with
evidence-cited reasons. It never returns a definitive `ghost_job` verdict,
never scrapes LinkedIn, never produces a crowd score or a generic
company-health score, and never exposes an uncalibrated 0–100 "Apply
Priority" — see `spec.md` §1 for the full non-goals list. `evergreen`,
`paused`, and similar labels are treated as hypotheses to investigate, not
observable facts to assert.

See `spec.md` for the full product spec and `PLAN.md` for the build order
this repository followed.

## The four actions

- **`apply_now`** — worth focused effort now (open, recent primary publish
  evidence, no material negative event).
- **`quick_apply`** — apply with minimal tailoring (evidence is mixed or
  weak, or none of the stronger conditions are met).
- **`wait`** — evidence is unresolved or hiring appears paused; includes
  `recheck_after_days` (`min(14, days_until_validThrough)` when a declared
  expiry exists, otherwise a 14-day config default).
- **`skip`** — the posting is closed, or evidence indicates a long-lived
  repost pattern with corroborating weak-hiring signals.

`message_first` is deferred until a verified contact exists (spec.md §1).

## How to run

```bash
# 1. Install
uv sync

# 2. Create the database (idempotent)
uv run rli init-db --path ./data/rli.db

# 3. Load the verified target-company list into `companies`
uv run rli load-targets --targets scripts/targets.csv --db ./data/rli.db

# 3b. Load pre-collected dated company events (spec.md §4) into `company_events`
uv run rli load-events --path data/events/company_events.csv --db ./data/rli.db

# 4. Daily board snapshot (run once now, then put on a schedule — see
#    docs/cron.md for the full cron setup and how to verify it ran)
uv run rli snapshot --db ./data/rli.db
uv run rli snapshot-status --db ./data/rli.db

# 5. Archive backfill (Wayback Machine; live network calls)
uv run rli archive backfill --months 12 --db ./data/rli.db
uv run rli archive coverage --db ./data/rli.db --out data/archive_coverage.md

# 6. Derive history: interval-censored closures + repost/version matching
uv run rli history rebuild --db ./data/rli.db
uv run rli history sample --db ./data/rli.db --out data/match_sample.csv --n 50

# 7. Build a point-in-time replay dataset, replay A/B on it, audit leakage
#    (this is the ONE replay command that makes live network calls)
uv run rli replay build --dataset my-dataset --split dev --split-kind company --db ./data/rli.db
uv run rli replay run --system A --dataset my-dataset --db ./data/rli.db
uv run rli replay run --system B --dataset my-dataset --db ./data/rli.db
uv run rli replay check --dataset my-dataset --db ./data/rli.db

# 8. Reports
uv run rli eval baseline --dataset my-dataset --out reports/baseline.md --db ./data/rli.db
uv run rli eval behavior --out reports/behavior.md --db ./data/rli.db
uv run rli eval run --dataset my-dataset --out reports/evaluation.md --db ./data/rli.db
#   add --with-c to also score System C (needs ANTHROPIC_API_KEY; otherwise
#   it is recorded as "not run: no API key" and the agent gate reads "not run")
uv run rli eval gates --dataset my-dataset --db ./data/rli.db

# 9. Investigate one URL directly (System A = full probes, System B = rules)
uv run rli run --system B --url https://boards.greenhouse.io/<tenant>/jobs/<id> --db ./data/rli.db

# 10. System C: the bounded LLM agent (needs ANTHROPIC_API_KEY for a live call)
uv run rli agent run --url https://boards.greenhouse.io/<tenant>/jobs/<id> --db ./data/rli.db
uv run rli agent trace --run-id <run-id-from-stderr> --db ./data/rli.db

# 11. Product API + UI
RLI_DB_PATH=./data/rli.db uv run python -m rli.api
# then, in another shell:
curl -s -X POST http://127.0.0.1:8000/investigate \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://boards.greenhouse.io/<tenant>/jobs/<id>", "system": "B"}'
# or open http://localhost:8000/ in a browser
```

Steps 4–7 need live network access; step 7's `replay build` is the only
*replay* command that does (everything downstream of it replays from the
cached record with tool calls forbidden — spec.md §6). Step 10 and an
`/investigate` call with `system: "C"` (or omitted — "C" is the API
default) need `ANTHROPIC_API_KEY`; without it, System C is untested live in
this environment (see Limitations) and the API transparently falls back to
System B, marking the response `degraded: true`.

`uv run rli --help` and `<command> --help` are the source of truth for every
flag above — this recipe was verified against that output, not against a
stale description.

## Safety and reproducibility invariants, and how they're enforced

| Invariant (spec.md §2/§9) | Enforced by |
|---|---|
| Untrusted job/news text is delimited, never trusted as instructions | `rli.llm.client.UntrustedBlock` — untrusted text can only travel through this wrapper, which a live delimiter string cannot enter |
| Model outputs are schema-validated; probe arguments are Pydantic-validated | `rli.llm.client.LLMClient.complete_structured` (raises `LLMSchemaError` rather than returning free text); `rli.agent.controller.decide` validates `ProbeCandidate.args` against each probe's `ArgsModel` and then discards the model's values, rebuilding args itself via `rli.probes.registry.build_args` |
| Per-probe network-domain allowlists; SSRF hardening | `rli.net.check_allowed` (https-only, no userinfo, no IP literals, no private hosts, exact host match per probe, `"*"` only for JSON-LD, redirects re-checked per hop) |
| Structured probe failure, no uncontrolled retries | `rli.net.NetResult` (`ok`/`error`/`retryable`); `RETRYABLE_STATUS_CODES` gates bounded retry only when `retryable` |
| Tool-result and LLM-output caching for exact replay | `rli.net.ToolCache` (append-only `tool_cache`, keyed by probe+args+fetch time); `rli.llm.client`'s cache keyed by `(model_id, prompt_hash, structured_input_hash)` — split so a changed prompt template invalidates globally without conflating it with per-case data |
| Bounded agent budgets and step caps | `rli.agent.controller.decide` (cost/latency/step-cap hard stops, rejects a repeated identical probe+args, stops when no unresolved policy input `could_change_action`); `config.toml [thresholds].max_dynamic_steps = 4`, `[budgets]` |
| One frozen action policy shared by A/B/C | `rli.policy.action` (called identically by `rli.eval.system_a`, `rli.eval.system_b`, and `rli.agent.loop.run_system_c` via `rli.eval.runner.decide_and_finish`) |
| Point-in-time replay forbids live tool calls | `rli.replay.mode.ReplayNetClient` raises on any live-network attempt during replay; only `available_at <= T` evidence and dynamic results the replayed system actually selected are exposed; `rli.replay.leakage` audits every replay run and reports violations (target: 0) |
| Every user-facing reason cites real evidence | `rli.agent.explanation.explain` validates every `reason.evidence_ids` against evidence actually in the case, with a fallback path when the model fails to comply |

## Limitations

This section states the current, honestly weaker parts of the system —
per spec.md §9, the README must report both agent and product limitations
even if the simpler system (rules) wins.

**Evaluation data is far short of the spec.md §6 headline targets.** The
last `rli eval run` (`reports/evaluation.md`, generated
2026-09-08T00:04:21Z against replay dataset `m4-dev-20`) evaluated **20
postings / 20 companies / 118 replay cases**, against spec §6's targets of
≥300 postings, ≥40 companies, ≥100 observed closures. The **collection
corpus** is much larger (13,396 postings, 78 companies, 5,697 closure
events — the closure-events leg is corpus-wide and does clear its target),
but the report is explicit that the corpus size and the evaluated-dataset
size are not interchangeable, and grades postings/companies on the smaller,
actually-evaluated dataset. **Headline gate: NOT MET.**

**Archive-era evidence is weak by construction.** For any replay case whose
point-in-time `T` predates this project's own daily snapshots, the only
observation available is a Wayback capture: `board_snapshot` evidence is
sparse, `source_quality='archive'`, and often absent entirely, and archive
derived closures are interval-censored (`last_seen_open`/`first_seen_absent`
only — no exact `closed_at` is ever invented). PROGRESS.md's M4 note records
that 117 of 118 dev-split cases were archive-era at the time replay was
built, making A/B agreement close to trivial until more live-era snapshot
history accumulates.

**`team_signal` is unlicensed and its policy branch is unreachable.** There
is no licensed team/hiring-signal source configured
(`[team_signal].enabled = false`), and `team_signal` is the only probe that
can populate `corroborating_hiring_signal`. The policy's repost/long-lived
→ `skip` branch (P4) is therefore never exercised by real evidence in any
current report, and the agent's high-cost probe tier is effectively empty.

**Company-events coverage is partial, and was recently found to be worse
than intended.** The `company_events` collector reached only **15 of 78**
target companies (the web-search cap encountered during a live collection
session). Worse, PROGRESS.md's M6 log records a since-fixed bug: collected
events were never loaded into `data/rli.db` at all, which is why
`reports/evaluation.md` (generated 2026-09-08, after the fix landed as the
new `rli load-events` command) still reports **0/78** companies carrying
any `company_events` row — the fix exists but the evaluation dataset has
not yet been rebuilt against a database that actually has events loaded.
Run `rli load-events` before any run/eval that should see company-event
evidence, and treat the current evaluation report's company-events figures
as reflecting the pre-fix state. Even after loading, for the uncovered
majority the `material_negative_event` and `freeze_or_pause` policy inputs
read as the `Unknown` sentinel rather than a negative finding (never as "no
news = flat hiring").

**System C (the LLM agent) has not been exercised live in this
environment.** No `ANTHROPIC_API_KEY` was available during development or
in the last evaluation run, so System C's investigator/controller loop has
1,076 passing offline/mocked tests but zero recorded live model calls, and
`reports/evaluation.md`'s agent gate reads **NOT RUN** rather than pass or
fail — System C produced zero scoped runs, so it has not failed, it has
never been graded. The product API (`POST /investigate`) detects the
missing key and transparently degrades to System B
(`degraded: true, degraded_reason: "no ANTHROPIC_API_KEY configured; ran
System B instead of System C"`), and also degrades if every System C model
call in a run errors out.

**System A is not a neutral upper bound on probe use.** `rli.eval.system_a`
runs every dynamic probe that survives history/licensing gates regardless
of whether it could change the action, while System C is gated by
`rli.policy.inputs.could_change_action`. Every probe-count comparison
against A is therefore biased toward the leaner system by construction, not
by measurement — this is why spec §6's agent gate compares medium/high
probe use against System B, not A.

**Cron reliability is not guaranteed.** `docs/cron.md` documents a single
daily invocation; a missed or failed day is recorded per-company as a
`capture_attempts` coverage gap, never silently treated as "still open" or
"closed", but the collection history is only as complete as the cron job's
actual uptime — see `docs/cron.md`'s verification checklist
(`rli snapshot-status`, the day's log file, and `board_snapshots` row-count
growth) for how to check whether it is really running.

**Repost/version match precision is not yet validated.** Spec §4 requires a
hand-checked sample of 50 matches; `reports/evaluation.md` records that
`data/match_precision.md` does not exist yet, so the repost-derived policy
inputs (`repost_pattern`) carry an unquantified error rate.

**Probe cost points and model dollars are different units and are never
summed** — `run_steps.cost_usd` holds placeholder cost points on probe
steps (`[probe_costs]`: low=1, medium=3, high=10) and real USD on model
steps; no report anywhere quotes one combined "total cost" figure.

## Agent go/no-go

`reports/evaluation.md` exists (generated by `rli eval run --dataset
m4-dev-20 --out reports/evaluation.md` on 2026-09-08). PROGRESS.md labels
M6 "done (code), interim report" — this is that interim report, on the
small `m4-dev-20` dataset, predating a full evaluation on the larger
backfilled corpus. Its verdict as of this writing:

- **Headline evaluation gate: NOT MET** (20 postings / 20 companies / 118
  replay cases evaluated, vs. spec §6's ≥300 / ≥40 / ≥100 targets — see
  Limitations above).
- **Agent gate: NOT RUN.** No `ANTHROPIC_API_KEY` was present, so System C
  was not invoked (`--with-c` was not requested, and would have found no
  key regardless); the report explicitly distinguishes "not run" from
  "fail" — System C has produced zero scoped runs and has not been judged
  against spec §6's 70%-probe-use / agreement-within-2pp criteria. System
  B alone, for reference, used a 1.03 medium/high-cost probe per run and
  agreed with System A 100% (overall and macro) on this small dataset — a
  number the report itself flags as near-trivial given the dataset's size
  and archive-heavy composition.
- **C2 (learned probe ranking): degenerate**, not kept — the temporal
  holdout split had a single-class label, so no learned-vs-deterministic
  comparison was possible on this data.

To actually grade System C, set `ANTHROPIC_API_KEY` and re-run:
`uv run rli eval run --dataset m4-dev-20 --with-c --out reports/evaluation.md`.

## Product gate

**Unproven**, per spec §6 and confirmed in `reports/evaluation.md`: the
`outcomes` table is empty — no `applied`/`screen`/`interview` (or any other)
personal outcome has ever been recorded via `POST /outcomes`, so there is no
evidence, of any kind, that following a recommended action improves
job-search outcomes (effort-per-screen, effort-per-interview, or any other
predeclared metric). This is a "not yet run", not a failed claim: the gate
requires held-out personal-outcome data that does not exist yet in this
environment.

## Product shell

`PROGRESS.md` still lists M7 as "todo" as of this writing, but the code
below exists, is exercised by `tests/test_api.py` (17 passing tests), and
was manually verified against a scratch database while writing this
document (see `docs/demo.md`).

`uv run python -m rli.api` starts the FastAPI app (`rli.api.app:app`) via
uvicorn.

- `POST /investigate {url, system?: "A"|"B"|"C"}` — `system` defaults to
  `"C"`; falls back to System B with `degraded: true` when
  `ANTHROPIC_API_KEY` is unset or every System-C model call fails. Returns
  the spec §1 decision fields plus `run_id`, `system_used`, `degraded`,
  `degraded_reason`.
- `GET /runs/{run_id}` — the internal `run_steps` trace, only when header
  `X-RLI-Debug: 1` is sent **and** `RLI_API_DEBUG_ROUTES` is on (the
  default); `404` otherwise.
- `POST /outcomes {run_id|posting_id, outcome, occurred_at?, note?}` —
  records one of `applied | reply | screen | interview | offer | rejection
  | silence` against a posting.
- `GET /watch`, `POST /watch {url}`, `GET /watch/due` — a simple role-watch
  list (stored in a JSON file, not the SQLite schema) that reruns System B
  (never C — a watch recheck is meant to be cheap) once a watched posting's
  `recheck_after_days` has elapsed.
- `GET /health`, `GET /` (serves `rli/ui/index.html`).

Settings are environment variables, not `config.toml` (Phase 5 was not
allowed to touch the frozen config system): `RLI_DB_PATH` (default
`./data/rli.db`), `RLI_API_DEBUG_ROUTES` (default on), `RLI_WATCH_STORE_PATH`
(default `./data/watches.json`), `RLI_API_HOST` (default `127.0.0.1`),
`RLI_API_PORT` (default `8000`).

`rli/ui/index.html` is a single self-contained HTML+vanilla-JS page: a URL
input with a system selector, the recommended action / posting state /
recheck days / evidence quality, a hypotheses section kept visually and
structurally separate from evidence, reasons linked to the evidence items
that support them, an evidence timeline sorted by
`source_event_at`/`available_at`, an outcome-submission form, and
watch/check-due buttons.

See `docs/demo.md` for worked clear/ambiguous/failure demos, each with both
a CLI and an API form.

## Tests

```bash
uv run pytest -q
uv run ruff check .
```

1,076 tests pass, 1 is skipped by design (`tests/test_llm_live.py`: a real
`ANTHROPIC_API_KEY`-gated live-LLM test that costs money and is not run
without one). `ruff check .` is clean. Per `PROGRESS.md`'s milestone table:
M0–M5 done (M1–M3 "nearly done"/"in progress" with specific remaining items
logged there), M6 "done (code), interim report", M7 "todo" as of the version
read while writing this document — treat `PROGRESS.md` as the authoritative,
continuously-updated milestone tracker and this README as a snapshot of what
was verified at the time it was written.

## Configuration

Configuration lives in `config.toml` (thresholds, budgets, `[net]` retry
and backoff knobs, per-host rate limits, per-probe domain allowlists, probe
cost tiers, and action-policy freeze state). Most values there are still
placeholders pending tuning on more collection data — see the Limitations
section above and the comments in `config.toml` itself, which mark each
untuned value explicitly.

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

## Cron

Daily board snapshots are the long pole of the whole project — history
gated evidence only exists for as long as this job has actually been
running. See `docs/cron.md` for the crontab entry, why it must use absolute
paths, and three independent ways to verify it actually ran.

## Project layout

```
rli/
  net/       allowlisted, rate-limited, cached HTTP access
  resolvers/ Greenhouse / Ashby / Lever / JSON-LD adapters
  snapshots/ daily board-snapshot capture
  archive/   Wayback Machine backfill
  history/   closures, repost/version matching, board-history features
  events/    company-events collection/store
  probes/    dynamic probes (repost_history, requirements_drift,
             company_events, team_signal) + registry
  policy/    evidence_quality, policy inputs, the frozen action policy
  eval/      System A (full probes), System B (rules), metrics, gates,
             evaluation report — shared by live runs and replay
  agent/     System C: investigator, deterministic controller, bounded
             loop, evidence-cited explanation
  llm/       structured-output LLM client, prompt/cache machinery
  replay/    point-in-time replay: build, run, leakage audit
  api/       Phase 5 product shell: FastAPI app, settings, watch logic/store
  ui/        the single-page product UI served at `/`
  cli.py     the `rli` command-line entrypoint
docs/        operational docs (cron.md, demo.md)
tests/       pytest suite (respx-mocked network, scratch databases)
reports/     generated evaluation/baseline/behavior reports
```
