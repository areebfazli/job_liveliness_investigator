# Role-Liveness Investigator — Main Project Spec

## 1. Product

Build an evidence-backed system for tech/startup job seekers that answers:

> **Is this role worth effort now, and what should I do next?**

### v1 actions

- `apply_now` — worth focused effort now
- `quick_apply` — apply with minimal tailoring
- `wait` — evidence is unresolved or hiring appears paused; include `recheck_after_days`
- `skip` — current evidence does not justify more effort

`message_first` is deferred until a verified contact exists.

`recheck_after_days` is deterministic: `min(14, days_until_validThrough)` when a declared expiry exists, otherwise a config default (initially 14).

### v1 non-goals

No definitive `ghost_job` label, LinkedIn scraping, application tracker, resume tailoring, crowd score, salary-compliance feature, generic company-health score, or uncalibrated 0–100 Apply Priority.

`evergreen`, `paused`, `pipeline_building`, etc. are hypotheses, not observable states.

### User output

```json
{
  "posting_state": "open | closed | reposted | unknown",
  "recommended_action": "apply_now | quick_apply | wait | skip",
  "recheck_after_days": null,
  "evidence_quality": "strong | mixed | weak",
  "hypotheses": [],
  "reason": [
    {"text": "Role was first published 11 days ago.", "evidence_ids": ["e1"]}
  ],
  "evidence": []
}
```

Do not expose numeric confidence until it is calibrated on held-out outcome data.

`evidence_quality` is deterministic:
- `strong`: primary/structured evidence supports the action and no material contradiction remains
- `mixed`: material evidence conflicts
- `weak`: key evidence is missing, archive-only, or affected by failures

The internal run trace, not the user output, stores probes, controller decisions, model/prompt IDs, cache hits, cost, latency, and errors.

---

## 2. System

```text
Job URL
  ↓
Resolver + current board snapshot                 deterministic
  ↓
Case file
  ↓
LLM Investigator
  - contradictions
  - unresolved questions
  - candidate probes + arguments
  ↓
Controller                                         deterministic
  - eligibility / history
  - schema + domain allowlist
  - budget / step limits
  - probe ranking
  - RUN / STOP
  ↓
One probe → append evidence → repeat              bounded
  ↓
Fixed action policy
  ↓
LLM evidence-cited explanation
```

### Responsibility split

**LLM:** interpret unstructured evidence, identify conflicts/missing information, propose investigations, explain results.

**Code / learned models:** source trust, timestamps, schemas, permissions, caching, budgets, hard stops, probe ranking, calibration, action policy.

Never rely on the LLM alone for budgets, probabilities, stopping, permissions, or the final action.

### Safety and reproducibility invariants

- Treat job pages/news as untrusted data; delimit them in prompts.
- Schema-validate model outputs and Pydantic-validate probe arguments.
- Enforce per-probe network-domain allowlists.
- Structured probe failure: `{ok:false,error,retryable}`; no uncontrolled retry loops.
- Cache tool results where valid.
- Cache LLM outputs by `(model_id, prompt_hash, structured_input_hash)` for exact benchmark replay; do not assume temperature 0 makes APIs deterministic.

---

## 3. Evidence and Sources

### Evidence item

```json
{
  "id": "e1",
  "probe": "resolve_posting",
  "claim_type": "first_published",
  "value": "2026-08-20T10:00:00Z",
  "source_url": "...",
  "raw_excerpt": "...",
  "source_quality": "ats_native | page_structured | archive | news | enrichment",
  "source_event_at": "...",
  "available_at": "...",
  "fetched_at": "..."
}
```

`available_at` means the earliest time the evidence is **verifiably available** to the system. Replay at historical time `T` may only expose:

```text
available_at <= T
```

For archived evidence, `available_at` is the capture time. Do not backdate current discoveries merely because the underlying event happened earlier.

Derived direction/weights are not raw evidence and are never assigned by the LLM.

### Identity

Use the normalized company website domain as `company_id`; store ATS board/tenant identifiers separately.

### Source policy

- **Greenhouse:** documented public API; `first_published` / `updated_at` → `ats_native`
- **Ashby:** documented public API; `publishedAt` → `ats_native`
- **Lever:** public current postings; undocumented date fields are not trusted as ATS-native publish dates
- **JSON-LD:** `JobPosting.datePosted` → `page_structured`
- **`validThrough`:** publisher-declared expiry when present; not proof of a ghost job
- **Wayback:** archive fallback/history only; always record capture coverage
- **Licensed enrichment:** optional source for recent team/hiring signals

Start with Greenhouse, Ashby, Lever, JSON-LD, and Wayback. Add ATS adapters only when the coverage audit justifies them.

---

## 4. Evidence Collection and Agent Loop

### Always run

| Component | Returns |
|---|---|
| `resolve_posting` | ATS, canonical job ID/URL, publish evidence |
| `board_snapshot` | current company/team openings |

### Dynamic probes

| Probe | Returns | Cost | History required |
|---|---|---:|:---:|
| `repost_history` | disappearance/reappearance + versions | low | yes |
| `requirements_drift` | structured version diff | medium | yes |
| `company_events` | dated layoffs, freezes, funding, expansion | medium | no |
| `team_signal` | recent similar hires/activity from licensed source | high / optional | no |

Rules:
- probes return facts, not verdicts
- history probes are ineligible without usable history
- `team_signal` is the only source for the policy input `corroborating_hiring_signal`; it is eligible only when that input is unknown and the repost/long-lived branch is reachable
- no crowd reports as direct v1 evidence

### History

Snapshot and Wayback CDX jobs use per-host rate limits with exponential backoff; a throttled or failed capture is recorded as a coverage gap, never as an absence.

Store daily board snapshots:

```text
company_id, captured_at, open_job_ids, titles, teams, locations
```

Derived history features always carry `history_days` / `history_coverage`; missing history never means flat hiring.

Match reposted/versioned roles using title, team, location, and description similarity. Keep thresholds configurable and validate match precision on a hand-checked sample of 50 matches.

Archive-derived closures are **interval-censored**: keep `last_seen_open` and `first_seen_absent`. Do not invent an exact `closed_at` from sparse captures.

For historical `company_events`, pre-collect dated events and replay by `available_at`; do not live-search during benchmark replay. Document that today's searchable news index is not a perfect reconstruction of historical search results.

### Agent loop

Start with at most `4` dynamic probe steps; tune cost/latency caps from measured runs.

```text
1. resolver + board snapshot
2. build case state
3. investigator → conflicts, unresolved questions, candidate probes+args, or STOP
4. controller filters invalid/ineligible candidates
5. controller ranks candidates
6. execute best probe
7. append evidence
8. repeat until STOP / budget / step cap
9. fixed action policy
10. evidence-cited explanation
```

Hard stop if:
- no eligible probe is worth its cost
- budget/latency/step cap is reached
- the same probe+arguments would repeat
- no unresolved question could change the action

An **unresolved question** is a policy input (§5) that is still unpopulated. The controller computes the set of unpopulated policy inputs; a candidate probe is eligible only if it can populate at least one of them. This makes the stop rule deterministic and testable.

### Probe ranking lifecycle

**v1:** deterministic cost-aware ranking over valid candidates.

**Optional upgrade:** after enough replay data exists, train a small calibrated estimator on `(case_state, candidate_probe)` rows to predict whether that probe would move the partial-information action toward the full-probe reference action.

Start with logistic regression; try boosted trees only if held-out results materially improve. Combine predicted value with measured money cost, latency, and failure rate. If learned ranking does not beat deterministic ranking, remove it.

---

## 5. Decision and Outcomes

### Observable state

`open | closed | reposted | unknown`

Own snapshots record:

```text
first_observed
last_seen_open
first_seen_absent
reappeared_at
replacement_job_id
content_hash
```

Never encode `closed = filled` or `long_lived = ghost`.

### v1 action policy

The policy is explicit, inspectable, tuned once on development/temporal-validation data, then frozen before final evaluation.

```text
closed                                             → skip
open + recent primary publish evidence
     + no material negative event                  → apply_now
open + mixed/weak evidence                         → quick_apply
open + explicit freeze/pause or declared expiry
     + unresolved current status                   → wait
repeated unchanged repost + long-lived history
     + corroborating weak hiring signals           → skip
otherwise                                          → quick_apply
```

Policy inputs are: `posting_state`, `publish_recency` (from resolver), `evidence_quality`, `material_negative_event` and `freeze_or_pause` (from `company_events`), `declared_expiry` (from resolver), `repost_pattern` (from `repost_history` + `requirements_drift`), `corroborating_hiring_signal` (from `team_signal`). Terms such as `recent`, `long-lived`, and `material negative event` are configuration with documented frozen thresholds. This policy is a user-effort heuristic, not a claim about employer intent.

### Outcome data

**Posting behavior:** closure/repost timing from snapshots. Open postings are right-censored; archive-derived closures are interval-censored (`last_seen_open`, `first_seen_absent`). Use a library with interval-censored fitters (lifelines) for evaluation, or, if using scikit-survival, use interval midpoints with interval width recorded as a covariate and say so in the report. Do not use posting survival probability as the v1 action engine.

**Personal outcomes:**

`applied | reply | screen | interview | offer | rejection | silence`

`screen`, `interview`, and `offer` strongly indicate the opportunity was active for that user. `silence` is not evidence that the role was fake.

Personalized ranking, Apply Priority, and `message_first` are later features after enough outcome/contact data exists.

---

## 6. Evaluation

### Data foundation

Start live snapshots on day 0. Attempt 6–12 months of Wayback backfill, recording capture density/coverage.

Initial target before headline evaluation:
- ≥300 postings
- ≥40 companies
- ≥100 observed closure events

These are project targets, not statistical guarantees. If archive coverage is sparse, extend live collection or reduce the claimed scope rather than manufacturing labels.

### Systems

All systems use the same frozen action policy.

- **A — Full probes:** every available dynamic probe
- **B — Rules:** deterministic routing baseline
- **C — Agent:** LLM investigator + deterministic controller/ranking
- **C2 — Agent + learned probe ranking:** only if enough data exists

Freeze/version B before evaluating C.

### Replay

At historical time `T`:
- expose only evidence with `available_at <= T`
- expose a dynamic result only if the simulated system selects that probe
- live **tool** calls are forbidden in replay; results come only from the cached full-probe record
- live **LLM** calls are allowed on cache miss (e.g. a changed investigator prompt) and are recorded, so the cache is complete for subsequent runs

### Splits

1. **Temporal holdout:** later postings excluded from training/tuning
2. **Company holdout:** test companies absent from training

### Metrics

**Data quality:** ATS resolution, publish-date coverage by source quality, extraction correctness, repost-match precision, citation support, future-leakage violations (`0` target).

**Posting behavior:** censoring-aware closure/repost curves, calibration/ranking metrics where sample size supports them.

**Agent efficiency:** action agreement with A (overall and macro-averaged per action class, reported with the action distribution so a default-heavy policy cannot pass trivially), medium/high-cost probe count, total cost, latency, repeated calls, invalid arguments, recovery after failures, early-stop regret, unnecessary probes.

**Later product value:** effort per screen/interview and outcome rates by recommended action.

### Two separate gates

**Agent gate:** C is worth keeping if it materially lowers investigation cost while preserving A-like decisions. Default target:

```text
C medium/high-cost probe use <= 70% of B
AND C action agreement with A >= B agreement with A - 2 percentage points
    (both overall and macro-averaged per action class)
```

Report absolute cost/latency too. If rules are equally good and simpler, remove the agent.

**Product gate:** do not claim the recommendations improve job-search outcomes until held-out personal outcome data shows better effort-per-screen/interview or another predeclared user metric.

---

## 7. Storage and Stack

### SQLite

```text
companies
postings
posting_snapshots
board_snapshots
company_events
evidence
runs
run_steps
outcomes
```

`run_steps` is the canonical trace: controller/model decisions, prompt/model hashes, cache status, cost, latency, and errors.

### Stack

```text
Python, FastAPI, SQLite, Pydantic, httpx
BeautifulSoup / JSON-LD parser
scikit-learn
lifelines (interval-censored fitters) or scikit-survival with midpoint approximation
one structured-output-capable LLM API
cron/scheduler, pytest
```

OpenTelemetry/tracing UI is optional. Build the product UI only after replay/evaluation works.

---

## 8. Project Lifecycle

### Phase 0 — Data foundation
- audit target companies and ATS coverage
- finalize posting/evidence schemas
- start daily posting + board snapshots
- attempt archive backfill; measure coverage

**Exit:** automated snapshots work, resolver covers most targets, evaluation data is feasible.

### Phase 1 — Evidence layer
- Greenhouse/Ashby/Lever + JSON-LD
- snapshot store
- repost/version matching
- board-history features
- historical company-event store

**Exit:** case files contain timestamped, source-linked facts only.

### Phase 2 — Baselines and replay
- dynamic probes
- full-probe runner
- fixed action policy
- deterministic rules baseline
- point-in-time replay dataset
- censoring-aware posting-behavior analysis

**Exit:** A/B, frozen policy, replay data, and baseline metrics exist.

### Phase 3 — Agent
- structured investigator output
- bounded loop
- controller validation/budgets/ranking
- cache + failure handling
- evidence-cited explanation
- optional learned probe ranking if data supports it

**Exit:** C works on cached replay and live supported postings.

### Phase 4 — Evaluation
- A vs B vs C (and C2 if built)
- temporal + company holdouts
- data, behavior, agent, cost/latency, and failure metrics
- agent go/no-go decision

**Exit:** one report proves or disproves the value of agentic orchestration.

### Phase 5 — Product shell
- URL input
- recommended action + recheck timing
- evidence timeline
- optional role-watch notification
- README with limitations and go/no-go result
- demo: clear case, ambiguous case, failure/recovery case

---

## 9. Definition of Done

- supported URLs resolve and snapshot correctly
- all evidence is source-linked, timestamped, and point-in-time safe
- archive uncertainty/coverage is explicit; no fabricated exact closure dates
- inputs/arguments are schema-validated and network domains allowlisted
- the agent cannot exceed hard budgets or repeat identical calls
- benchmark replay uses cached model/tool outputs
- A/B/C share the same frozen action policy and held-out data
- evaluation never equates `closed` with `filled` or `long-lived` with `ghost`
- cost, latency, failure, citation quality, and leakage are measured
- every user-facing reason maps to evidence
- the README reports both agent and product limitations, even if rules win

---

## 10. Technical References

- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html) — documented `first_published` / `updated_at`
- [Ashby Public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api) — documented `publishedAt`
- [Lever Postings API](https://github.com/lever/postings-api) — public published-job access; undocumented dates are not treated as trusted ATS-native dates
- [Google JobPosting](https://developers.google.com/search/docs/appearance/structured-data/job-posting) — `datePosted`; `validThrough` only when an expiration date exists
- [lifelines](https://lifelines.readthedocs.io/) — interval-censored fitters for archive-derived closures
- [scikit-survival](https://scikit-survival.readthedocs.io/en/stable/user_guide/00-introduction.html) — right-censored survival data (midpoint approximation if used for archive data)
- [TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) / [GroupKFold](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.GroupKFold.html) — temporal and grouped evaluation
