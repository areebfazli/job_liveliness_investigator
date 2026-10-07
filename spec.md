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

#### Amendment 2026-10-07 (Ashby publish dates)

Ashby documents `publishedAt` as "when the job was last published" ([Ashby Public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api)): a re-publish moves it, so on its own it is not a first-publication date. Since the 2026-10-06 correction the code stores and cites it as `last_published`, never as `first_published`. The Ashby line above is kept verbatim as **before**; the version below is **after** and is what the code implements from this date.

| Rule | Before | After |
|---|---|---|
| Ashby `publishedAt` | documented public API; `publishedAt` → `ats_native` | documented public API; `publishedAt` is the **last** publish date → `ats_native` `last_published`: a refresh candidate under §5 (it counts toward recency only when it coincides with an observed content-hash change), never first-publish evidence on its own |
| Ashby first publish (new row) | — | an Ashby `publishedAt` value `v` is also an `ats_native` `first_published` claim when (1) our own collection captured the company's board before the posting's first own sighting `first_observed`, and the last complete own capture before it (time `t_absent`) did not list the job — archive captures never count, and a failed or partial capture is a coverage gap, not an absence; (2) `t_absent < v <= first_observed`, with the first-published guard's pre-fix snapshot run-window slack on `first_observed`, and an absence stamped inside a pre-fix run window taken at that window's end; and (3) `first_observed − v <= ashby_first_seen_max_lag_days` (config, initially 2). `v` may come from any own capture that carried it; `source_event_at = v`, and `available_at` is the earliest own capture at or before `T` that carried `v`, never earlier. Every other Ashby posting keeps `last_published` only. Greenhouse and Lever are unchanged. |

A job absent from our earlier capture and first seen shortly after its stated publish time cannot have been published earlier and re-published in between, so its last publication is its first. A later re-publish changes the value, so an unchanged early value proves no re-publish happened in between, and a later value never changes what an earlier `T` sees.

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

#### Amendment 2026-09-10 (policy tuning, before freeze)

Replay on real data showed the original table left the agent with no reachable question: `apply_now` needed a first publish within 14 days, `skip` needed a licensed hiring signal that does not exist, and `long-lived` was measured from our own first snapshot. The table above is kept verbatim as **before**; the version below is **after** and is what the code implements from this date.

| Rule | Before | After |
|---|---|---|
| `recent` | first publish ≤ 14 days ago | latest of first publish **or** an ATS `updated_at` that coincides with an observed content-hash change, ≤ 30 days ago |
| `long-lived` | days since our own `first_observed` ≥ 180 | days since the earliest of ATS first publish / earliest archive capture / own `first_observed` ≥ 180 |
| `corroborating_hiring_signal` source | licensed enrichment only | `team_signal` probe derived from own + archive board snapshots: new roles on the same team in the last 30 days, or closures on the same team in the last 60 days; unknown when team history < `min_history_days` |
| `wait` (new row) | — | open + material negative event inside the window + posting not refreshed since that event → `wait`, `recheck_after_days` = default |

```text
closed                                             → skip
open + unresolved posting state                    → wait
open + explicit freeze/pause or declared expiry
     + unresolved current status                   → wait
open + material negative event after last refresh  → wait      (new)
repeated unchanged repost + long-lived history
     + corroborating hiring signal = false         → skip
open + recent (see table) + strong evidence
     + no material negative event                  → apply_now
open + mixed/weak evidence                         → quick_apply
otherwise                                          → quick_apply
```

`team_signal` remains the only source for `corroborating_hiring_signal` and keeps its medium/high cost tier so the controller must still justify running it. Its data source is now first-party board history rather than a licensed feed; §4's table row is read accordingly. All new thresholds live in `config.toml` and are frozen with the rest of the policy.

#### Amendment 2026-09-12 (positive hiring-activity path, before freeze)

Evaluation on rebuilt datasets showed that among live, strong-evidence open roles the policy says `apply_now` only when the role was published or refreshed within `recent_publish_days`; an open role older than that can never rise above `quick_apply`, whatever the employer is doing. The 2026-09-10 table is kept verbatim as **before**; the version below is **after** and is what the code implements from this date.

| Rule | Before | After |
|---|---|---|
| `apply_now` via hiring activity (new row) | — | open + strong evidence + no material negative event + `corroborating_hiring_signal` = true → `apply_now`, regardless of publish recency |
| `team_signal` eligibility (§4) | only when `corroborating_hiring_signal` is unknown **and** the repost/long-lived skip branch is reachable | only when `corroborating_hiring_signal` is unknown **and** either the repost/long-lived skip branch **or** this hiring-activity `apply_now` branch is reachable |

```text
closed                                             → skip
open + unresolved posting state                    → wait
open + explicit freeze/pause or declared expiry
     + unresolved current status                   → wait
open + material negative event after last refresh  → wait
repeated unchanged repost + long-lived history
     + corroborating hiring signal = false         → skip
open + recent + strong evidence
     + no material negative event                  → apply_now
open + strong evidence + no material negative event
     + corroborating hiring signal = true          → apply_now   (new)
open + mixed/weak evidence                         → quick_apply
otherwise                                          → quick_apply
```

The new row sits after the recency `apply_now` row so a recent role never needs the probe. `corroborating_hiring_signal` keeps its single source (`team_signal`, first-party board history) and its cost tier. The deterministic rules baseline (System B) is re-versioned to route `team_signal` whenever this row is reachable, so A, B and C are compared under the same table.

#### Amendment 2026-10-07 (Ashby publish dates in recency, before freeze)

§3's amendment of the same date changes what counts as an ATS first publish for Ashby. The two 2026-09-10 rows that name it are kept verbatim as **before**; the versions below are **after** and are what the code implements from this date. The action table itself is unchanged.

| Rule | Before | After |
|---|---|---|
| `recent` | latest of first publish **or** an ATS `updated_at` that coincides with an observed content-hash change, ≤ 30 days ago | latest of first publish (for Ashby, only a `publishedAt` that qualifies under §3's amendment 2026-10-07) **or** an ATS `updated_at` or Ashby `publishedAt` (last published) that coincides with an observed content-hash change, ≤ 30 days ago |
| `long-lived` | days since the earliest of ATS first publish / earliest archive capture / own `first_observed` ≥ 180 | days since the earliest of ATS first publish (for Ashby, only a qualifying `publishedAt`) / earliest archive capture / own `first_observed` ≥ 180 |

The new threshold `ashby_first_seen_max_lag_days` lives in `config.toml` and is frozen with the rest of the policy.

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
