# Evaluation report (spec.md §6 / PLAN.md M6)

Dataset: `company-7d-v4` (`split_name=dev`, `split_kind=company`) · Splits read: `dev, validation, test` · `allow_test=True` · Policy version: `policy-v1:2efb6db87f27c96b9691003d36426b26` · Generated: `2026-10-08T09:02:14.252085Z`

## Headline gate (sample sizes)

| measure | built (dataset row) | SCORED (this evaluation) | collection corpus | spec §6 target | met? (judged on scored) |
| --- | --- | --- | --- | --- | --- |
| postings | 330 | 330 | 40514 | >= 300 | yes |
| companies | 64 | 64 | 351 | >= 40 | yes |
| closure events | n/a (a replay case is a (posting, T) grid point, not a closure) | n/a | 23900 | >= 100 | yes (corpus-wide) |
| replay cases | 1452 | 1452 | — | — | — |
| excluded from scoring | — | identity_unresolved=0, unassigned=0, holdout not read=0 | — | — | — |
| live-era cases (T >= the company's first own capture) | — | 1447 | — | — | — |
| archive-era cases | — | 5 | — | — | — |
| live-era share | — | 99.7% | — | — | — |

Era boundary is PER COMPANY: a case is live-era only when its T is at or after its own company's first own board capture. 64 of 64 companies in scope have any own capture; the earliest own capture anywhere is `2026-09-07T17:43:51.713871Z`. See the 'Era split: live vs. archive' section below.

Headline gate (postings and companies judged on SCORED data): **MET**.

The size columns are NOT interchangeable. 'built' is what the replay dataset was built with; 'SCORED' is what this evaluation actually scored — cases that passed the split gate and whose posting identity resolved. The collection corpus may clear spec.md §6's targets while the scored slice does not. The closure-event leg has no dataset-scoped equivalent and is judged corpus-wide, which makes it the optimistic leg of this verdict.

## Systems run

| system | status | scoped runs |
| --- | --- | --- |
| A | reused | 1452 |
| B | reused | 1452 |
| C | skipped: not requested (pass with_c=True / --with-c) | 0 |
| R | reused | 1452 |

`reused` means the dataset already carried that system's replay runs and they were scored as-is; `ran` means `rli.replay.run.run_replay` was invoked (offline by construction — the replay net client raises on any live call). No dataset was built and no network call was made by this evaluation.

## Case-set accounting

| item | value |
| --- | --- |
| cases collected | 1452 |
| systems | A, B, C, R |
| allowed splits (read-side) | dev, validation, test |
| runs per system | A=1452 B=1452 C=0 R=1452 |
| identity unresolved (no run resolved the posting; split from `replay_cases`) | 0 ((none)) |
| unassigned (no posting / posting absent from the split map) | 0 |
| excluded holdout (split outside the read-side filter) | 0 |
| duplicate re-runs collapsed to the latest | 0 |

A case is `(input_url, replay_at)`. An unassignable posting is EXCLUDED rather than defaulted into a split — an unknown split could, for all this report knows, be the holdout.

## Holdout and splits

Split assignment: FROZEN per case at build time (`replay_cases.split`).

Company holdout: company holdout check (test companies from the stable company-hash split; 70 test companies, 1 other non-test dataset(s) checked): CLEAN - retired, ignored: company-150-v2 (4 test companies, 92 cases), company-150-v3 (4 test companies, 93 cases), company-7d (26 test companies, 497 cases), company-7d-v2 (0 test companies, 0 cases), company-7d-v3 (0 test companies, 0 cases), company-dev-150 (4 test companies, 90 cases), dev-300 (10 test companies, 146 cases), dev-300-v2 (10 test companies, 148 cases), dev-300-v3 (10 test companies, 149 cases), dev-7d (50 test companies, 1435 cases), dev-7d-v2 (0 test companies, 0 cases), dev-7d-v3 (0 test companies, 0 cases).

Stable company holdout excluded at build time: `{"fractions": [0.6, 0.2, 0.2], "method": "company-hash", "seed": 20260607, "test_companies_excluded": 70}`.

| split | cases read | A runs | B runs | R runs |
| --- | --- | --- | --- | --- |
| dev | 1452 | 1452 | 1452 | 1452 |
| validation | 0 | 0 | 0 | 0 |
| test | 0 | 0 | 0 | 0 |

Not read: 0 case(s) in a split outside the filter (`dev, validation, test`), 0 unassigned. Read but not scored (identity unresolved), by split: (none).

The `test` holdout was permitted by the read-side filter but NO test-split case is in this dataset, so no holdout data was read.

## Era split: live vs. archive

Archive-era cases are weak by construction, not because of anything either system under test did. For any replay case whose `replay_at` predates its company's own board-snapshot collection, the only observation available is a Wayback capture, so `board_snapshot` evidence is sparse, `source_quality='archive'`, and often absent entirely (spec.md §1 classifies Wayback-only evidence as weak). The pooled agreement figures elsewhere in this report average the two eras together.

Era boundary is PER COMPANY: a case is live-era only when its T is at or after its own company's first own board capture. 64 of 64 companies in scope have any own capture; the earliest own capture anywhere is `2026-09-07T17:43:51.713871Z`.

Cases: live-era=1447, archive-era=5, live-era share=99.7%.

### `live-era` — product-relevant view

**A**

| measure | value |
| --- | --- |
| action distribution | apply_now=85 quick_apply=1228 skip=84 wait=50 |
| evidence_quality distribution | mixed=2 strong=214 weak=1231 |
| overall agreement with A | 100.0% |
| macro agreement with A | 100.0% |
| medium/high probes per run | 1.11 |

**B**

| measure | value |
| --- | --- |
| action distribution | apply_now=85 quick_apply=1228 skip=84 wait=50 |
| evidence_quality distribution | mixed=2 strong=214 weak=1231 |
| overall agreement with A | 100.0% |
| macro agreement with A | 100.0% |
| medium/high probes per run | 1.00 |

**R**

| measure | value |
| --- | --- |
| action distribution | apply_now=85 quick_apply=1228 skip=84 wait=50 |
| evidence_quality distribution | mixed=2 strong=214 weak=1231 |
| overall agreement with A | 100.0% |
| macro agreement with A | 100.0% |
| medium/high probes per run | 0.95 |

### `archive-era`

**A**

| measure | value |
| --- | --- |
| action distribution | quick_apply=5 |
| evidence_quality distribution | weak=5 |
| overall agreement with A | 100.0% |
| macro agreement with A | 100.0% |
| medium/high probes per run | 1.00 |

**B**

| measure | value |
| --- | --- |
| action distribution | quick_apply=5 |
| evidence_quality distribution | weak=5 |
| overall agreement with A | 100.0% |
| macro agreement with A | 100.0% |
| medium/high probes per run | 1.00 |

**R**

| measure | value |
| --- | --- |
| action distribution | quick_apply=5 |
| evidence_quality distribution | weak=5 |
| overall agreement with A | 100.0% |
| macro agreement with A | 100.0% |
| medium/high probes per run | 1.00 |

### Strong evidence and apply_now by grid point

BUILD-TIME cases are those with T at or after the dataset's build start (`2026-10-07T23:50:53.813461Z`): a still-open posting's last grid point is dated by its own live observation during the build, so build-time cases carry one T per posting (246 case(s) over 246 T value(s) here). Strong = System A's evidence quality, which every system shares on a case.

| scope | grid points | build-time cases | strong at build time / all strong | grid points with any strong | apply_now at build time / all apply_now |
| --- | --- | --- | --- | --- | --- |
| all cases | 306 | 246 over 246 T value(s) | 153/214 (71.5%) | 166 | A 62/85, B 62/85, R 62/85 |
| live-era | 305 | 246 over 246 T value(s) | 153/214 (71.5%) | 166 | A 62/85, B 62/85, R 62/85 |
| archive-era | 1 | 0 over 0 T value(s) | 0/0 (n/a) | 0 | A 0/0, B 0/0, R 0/0 |

Grid points with any strong-evidence or apply_now case (latest 40 of 166 shown); 140 grid point(s) have neither:

| T | cases | strong (A) | apply_now (A) | apply_now (B) | apply_now (R) |
| --- | --- | --- | --- | --- | --- |
| 2026-10-08T00:00:13.164421Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:00:20.467010Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:00:23.522240Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:00:29.923569Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:00:31.579189Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:00:34.605394Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:00:35.930042Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:00:39.853888Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:00:52.809717Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:00:56.222693Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:08.730513Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:12.570951Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:21.360272Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:23.021498Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:30.334629Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:32.377287Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:39.942947Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:43.646237Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:45.239909Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:01:46.471594Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:01:49.475842Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:50.996953Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:53.736412Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:01:55.502437Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:01:57.216371Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:05.246831Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:14.293778Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:02:17.292337Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:21.310632Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:22.330927Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:23.256972Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:26.369964Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:02:27.900298Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:02:35.981564Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:48.519240Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:02:52.228564Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:02:56.519362Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:02:57.887462Z | 1 | 1 | 0 | 0 | 0 |
| 2026-10-08T00:03:01.967118Z | 1 | 1 | 1 | 1 | 1 |
| 2026-10-08T00:03:03.814601Z | 1 | 1 | 1 | 1 | 1 |

### Live-era gate (informational)

This is an INFORMATIONAL, NON-authoritative re-run of the spec.md §6 agent gate, scoped to live-era cases only, using the same three legs and thresholds. **The pooled 'Agent gate' section below remains the authoritative spec.md §6 verdict** — nothing here replaces it; this exists only to show whether that verdict would look different on the product-relevant (live-era) slice.

| leg | candidate (C) | baseline (B) | requirement | pass? |
| --- | --- | --- | --- | --- |
| medium/high probes per run | n/a | 1.00 | ratio n/a <= 0.70 | n/a |
| overall agreement with A | n/a | 100.0% | >= n/a | n/a |
| macro agreement with A | n/a | 100.0% | >= n/a | n/a |

Live-era gate verdict (informational, NON-authoritative): **NOT RUN**.

## Action distributions

| action | A | B | R |
| --- | --- | --- | --- |
| apply_now | 85 | 85 | 85 |
| quick_apply | 1233 | 1233 | 1233 |
| skip | 84 | 84 | 84 |
| wait | 50 | 50 | 50 |

spec.md §6 requires agreement to be read WITH the action distribution: a default-heavy policy posts high overall agreement trivially.

System C has no action distribution: skipped: not requested (pass with_c=True / --with-c).

## Agreement with System A

| system | paired cases (with A) | overall agreement | macro agreement |
| --- | --- | --- | --- |
| A | 1452 | 100.0% | 100.0% |
| B | 1452 | 100.0% | 100.0% |
| R | 1452 | 100.0% | 100.0% |

`A` compared against itself is trivially 100% and is shown so that A's own probe, cost and latency figures have a row in every table below.

System C: not run: no LLM endpoint configured.

### Per-class agreement

**A vs A** (classes are System A's actions)

| A action | n (A count) | A agreement |
| --- | --- | --- |
| apply_now | 85 | 100.0% |
| quick_apply | 1233 | 100.0% |
| skip | 84 | 100.0% |
| wait | 50 | 100.0% |

**B vs A** (classes are System A's actions)

| A action | n (A count) | B agreement |
| --- | --- | --- |
| apply_now | 85 | 100.0% |
| quick_apply | 1233 | 100.0% |
| skip | 84 | 100.0% |
| wait | 50 | 100.0% |

**R vs A** (classes are System A's actions)

| A action | n (A count) | R agreement |
| --- | --- | --- |
| apply_now | 85 | 100.0% |
| quick_apply | 1233 | 100.0% |
| skip | 84 | 100.0% |
| wait | 50 | 100.0% |

### Confusion matrices

**A action (row) -> A action (column)**

| A \ A | apply_now | quick_apply | skip | wait |
| --- | --- | --- | --- | --- |
| apply_now | 85 | 0 | 0 | 0 |
| quick_apply | 0 | 1233 | 0 | 0 |
| skip | 0 | 0 | 84 | 0 |
| wait | 0 | 0 | 0 | 50 |

**A action (row) -> B action (column)**

| A \ B | apply_now | quick_apply | skip | wait |
| --- | --- | --- | --- | --- |
| apply_now | 85 | 0 | 0 | 0 |
| quick_apply | 0 | 1233 | 0 | 0 |
| skip | 0 | 0 | 84 | 0 |
| wait | 0 | 0 | 0 | 50 |

**A action (row) -> R action (column)**

| A \ R | apply_now | quick_apply | skip | wait |
| --- | --- | --- | --- | --- |
| apply_now | 85 | 0 | 0 | 0 |
| quick_apply | 0 | 1233 | 0 | 0 |
| skip | 0 | 0 | 84 | 0 |
| wait | 0 | 0 | 0 | 50 |

System C has no confusion matrix: not run: no LLM endpoint configured.

## Probe-dependent cases (informative agreement)

Agreement with A is only informative where a dynamic probe could have mattered. For every case the frozen policy is re-applied to System A's own ALWAYS-RUN evidence (resolver, board snapshot, archive board state, refresh match; dynamic-probe evidence removed; same T, same always-run failures). A case is PROBE-DEPENDENT when A's recorded action differs from that no-probe action. On every case outside the subset every system agrees with A (no disagreement was found there), so overall/macro agreement is inflated by them.

| measure | value |
| --- | --- |
| cases with a A run | 1452 |
| no-probe action computed | 1452 |
| probe-dependent cases | 67 (4.6%) |
| no-probe -> recorded action (probe-dependent) | apply_now -> quick_apply=8 quick_apply -> apply_now=9 quick_apply -> wait=50 |
| no-probe action distribution | apply_now=84 quick_apply=1284 skip=84 |
| self-check: runs with no dynamic probe reproduced | 168/168 |

The subset is defined from System A's action ONLY. A case where A equals the no-probe action but another system disagrees with A is OUTSIDE the subset and is counted in the pooled figures only; such cases, per system: (none).

The self-check re-derives the action for every scoped run (any system) that ran no dynamic probe; there the no-probe action must equal the recorded one, so anything short of all of them means the reconstruction is wrong and this section should not be trusted.

### Agreement with A on probe-dependent cases

| system | paired probe-dependent cases | overall (probe-dependent) | macro (probe-dependent) | overall (all cases) | macro (all cases) |
| --- | --- | --- | --- | --- | --- |
| A | 67 | 100.0% | 100.0% | 100.0% | 100.0% |
| B | 67 | 100.0% | 100.0% | 100.0% | 100.0% |
| R | 67 | 100.0% | 100.0% | 100.0% | 100.0% |

### Agent-gate legs on probe-dependent cases (informational)

The spec.md §6 legs (C vs B, agreement with A) recomputed on the probe-dependent subset only. INFORMATIONAL: the pooled 'Agent gate' section stays the spec verdict.

| leg | candidate (C) | baseline (B) | requirement | pass? |
| --- | --- | --- | --- | --- |
| medium/high probes per run | n/a | 1.27 | ratio n/a <= 0.70 | n/a |
| overall agreement with A | n/a | 100.0% | >= n/a | n/a |
| macro agreement with A | n/a | 100.0% | >= n/a | n/a |

Probe-dependent legs (informational): **NOT RUN**.

## Cost and latency (probe cost points and model dollars reported SEPARATELY)

| system | runs | probe steps | probe cost POINTS (total) | probe cost POINTS (mean/run) | model steps | model cost USD (total) | model cost USD (mean/run) | tokens in/out | latency ms (total) | latency ms (mean/run) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 1452 | 4584 | 8324.00 | 5.73 | 0 | $0.0000 | $0.0000 | 0/0 | 2424 | 2 |
| B | 1452 | 4431 | 7398.00 | 5.10 | 0 | $0.0000 | $0.0000 | 0/0 | 2478 | 2 |
| R | 1452 | 4281 | 7098.00 | 4.89 | 0 | $0.0000 | $0.0000 | 0/0 | 1446 | 1 |

**These are two different units and are NEVER summed.** `run_steps.cost_usd` holds unitless placeholder cost POINTS on `component='probe'` rows (configured in `[probe_costs]`: low=1, medium=3, high=10) and REAL DOLLARS on `component='model'` rows. `runs.total_cost_usd` adds the two together, which is why it is not quoted anywhere in this report and why no combined 'total cost' column exists. A probe-heavy system and a model-heavy system are not comparable on one axis.

Latency is SUMMED STEP LATENCY, a lower bound on wall-clock time: it excludes controller and scheduling overhead between steps.

System C: not run: no LLM endpoint configured — no probe points, dollars or tokens.

## Failure and efficiency metrics

| system | probe steps by tier | medium/high probe steps | repeated calls | invalid arguments | recovered / runs with a failed probe | early-stop regret cases / opportunities | unnecessary probe steps | A's extra probes that changed no action | undecodable decisions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | high=76 low=2980 medium=1528 | 1604 (1.10/run) | 0 | 0 | 0/0 (n/a) | 0/0 (n/a) | 119 (7.1%) | 0 | 0 |
| B | high=9 low=2979 medium=1443 | 1452 (1.00/run) | 0 | 0 | 0/0 (n/a) | 0/151 (0.0%) | 118 (7.7%) | 153 | 0 |
| R | high=9 low=2904 medium=1368 | 1377 (0.95/run) | 0 | 0 | 0/0 (n/a) | 0/160 (0.0%) | 0 (0.0%) | 303 | 0 |

`repeated calls` and `invalid arguments` are controller-forbidden events: any nonzero value is a real finding, not noise.

`unnecessary probe steps` is the LITERAL reading available in the trace — a dynamic probe execution that produced ZERO evidence rows for its own run. A probe whose evidence WAS recorded but did not move the action is not recoverable from the trace at all, so this number is a lower bound on wasted work, never an upper bound. The last column is the counterpart reading: dynamic probes the fuller reference system spent on cases where both systems ended up recommending the same action.

System C: not run: no LLM endpoint configured.

## Data quality

Scoped to system(s) `A` over splits `dev, validation, test`. System A runs every probe, so scoping here to A measures the cached record at its fullest rather than penalising it for System B's deliberately narrower probe set.

| measure | value |
| --- | --- |
| runs checked | 1452 |
| postings checked | 330 |
| ATS resolution rate | 1452/1452 (100.0%) |
| ATS distribution (distinct postings) | ashby=126 greenhouse=108 lever=96 |
| FIRST-publish coverage (dated `first_published`, ats_native/page_structured, after the first-published guard) | 216/1452 (14.9%) — ats_native=133 page_structured=91 |
| refresh / last-published coverage (`updated_at`, `last_published`, `refreshed_at`; says the posting moved, not when it first appeared) | 243/1452 (16.7%) — last_published=135 refreshed_at=18 updated_at=108 |
| any publish-family claim (either of the above, by source quality) | 326/1452 (22.5%) — ats_native=243 page_structured=191 |
| repost match precision | 85.7% (parsed 85.7% from data/match_precision.md) |

### Citation support, per system

| system | runs | reasons | every cited id exists | citing a missing id | citing no id | classified / unclassified | supported / unsupported (of classified) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | 1452 | 1920 | 1920 (100.0%) | 0 | 0 | 1919 / 1 | 1919 / 0 (100.0%) |
| B | 1452 | 1845 | 1845 (100.0%) | 0 | 0 | 1845 / 0 | 1845 / 0 (100.0%) |
| R | 1452 | 1845 | 1845 (100.0%) | 0 | 0 | 1845 / 0 | 1845 / 0 (100.0%) |

Each system is scored over its own scoped runs. A, B and R publish the deterministic reasons (`rli.policy.explain_stub`); C publishes the LLM's reasons that survived the explanation guard, or the deterministic ones on a fallback. An UNCLASSIFIED reason matched no claim family: it is reported, never guessed at, and counts as neither supported nor unsupported.

### Model calls, dropped citations and explanation fallbacks

| system | runs | model calls | investigator-error runs | LLM citations dropped: fabricated id | LLM reasons dropped as unsupported | explanation fallbacks (by reason) |
| --- | --- | --- | --- | --- | --- | --- |
| A | 1452 | 0 (0 investigator / 0 explanation) | 0 | 0 runs / 0 ids | 0 runs / 0 reasons | 0 ((none)) |
| B | 1452 | 0 (0 investigator / 0 explanation) | 0 | 0 runs / 0 ids | 0 runs / 0 reasons | 0 ((none)) |
| R | 1452 | 0 (0 investigator / 0 explanation) | 0 | 0 runs / 0 ids | 0 runs / 0 reasons | 0 ((none)) |

Read from `run_steps`: `citation_invalid:<n>` / `citation_unsupported:<n>` rows (the explanation guard dropped LLM citations or whole reasons), `explanation_fallback:<why>` (the deterministic reasons were published instead; `cost_cap` / `latency_cap` mean the explanation call was never made), and `run_flag:investigator_error` (or the older `controller_decision:stop:investigator_error`). An investigator-error run still completes: the controller stops and the frozen policy decides on the evidence so far. Such runs are INCLUDED in the per-system figures (agreement with A, action distributions, probe counts, cost, citation support), in the spec.md §6 agent gate and in the era and probe-dependent views. They are EXCLUDED from the C-vs-R rates (probe use and agreement per compared case), where they are counted separately as C failures and, at 10 or more, as a material loss on investigator reliability.

## Future leakage

| measure | value |
| --- | --- |
| violations (spec.md §6 target: 0) | 0 |
| clean | yes |

| violation kind | count |
| --- | --- |
| `evidence_after_t` | 0 |
| `evidence_fetched_after_t` | 0 |
| `capture_fetched_after_t` | 0 |
| `cache_miss` | 0 |
| `net_call` | 0 |
| `missing_probe_result` | 0 |
| `missing_replay_at` | 0 |
| `input_without_evidence` | 0 |

| reported, not counted as a violation | count |
| --- | --- |
| model cache misses (spec.md §6 allows live LLM calls on a miss) | 0 |
| blob input exposures (a served `data` blob answers a policy input no claim backs at T; needs a dataset rebuild) | 0 |
| pre-fix capture batches with no recorded snapshot run window (capture_fetched_after_t cannot see them; run `rli import-run-windows`) | 0 |

`evidence_fetched_after_t` counts evidence that passed the `available_at <= T` gate although the fetch behind it completed after T (checked against `tool_cache`); `evidence_after_t` trusts the stamp. `capture_fetched_after_t` counts cases whose T falls inside a pre-fix daily snapshot run window in which their company was captured (the capture is stamped with the run's start). A replayed system reaches the network never: `rli.replay.mode.ReplayNetClient` raises on any attempt and the violation is written into the trace, so this audit counts recorded attempts rather than inferring them.

## Posting behaviour (survival summary)

**Corpus-wide, NOT dataset-scoped.** `rli.eval.survival.behavior_report` takes no dataset or split argument — its only scope is a single company — so these curves describe every posting the collector has ever seen, including postings outside the evaluated split. Do not read them as properties of the dataset scored above.

| measure | value |
| --- | --- |
| total intervals | 40371 |
| closed (interval-censored) | 23419 |
| right-censored (still open) | 16952 |
| archive-only observations | 16045 |

| curve | n | events | censored | median days | note |
| --- | --- | --- | --- | --- | --- |
| right-censored (Kaplan-Meier) | 40371 | 23419 | 16952 | 121.1 |  |
| interval-censored | 40371 | 23419 | 16952 | 55.8 | median CI not available: lifelines 0.30.3's Turnbull/NPMLE estimator does not compute a confidence interval for fit_interval_censoring |

Closures are interval-censored by construction (spec.md §4/§5: an exact `closed_at` is never invented), so the two curves answer slightly different questions and are shown side by side rather than merged.

_corpus-wide (all companies): rli.eval.survival.behavior_report takes no dataset or split scope, so these curves are NOT dataset-scoped_

## Agent gate

spec.md §6: System C must use <= 70% of System B's medium/high-cost probes while staying within 2% of B's agreement with System A, overall AND macro-averaged.

Verdict: **NOT RUN** (candidate runs: 0, baseline runs: 1452).

Whether a pass is the LLM's doing or the deterministic controller's is answered by 'Does the LLM add anything? (System C vs System R)' below.

> **System A structural caveat** — printed here, beside the verdict rather than in a footnote, because a probe-count comparison read without it is misleading.
>
> STRUCTURAL CAVEAT: System A is not a neutral upper bound. `rli.eval.system_a` calls `eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`, which makes spec.md §4's unresolved-question gate vacuous for A: A runs every dynamic probe that survives the history and licensing gates, whether or not that probe could change the action. System C is gated by `rli.policy.inputs.could_change_action` and therefore skips probes A always runs. Every probe-count comparison against A (medium/high probe use, cost points, latency, 'unnecessary probes', early-stop regret) is biased in the leaner system's favour BY CONSTRUCTION, not by measurement. This is why spec.md §6's agent gate measures probe use against System B rather than against A. Read agreement-with-A as an accuracy figure, and probe-count-vs-A as an upper bound on achievable savings — never as evidence that A wasted work.

| leg | candidate (C) | baseline (B) | requirement | pass? |
| --- | --- | --- | --- | --- |
| medium/high probes per run | n/a | 1.00 | ratio n/a <= 0.70 | n/a |
| overall agreement with A | n/a | 100.0% | >= n/a | n/a |
| macro agreement with A | n/a | 100.0% | >= n/a | n/a |

The gate is `not_run`, not `fail`: System C produced zero scoped runs (skipped: not requested (pass with_c=True / --with-c)). An ungraded candidate has not failed. All candidate columns above read `not run: no LLM endpoint configured`.

Notes:

- STRUCTURAL CAVEAT: System A is not a neutral upper bound. `rli.eval.system_a` calls `eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`, which makes spec.md §4's unresolved-question gate vacuous for A: A runs every dynamic probe that survives the history and licensing gates, whether or not that probe could change the action. System C is gated by `rli.policy.inputs.could_change_action` and therefore skips probes A always runs. Every probe-count comparison against A (medium/high probe use, cost points, latency, 'unnecessary probes', early-stop regret) is biased in the leaner system's favour BY CONSTRUCTION, not by measurement. This is why spec.md §6's agent gate measures probe use against System B rather than against A. Read agreement-with-A as an accuracy figure, and probe-count-vs-A as an upper bound on achievable savings — never as evidence that A wasted work.
- the held-out 'test' split is IN SCOPE for this gate. spec.md §6 permits this once, for the final evaluation; any tuning decision made after reading it invalidates the holdout.
- system C has no runs in scope for dataset 'company-7d-v4' on splits ('dev', 'validation', 'test'), so the gate was not evaluated. On the current database this is expected for System C: no LLM endpoint was configured or reachable, so C was never run. `passed` is None (unknown), NOT False (failed).
- System C: skipped: not requested (pass with_c=True / --with-c)

## Does the LLM add anything? (System C vs System R)

System R runs System C's controller, eligibility gate, ranking and budgets with no LLM: its investigator step proposes every eligible probe in rank order and its explanation is the deterministic one. Anything C does better than R is the LLM's contribution. This comparison is NOT a spec.md §6 gate (the agent gate above is kept exactly as specified); it is what spec.md §6's "If rules are equally good and simpler, remove the agent" needs.

Result: **NOT COMPARED**.

Not compared: no paired C/R case (C missing). Replay System R offline (`rli replay run --system R --dataset company-7d-v4`; no LLM) and re-run the evaluation to see whether the LLM adds anything over the deterministic controller.

A dimension is credited to (or charged against) C only when the difference is material: medium/high probe use per compared case differs by >= 1% relative, or agreement with A (overall or macro) by >= 1% points, AND at least 10 compared cases move in that direction (net, per improving class for agreement). Fewer than 30 compared cases is inconclusive. Rates are over paired cases only, excluding C investigator-error runs; 10 or more such runs is a material loss on investigator reliability.


## Product gate

spec.md §6: on held-out postings, following the recommended action must lower applications per screen or per interview versus the comparison group.

Verdict: **UNPROVEN**.

Reason: unproven: the `outcomes` table is empty: no application, screen or interview outcome has ever been recorded, so the product claim has no evidence of any kind; no posting with recorded outcomes falls in the held-out split(s) ('test',); applications-per-screen is undefined for the recommended group (n/a) or the comparison group (n/a): no screen outcomes to divide by; applications-per-interview is undefined for the recommended group (n/a) or the comparison group (n/a): no interview outcomes to divide by

| measure | value |
| --- | --- |
| outcomes recorded | 0 |
| postings with outcomes | 0 |
| postings matched to a run | 0 |
| held-out postings | 0 |
| outcome counts | (none) |
| minimum outcomes required | 30 |
| recommended actions | apply_now, quick_apply |
| effort per screen (recommended vs comparison) | n/a vs n/a |
| effort per interview (recommended vs comparison) | n/a vs n/a |

### Outcomes by recommended action

| action | postings | applied | screen | interview | offer | rejection | silence | applied/screen | applied/interview |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| (none) | (none) | (none) | (none) | (none) | (none) | (none) | (none) | (none) | (none) |

`unproven` is not `fail`. A gate with no outcome data has not been failed, it has not been run; `fail` is reserved for a gate that had enough data and lost.

## C2 — learned probe ranking

spec.md §6's C2 is System C with a learned probe ranking substituted for the deterministic one. The label here is a PROXY built from the traces ("did running this probe move the partial action toward the reference action?"), not a ground-truth utility; the deterministic comparison score is `-probe_cost_points`, which is exactly what `rli.agent.controller.rank_candidates` falls back to once value is tied.

| measure | value |
| --- | --- |
| status | degenerate |
| rows (candidate probe decisions) | 1680 |
| train / holdout rows | 1260 / 420 |
| positive rate (train / holdout) | 0.0% / 0.0% |
| features | has_board_absent, has_board_present, has_publish_evidence, n_evidence, n_evidence_archive, n_evidence_ats_native, n_evidence_enrichment, n_evidence_news, n_evidence_page_structured, n_probes_before, probe_cost_points, probe_is_company_events, probe_is_repost_history, probe_is_requirements_drift, probe_is_team_signal |
| learned AUC / accuracy | n/a / n/a |
| deterministic AUC / accuracy | n/a / n/a |
| gain (AUC / accuracy) | n/a / n/a |
| keep the learned ranker? | NO |

Note: train or holdout labels are a single class; AUC is undefined for this split

`degenerate` means there were enough rows to try, but one side of the temporal split carried a single label class, so AUC is undefined and no comparison against the deterministic ranking is possible. Nothing is kept. This is a property of the proxy label on this dataset, not evidence that a learned ranking cannot help.

## Limitations

- Archive-era cases are weak by construction. For a case whose T predates ITS company's first own board capture, the only observation available is a Wayback capture, so `board_snapshot` evidence is sparse, `source_quality='archive'`, and often absent entirely. On this dataset 5 of 1452 scoped cases are archive-era (per-company boundary; 64/64 companies have any own capture); they are scored, and the pooled agreement figures average the two eras together.
- `team_signal` is ENABLED in this config (`[team_signal].enabled = true`); it is the only `high`-tier probe and the only source of `corroborating_hiring_signal`, which policy branches P4 (repeated unchanged repost -> skip) and P5b (hiring activity -> apply_now) need. Measured on this dataset's scoped runs: `team_signal` executions A=76, B=9, R=9; high-tier probe executions A=76, B=9, R=9; P4 fired A=0, B=0, R=0; P5b fired A=9, B=9, R=9. P4 never fired, so no `skip` in this report comes from the repost branch.
- Company-event coverage: 221/351 companies in this database carry any `company_events` row (773 event(s) in total). `company_events` is a medium-cost probe and a policy input; for a company with no row the material-negative-event and hiring-freeze inputs are False only when the probe's collection status says the company was searched, and UNKNOWN otherwise (spec.md §4: missing history never means flat hiring).
- Sample sizes vs. spec.md §6 targets — built: 330 postings, 64 companies, 1452 cases; SCORED: 330 postings (target >=300), 64 companies (target >=40), 1452 cases (0 identity-unresolved, 0 unassigned and 0 out-of-scope holdout case(s) excluded); collection corpus: 40514 postings, 351 companies, 23900 closure events (target >=100). The headline gate is judged on what was SCORED, and it is MET. The closure-event leg has no dataset-scoped equivalent (a replay dataset's unit is a (posting, T) grid point, not a closure) and is therefore corpus-wide.
- Probe cost POINTS and model DOLLARS are different units and are never summed. `run_steps.cost_usd` holds placeholder cost points on `component='probe'` rows and real USD on `component='model'` rows; `runs.total_cost_usd` adds them, which is why no single 'total cost' figure appears anywhere in this report.
- STRUCTURAL CAVEAT: System A is not a neutral upper bound. `rli.eval.system_a` calls `eligible_probes(..., unpopulated_inputs=set(ALL_DYNAMIC_INPUTS))`, which makes spec.md §4's unresolved-question gate vacuous for A: A runs every dynamic probe that survives the history and licensing gates, whether or not that probe could change the action. System C is gated by `rli.policy.inputs.could_change_action` and therefore skips probes A always runs. Every probe-count comparison against A (medium/high probe use, cost points, latency, 'unnecessary probes', early-stop regret) is biased in the leaner system's favour BY CONSTRUCTION, not by measurement. This is why spec.md §6's agent gate measures probe use against System B rather than against A. Read agreement-with-A as an accuracy figure, and probe-count-vs-A as an upper bound on achievable savings — never as evidence that A wasted work.
- Latency is SUMMED STEP LATENCY (`runs.total_latency_ms`), a lower bound on wall-clock time: it excludes controller and scheduling overhead between steps.
- Where strong evidence sits in time: 153 of 214 strong-evidence cases (71.5%) are BUILD-TIME cases (T at or after the build start 2026-10-07T23:50:53.813461Z; 246 case(s) in all), and only 166 of 306 grid point(s) have any. apply_now at build time: A 62/85, B 62/85, R 62/85.
- Only 67 of 1452 cases (4.6%) are probe-dependent: on the rest, System A's action equals the action the frozen policy gives with NO dynamic-probe evidence. On every case outside the subset every system agrees with A (no disagreement was found there), so overall/macro agreement is inflated by them. See 'Probe-dependent cases'.
- This evaluation was READ-ONLY: no system was replayed and no holdout marker was written to the trace. Systems without runs on this dataset are absent from every figure (see 'Systems run').
- The read-side split filter PERMITTED the `test` holdout (`allow_test=True`), but no scoped case fell in it, so no holdout data was actually read and no `holdout_test_evaluated` marker was written. The evaluated dataset was drawn from a non-holdout split; the holdout remains untouched.
- The posting-behaviour (survival) section is CORPUS-WIDE, not dataset-scoped: `rli.eval.survival.behavior_report` has no dataset or split argument, so its curves describe every posting the collector has ever seen, including postings outside the evaluated split.
