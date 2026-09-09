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
#   add --with-c to also score System C (needs a reachable LLM endpoint —
#   see "LLM setup" below; otherwise C is recorded as "not run: no LLM
#   endpoint configured" and the agent gate reads "not run")
uv run rli eval gates --dataset my-dataset --db ./data/rli.db

# 9. Investigate one URL directly (System A = full probes, System B = rules)
uv run rli run --system B --url https://boards.greenhouse.io/<tenant>/jobs/<id> --db ./data/rli.db

# 10. System C: the bounded LLM agent (needs an LLM endpoint — see "LLM setup")
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
default) need a reachable LLM endpoint (next section); without one, System
C is untested live in this environment (see Limitations) and the API
transparently falls back to System B, marking the response
`degraded: true`.

`uv run rli --help` and `<command> --help` are the source of truth for every
flag above — this recipe was verified against that output, not against a
stale description.

## LLM setup (System C)

System C talks to exactly **one** kind of API: an OpenAI-compatible
chat-completions endpoint (`POST {base_url}/chat/completions`). There is no
vendor SDK and no per-provider code path (`OpenAICompatibleClient` in
`rli/llm/client.py` is a direct httpx call), so anything that speaks that
protocol works and switching providers is a two-line config change.

Three ways to point it somewhere, in `config.toml`'s `[llm]` table:

**1. A local model with Ollama (the shipped default — free, no API key, no
data leaves the machine).**

```bash
ollama serve                 # in its own shell
ollama pull qwen3:8b         # ~5 GB; qwen3:4b is the smaller alternative
uv run rli agent run --url https://boards.greenhouse.io/<tenant>/jobs/<id> --db ./data/rli.db
```

```toml
[llm]
provider = "openai_compatible"
base_url = "http://localhost:11434/v1"
model_id = "qwen3:8b"
```

Pick a model that can hold a structured output format: both System C prompts
demand a JSON object matching a schema. On a CPU-only machine expect tens of
seconds per call, so raise `[llm].timeout_s` rather than lowering it.

**2. Google Gemini's free tier (an API key, no local RAM).**

```bash
export GEMINI_API_KEY=...     # https://aistudio.google.com/apikey
```

```toml
[llm]
provider = "openai_compatible"
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
model_id = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
```

**3. Any other OpenAI-compatible endpoint** — vLLM, llama.cpp's server, LM
Studio, OpenAI, an internal gateway. Set `base_url`, `model_id`, and
`api_key_env` to whichever environment variable holds the credential
(default `LLM_API_KEY`). The key itself never appears in `config.toml`, in
a log line, or in an error message — `OpenAICompatibleClient` scrubs it from
every message it raises.

Structured output is requested as `response_format: json_schema` with the
Pydantic model's own schema. A server that answers HTTP 400 to that is
retried once with `response_format: json_object` and the schema inlined in
the system prompt, and the client remembers that for the rest of the run
rather than re-probing on every call.

**Cost accounting is fully config-driven.** `[llm.prices."<model id>"]` maps
a model id to `input_usd_per_mtok` / `output_usd_per_mtok` (USD per 1,000,000
tokens), and that table is the only source of the `cost_usd` figures in
`run_steps`, in `runs.total_cost_usd`, and in the `[budgets]` ledger the
controller enforces.
Local models are listed at `0.0` (they are free); a model id absent from the
table costs `0.0` and does not raise, so a paid model you add must also be
priced or it will look free to the budget cap. Prices are never sent to the
API and are not part of the LLM cache key, so re-pricing does not invalidate
any cached model output.

A live end-to-end check of whatever you configured:

```bash
RLI_LLM_LIVE=1 uv run pytest tests/test_llm_live.py -q
```

It skips (never fails) when `RLI_LLM_LIVE` is unset or the endpoint does not
answer.

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

**Company-events coverage is headline-only.** All **78 of 78** target companies
have been searched (15 via web search on 2026-09-07, the remaining 63 via
Google News RSS feeds on 2026-09-08 after the web-search budget ran out), giving
308 dated events. Event dates are article publication dates and materiality is
judged from headlines, so some layoffs are recorded without a confirmed
percentage or headcount. Events live in `data/events/company_events.csv` and
must be loaded with `rli load-events` before any run or evaluation; an earlier
evaluation was generated before that step existed and saw no events at all.

**System C (the LLM agent) has not been exercised live in this
environment.** No LLM endpoint was reachable during development or in the
last evaluation run, so System C's investigator/controller loop has 1,103
passing offline/mocked tests but zero recorded live model calls, and
`reports/evaluation.md`'s agent gate reads **NOT RUN** rather than pass or
fail — System C produced zero scoped runs, so it has not failed, it has
never been graded. (The reports in `reports/` predate the move to an
OpenAI-compatible client and still name the old provider's key; they are
generated artifacts and were not rewritten by hand.) The product API
(`POST /investigate`) probes the configured endpoint and transparently
degrades to System B (`degraded: true`, with the probe's reason in
`degraded_reason`), and also degrades if every System C model call in a run
errors out.

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

**Status (2026-09-08): NOT DECIDED.** See `reports/evaluation.md` (temporal dev split, 300 postings / 78 companies / 1,278 replay cases) and `reports/evaluation_company_split.md` (150 postings / 33 companies).

| measure | A | B | C |
|---|---|---|---|
| runs | 1,185 | 1,185 | not run (no LLM endpoint was reachable) |
| action distribution | apply_now 17 · quick_apply 1,053 · skip 115 | identical | n/a |
| agreement with A (overall / macro) | — | 100% / 100% | n/a |
| medium/high probes per run | 1.78 | 1.42 | n/a |
| probe cost points per run | 7.52 | 6.77 | n/a |
| leakage violations | 0 | 0 | n/a |

The agent gate (C medium/high probe use ≤ 70% of B and agreement with A within 2 points of B's) cannot be evaluated until System C runs against a live LLM endpoint. Rules (B) currently reproduce A exactly at 90% of A's probe cost, so if C does not beat that, the spec says to remove the agent.

Read the 100% agreement with the action distribution: almost every replay case is archive-era and therefore `weak` evidence, which routes to `quick_apply` by construction. This is not proof that B is as good as A on live-era postings.

## Product gate

**UNPROVEN.** The `outcomes` table is empty. No claim about job-search outcomes is made. Record outcomes via `POST /outcomes` and re-run `rli eval gates`.

## Product shell

`PROGRESS.md` still lists M7 as "todo" as of this writing, but the code
below exists, is exercised by `tests/test_api.py` (17 passing tests), and
was manually verified against a scratch database while writing this
document (see `docs/demo.md`).

`uv run python -m rli.api` starts the FastAPI app (`rli.api.app:app`) via
uvicorn.

- `POST /investigate {url, system?: "A"|"B"|"C"}` — `system` defaults to
  `"C"`; falls back to System B with `degraded: true` when the configured
  LLM endpoint is unreachable (or has no credential and is not local), or
  when every System-C model call fails. Returns
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

1,103 tests pass, 1 is skipped by design (`tests/test_llm_live.py`: the
live-LLM check, which runs only with `RLI_LLM_LIVE=1` and a reachable
endpoint). `ruff check .` is clean. Per `PROGRESS.md`'s milestone table:
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
